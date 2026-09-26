"""OGC WFS-источник + заготовка под ФГИС ТП (fgistp.economy.gov.ru).

ФГИС ТП и региональные ГИСОГД публикуют ПЗЗ/генпланы по-разному: где-то есть WFS
(тогда этот класс работает как есть), где-то только WMS-картинки или выгрузки документов.

Как найти рабочий URL:
  1. Откройте карту ФГИС ТП / ГИСОГД Приморья в браузере, включите слой ПЗЗ.
  2. DevTools → Network → фильтр "wfs" / "wms" / "GetFeature".
  3. Если видите GetFeature/GetCapabilities — берите базовый URL и typeName в config.yaml.
  4. Если только WMS-тайлы — используйте FileSource (скачанный GeoJSON/SHP) или
     слой «Территориальные зоны» из НСПД (он уже подключён в сканере по умолчанию).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from shapely.geometry import mapping

from ..geo import BBox, WGS84, feature_geometry

log = logging.getLogger(__name__)


@dataclass
class WfsSource:
    name: str
    url: str
    type_name: str
    version: str = "2.0.0"
    extra_params: dict[str, Any] = field(default_factory=dict)
    timeout: float = 30.0
    max_features: int = 5000
    verify: Any = True  # путь к CA-сертификату Минцифры или False для гос. сайтов с «русским» SSL

    def build_params(self, bbox: BBox) -> dict[str, Any]:
        x1, y1, x2, y2 = bbox
        type_key = "typeNames" if self.version.startswith("2") else "typeName"
        count_key = "count" if self.version.startswith("2") else "maxFeatures"
        return {
            "service": "WFS",
            "request": "GetFeature",
            "version": self.version,
            type_key: self.type_name,
            "outputFormat": "application/json",
            "srsName": WGS84,
            # явный CRS в bbox снимает неоднозначность порядка осей
            "bbox": f"{x1},{y1},{x2},{y2},urn:ogc:def:crs:OGC:1.3:CRS84",
            count_key: self.max_features,
            **self.extra_params,
        }

    def fetch(self, bbox: BBox) -> list[dict]:
        if not self.url or not self.type_name:
            log.warning("Источник %s: не задан url/type_name — пропускаю", self.name)
            return []
        r = httpx.get(self.url, params=self.build_params(bbox), timeout=self.timeout, verify=self.verify)
        r.raise_for_status()
        feats = r.json().get("features") or []
        out = []
        for f in feats:
            g = feature_geometry(f)
            if g is None:
                continue
            out.append({"type": "Feature", "geometry": mapping(g), "properties": {**(f.get("properties") or {}), "_source": self.name}})
        return out


@dataclass
class FgisTpSource(WfsSource):
    """ФГИС ТП. URL и typeName задаются в config.yaml после разведки через DevTools.

    TODO(вы): когда найдёте рабочий WFS — пропишите маппинг полей зоны в `normalize_zone`,
    чтобы индекс зоны (Ж-1, СХ-2...) и перечень ВРИ попали в отчёт так же, как из НСПД.
    """

    @staticmethod
    def normalize_zone(props: dict) -> dict:
        # Типовые имена полей в выгрузках ПЗЗ; поправьте под реальный сервис
        code = props.get("zone_code") or props.get("INDEX") or props.get("code") or props.get("name")
        vri = props.get("vri") or props.get("permitted_use") or props.get("VRI")
        return {"zone_code": code, "permitted_uses": vri}

    def fetch(self, bbox: BBox) -> list[dict]:
        feats = super().fetch(bbox)
        for f in feats:
            f["properties"].update(self.normalize_zone(f["properties"]))
        return feats
