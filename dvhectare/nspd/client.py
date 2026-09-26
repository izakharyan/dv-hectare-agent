"""HTTP-клиент НСПД (nspd.gov.ru). ПКК (pkk.rosreestr.ru) закрыта — все данные теперь здесь.

Используемые (неофициальные, но стабильно работающие) эндпоинты:
  GET  /api/geoportal/v2/search/geoportal?thematicSearchId=1&query=<КН>   — поиск по КН/адресу
  POST /api/geoportal/v1/intersects?typeIntersect=fullObject                — объекты слоя в полигоне
  GET  /api/aeggis/v3/{layer_id}/wms?REQUEST=GetFeatureInfo...              — объекты слоя в точке
  GET  /api/geoportal/v1/tab-values-data?tabClass=...                       — вкладки карточки (ВРИ зоны и т.п.)

Важно:
  * НСПД отвечает только на российские IP (зарубежные получают 403).
  * Сертификат выдан НУЦ Минцифры (Russian Trusted Root CA). Лучше указать путь к нему
    в `ca_bundle`; иначе клиент отключит проверку SSL (как делают pynspd/rosreestr2coord).
  * Лимиты не документированы — держим паузу между запросами и кэшируем ответы.
"""
from __future__ import annotations

import logging
import random
import ssl
import time
from typing import Any, Iterator, Optional

import httpx
from shapely.geometry import box, mapping
from shapely.geometry.base import BaseGeometry

from .cache import ResponseCache

log = logging.getLogger(__name__)

BASE_URL = "https://nspd.gov.ru"
DEFAULT_HEADERS = {
    "accept": "*/*",
    "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
    "referer": "https://nspd.gov.ru/map?thematic=PKK",
    "origin": "https://nspd.gov.ru",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
}


class NspdError(Exception):
    pass


class BlockedIP(NspdError):
    """403 — IP заблокирован или не российский."""


class TooBigContour(NspdError):
    """НСПД отказался обрабатывать контур — надо дробить."""


class NotFound(NspdError):
    pass


def _ssl_verify(ca_bundle: Optional[str]) -> ssl.SSLContext | str:
    if ca_bundle:
        return ca_bundle
    log.warning(
        "SSL-проверка для nspd.gov.ru отключена (нет ca_bundle). "
        "Скачайте корневой сертификат Минцифры: https://www.gosuslugi.ru/crt"
    )
    ctx = ssl._create_unverified_context()
    ctx.set_ciphers("ALL:@SECLEVEL=1")
    return ctx


class NspdClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        min_delay: float = 1.0,
        retries: int = 3,
        cache_path: Optional[str] = ".cache/nspd.sqlite",
        cache_ttl: int = 7 * 24 * 3600,
        ca_bundle: Optional[str] = None,
        proxy: Optional[str] = None,
        transport: Optional[httpx.BaseTransport] = None,  # для тестов
    ):
        self.min_delay = min_delay
        self.retries = retries
        self._last_request = 0.0
        self.cache = ResponseCache(cache_path, cache_ttl) if cache_path else None
        kwargs: dict[str, Any] = dict(
            base_url=BASE_URL, timeout=timeout, headers=DEFAULT_HEADERS, follow_redirects=True
        )
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = _ssl_verify(ca_bundle)
            if proxy:
                kwargs["proxy"] = proxy
        self._http = httpx.Client(**kwargs)
        self.request_count = 0

    # ------------------------------------------------------------------ low level
    def close(self) -> None:
        self._http.close()
        if self.cache:
            self.cache.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _throttle(self) -> None:
        wait = self.min_delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait + random.uniform(0, self.min_delay * 0.3))
        self._last_request = time.monotonic()

    def request_json(
        self, method: str, path: str, *, params: Optional[dict] = None, json: Optional[dict] = None
    ) -> Any:
        key = None
        if self.cache:
            key = ResponseCache.make_key(method, path, params, json)
            cached = self.cache.get(key)
            if cached is not None:
                return cached

        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                r = self._http.request(method, path, params=params, json=json)
                self.request_count += 1
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                log.debug("Сетевая ошибка %s (попытка %s)", e, attempt)
                time.sleep(2 ** attempt)
                continue

            if r.status_code == 403:
                raise BlockedIP("403 от НСПД: IP заблокирован или не из РФ. Нужен российский IP/прокси.")
            if r.status_code == 404:
                raise NotFound(path)
            if r.status_code == 429 or r.status_code >= 500:
                body = r.text[:500]
                if '"code":400104' in body:
                    raise TooBigContour(body)
                last_exc = NspdError(f"HTTP {r.status_code}: {body}")
                time.sleep(3 * (2 ** attempt))
                continue
            if r.status_code >= 400:
                raise NspdError(f"HTTP {r.status_code}: {r.text[:500]}")
            try:
                data = r.json()
            except ValueError as e:
                # НСПД на слишком больших контурах иногда отдаёт не-JSON
                if path.endswith("/intersects"):
                    raise TooBigContour("не-JSON ответ на intersects") from e
                raise NspdError(f"Не-JSON ответ: {r.text[:200]}") from e
            if self.cache and key:
                self.cache.set(key, data)
            return data
        raise NspdError(f"Исчерпаны попытки: {last_exc}")

    # ------------------------------------------------------------------ search
    def search(self, query: str, thematic_search_id: int = 1) -> list[dict]:
        """Поиск как в строке поиска НСПД. thematic_search_id: 1 — объекты недвижимости,
        2 — кадастровое деление, 4 — АТД, 5 — зоны и территории, 7 — территориальные зоны."""
        try:
            data = self.request_json(
                "GET",
                "/api/geoportal/v2/search/geoportal",
                params={"thematicSearchId": thematic_search_id, "query": query},
            )
        except NotFound:
            return []
        return ((data or {}).get("data") or {}).get("features") or []

    def find_parcel(self, cad_number: str) -> Optional[dict]:
        """Земельный участок по кадастровому номеру (или None)."""
        cn = cad_number.strip()
        for f in self.search(cn, 1):
            opts = (f.get("properties") or {}).get("options") or {}
            if opts.get("cad_num") == cn or opts.get("cad_number") == cn:
                return f
        return None

    # ------------------------------------------------------------------ area search
    def intersects(self, geom_4326: BaseGeometry, *category_ids: int) -> list[dict]:
        """Все объекты указанных категорий, пересекающие полигон (WGS84)."""
        g = dict(mapping(geom_4326))
        g["crs"] = {"type": "name", "properties": {"name": "EPSG:4326"}}
        payload = {
            "categories": [{"id": c} for c in category_ids],
            "geom": {
                "type": "FeatureCollection",
                "features": [{"type": "Feature", "geometry": g, "properties": {}}],
            },
        }
        data = self.request_json(
            "POST", "/api/geoportal/v1/intersects", params={"typeIntersect": "fullObject"}, json=payload
        )
        return (data or {}).get("features") or []

    def iter_bbox(
        self,
        bbox: tuple[float, float, float, float],
        *category_ids: int,
        max_depth: int = 6,
        _depth: int = 0,
    ) -> Iterator[dict]:
        """Поиск в bbox с автоматическим дроблением на 4 части, если НСПД отказал.
        Дубликаты (объект на стыке тайлов) НЕ убираются — это делает вызывающий код."""
        xmin, ymin, xmax, ymax = bbox
        try:
            yield from self.intersects(box(xmin, ymin, xmax, ymax), *category_ids)
            return
        except TooBigContour:
            if _depth >= max_depth:
                log.warning("bbox %s не удалось обработать даже после дробления", bbox)
                return
        mx, my = (xmin + xmax) / 2, (ymin + ymax) / 2
        for sub in ((xmin, ymin, mx, my), (mx, ymin, xmax, my), (xmin, my, mx, ymax), (mx, my, xmax, ymax)):
            yield from self.iter_bbox(sub, *category_ids, max_depth=max_depth, _depth=_depth + 1)

    # ------------------------------------------------------------------ point search
    def features_at_point(self, lon: float, lat: float, layer_id: int, feature_count: int = 10) -> list[dict]:
        """WMS GetFeatureInfo: что лежит в точке на указанном слое."""
        d = 0.00001  # ~1 м
        size = 101
        params = {
            "REQUEST": "GetFeatureInfo",
            "SERVICE": "WMS",
            "VERSION": "1.3.0",
            "INFO_FORMAT": "application/json",
            "FORMAT": "image/png",
            "STYLES": "",
            "TRANSPARENT": "true",
            "QUERY_LAYERS": layer_id,
            "LAYERS": layer_id,
            "WIDTH": size,
            "HEIGHT": size,
            "I": size // 2,
            "J": size // 2,
            "CRS": "EPSG:4326",
            # НСПД принимает порядок lon,lat (как в pynspd), несмотря на WMS 1.3.0
            "BBOX": f"{lon - d},{lat - d},{lon + d},{lat + d}",
            "FEATURE_COUNT": feature_count,
        }
        try:
            data = self.request_json("GET", f"/api/aeggis/v3/{layer_id}/wms", params=params)
        except NotFound:
            return []
        return (data or {}).get("features") or []

    # ------------------------------------------------------------------ card tabs
    def tab_values(self, feature: dict, tab_class: str) -> Optional[list[str]]:
        """Вкладка карточки объекта. tab_class: permissionType (ВРИ зоны), landParts, landLinks..."""
        props = feature.get("properties") or {}
        opts = props.get("options") or {}
        if opts.get("geocoderObject"):
            params = {"tabClass": tab_class, "objdocId": opts.get("objdocId"), "registersId": opts.get("registersId")}
        else:
            params = {"tabClass": tab_class, "categoryId": props.get("category"), "geomId": feature.get("id")}
        try:
            data = self.request_json("GET", "/api/geoportal/v1/tab-values-data", params=params)
        except NotFound:
            return None
        val = (data or {}).get("value")
        if isinstance(val, list) and val == [""]:
            return None
        return val
