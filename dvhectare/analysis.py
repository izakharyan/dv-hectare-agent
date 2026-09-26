"""Ядро: сбор слоёв по bbox → вычисление свободной территории → кандидаты под гектар.

Логика «свободной земли» для ДВ-гектара:
    свободно = AOI − все ЗУ из ЕГРН − исключающие слои (ООПТ, ТОР, ОЭЗ, ОКН,
               образуемые по схемам/ПМТ) − доп. исключения (файлы/WFS)
Затем свободная территория нарезается на квадраты 100×100 м (1 га) с отступом от границ,
и каждому квадрату приписываются территориальная зона и «флаги» (ЗОУИТ, лесничество,
населённый пункт, водоём), которые требуют ручной проверки.

Ограничения метода (важно понимать):
  * Ранее учтённые участки БЕЗ границ в ЕГРН на карте не видны → «свободная» земля может
    оказаться чьей-то. Финальную проверку делает уполномоченный орган при подаче заявления.
  * Серые зоны надальнийвосток.рф (резерв, недра, КМНС и т.п.) в НСПД не публикуются —
    подключайте их как FileSource/WFS с role: exclude, если получите выгрузку.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from pyproj import Transformer
from shapely.geometry import MultiPolygon, Polygon, box, mapping
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from .config import ParcelFilter, Settings
from .geo import (
    BBox,
    area_m2,
    bbox_area_km2,
    dedupe,
    feature_geometry,
    feature_options,
    hectare_cells,
    union_of,
)
from .layers import get_layer
from .nspd.client import NspdClient
from .sources import LayerSource, build_source

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- записи
def parcel_record(feature: dict) -> dict[str, Any]:
    """Плоская запись ЗУ из ответа НСПД (поля слоя 36048)."""
    o = feature_options(feature)
    g = feature_geometry(feature)
    return {
        "cad_num": o.get("cad_num"),
        "category": o.get("land_record_category_type"),
        "permitted_use": o.get("permitted_use_established_by_document"),
        "ownership": o.get("ownership_type"),
        "status": o.get("status"),
        "area_m2": o.get("specified_area") or o.get("declared_area") or o.get("area"),
        "address": o.get("readable_address"),
        "cost_rub": o.get("cost_value"),
        "subtype": o.get("land_record_subtype"),
        "geometry": g,
    }


def zone_title(feature: dict) -> str:
    p = feature.get("properties") or {}
    o = p.get("options") or {}
    for v in (p.get("label"), p.get("descr"), o.get("name_by_doc"), o.get("type_zone"), o.get("reg_numb_border")):
        if v:
            return str(v)
    return str(feature.get("id"))


def filter_parcels(records: Iterable[dict], f: ParcelFilter) -> list[dict]:
    out = []
    kws = [k.lower() for k in f.vri_keywords]
    for r in records:
        vri = (r.get("permitted_use") or "").lower()
        status = r.get("status") or ""
        area = r.get("area_m2") or 0
        if kws and not any(k in vri for k in kws):
            continue
        if any(s.lower() in status.lower() for s in f.exclude_statuses):
            continue
        if area and not (f.min_area_m2 <= area <= f.max_area_m2):
            continue
        out.append(r)
    return out


def nspd_map_link(lat: float, lon: float, zoom: int = 17) -> str:
    """Ссылка на карту НСПД (она принимает координаты в EPSG:3857)."""
    x, y = _to_3857(lon, lat)
    return f"https://nspd.gov.ru/map?thematic=PKK&zoom={zoom}&coordinate_x={x:.2f}&coordinate_y={y:.2f}"


_to_3857 = Transformer.from_crs(4326, 3857, always_xy=True).transform


# ---------------------------------------------------------------- результат
@dataclass
class Candidate:
    id: int
    geometry: BaseGeometry
    area_m2: float
    lat: float
    lon: float
    zones: list[str] = field(default_factory=list)
    zone_permitted_uses: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "area_m2": round(self.area_m2),
            "zones": self.zones,
            "zone_permitted_uses": self.zone_permitted_uses[:15],
            "flags": self.flags,
            "nspd_link": nspd_map_link(self.lat, self.lon),
        }


@dataclass
class ScanResult:
    bbox: BBox
    parcels: list[dict]
    free_parcels: list[dict]
    zones: list[dict]
    exclusions: dict[str, list[dict]]
    flags: dict[str, list[dict]]
    free_area: BaseGeometry
    candidates: list[Candidate]
    stats: dict[str, Any]

    def summary(self) -> dict:
        return {
            "bbox": self.bbox,
            **self.stats,
            "top_candidates": [c.as_dict() for c in self.candidates[:10]],
            "free_parcels_matching_filter": [
                {k: v for k, v in r.items() if k != "geometry"} for r in self.free_parcels[:20]
            ],
        }


# ---------------------------------------------------------------- сканер
class Scanner:
    def __init__(self, client: NspdClient, settings: Settings, extra_sources: Optional[list[LayerSource]] = None):
        self.client = client
        self.s = settings
        self.extra: list[tuple[str, LayerSource]] = []
        if extra_sources is None:
            for cfg in settings.extra_sources:
                self.extra.append((cfg.role, build_source(cfg)))
        else:
            self.extra = [("exclude", src) for src in extra_sources]

    # --- слои
    def fetch_layer(self, key: str, bbox: BBox) -> list[dict]:
        layer = get_layer(key)
        feats = dedupe(self.client.iter_bbox(bbox, layer.category_id))
        # intersects по одной категории может вернуть «соседей» — отфильтруем по category
        feats = [f for f in feats if (f.get("properties") or {}).get("category") in (None, layer.category_id)]
        log.info("Слой %-18s: %d объектов", key, len(feats))
        return feats

    def zones(self, bbox: BBox, with_permitted_uses: Optional[bool] = None, limit: int = 60) -> list[dict]:
        feats = self.fetch_layer("terr_zones", bbox)
        want = self.s.scan.fetch_zone_permitted_uses if with_permitted_uses is None else with_permitted_uses
        for i, f in enumerate(feats):
            f.setdefault("properties", {})["_title"] = zone_title(f)
            if want and i < limit:
                try:
                    f["properties"]["_permitted_uses"] = self.client.tab_values(f, "permissionType") or []
                except Exception as e:  # вкладка есть не у всех зон
                    log.debug("ВРИ зоны %s: %s", f.get("id"), e)
                    f["properties"]["_permitted_uses"] = []
        for role, src in self.extra:
            if role == "zones":
                for f in src.fetch(bbox):
                    p = f["properties"]
                    p["_title"] = p.get("zone_code") or p.get("name") or src.name
                    p["_permitted_uses"] = p.get("permitted_uses") or []
                    feats.append(f)
        return feats

    # --- основной сценарий
    def scan(self, bbox: BBox) -> ScanResult:
        km2 = bbox_area_km2(bbox)
        if km2 > self.s.scan.max_area_km2:
            raise ValueError(
                f"Область {km2:.1f} км² больше лимита {self.s.scan.max_area_km2} км². "
                "Уменьшите bbox или поднимите scan.max_area_km2 в config.yaml."
            )
        aoi = box(*bbox)
        req0 = self.client.request_count

        parcels_raw = self.fetch_layer("parcels", bbox)
        parcels = [parcel_record(f) for f in parcels_raw]

        free_raw = self.fetch_layer("free_parcels", bbox)
        free_parcels = filter_parcels([parcel_record(f) for f in free_raw], self.s.parcel_filter)

        exclusions = {k: self.fetch_layer(k, bbox) for k in self.s.scan.exclude_layers}
        flags = {k: self.fetch_layer(k, bbox) for k in self.s.scan.flag_layers}
        for role, src in self.extra:
            if role in ("exclude", "flag"):
                target = exclusions if role == "exclude" else flags
                target[src.name] = src.fetch(bbox)

        zones = self.zones(bbox)

        # --- свободная территория
        occupied = union_of(r["geometry"] for r in parcels)
        excluded = union_of(feature_geometry(f) for fs in exclusions.values() for f in fs)
        free = aoi.difference(occupied).difference(excluded)
        patches = [p for p in _polygons(free) if area_m2(p) >= self.s.scan.hectare.min_patch_m2]
        free = MultiPolygon(patches) if patches else Polygon()

        # --- кандидаты
        h = self.s.scan.hectare
        cells: list[BaseGeometry] = []
        for p in sorted(patches, key=lambda g: -g.area):
            cells += hectare_cells(p, side_m=h.side_m, inset_m=h.inset_m, max_cells=h.max_cells - len(cells))
            if len(cells) >= h.max_cells:
                break

        zone_index = _Index([(f["properties"]["_title"], feature_geometry(f), f["properties"].get("_permitted_uses") or []) for f in zones])
        flag_items = []
        for key, fs in flags.items():
            for f in fs:
                flag_items.append((f"{key}: {zone_title(f)}", feature_geometry(f), []))
        flag_index = _Index(flag_items)

        patterns = [re.compile(p, re.I) for p in self.s.scan.allowed_zone_patterns]
        candidates: list[Candidate] = []
        for cell in cells:
            zhits = zone_index.hits(cell)
            ztitles = [t for t, _ in zhits]
            if patterns and not any(p.search(t) for p in patterns for t in ztitles):
                continue
            uses = sorted({u for _, us in zhits for u in us})
            c = cell.centroid
            candidates.append(
                Candidate(
                    id=len(candidates) + 1,
                    geometry=cell,
                    area_m2=area_m2(cell),
                    lat=c.y,
                    lon=c.x,
                    zones=ztitles,
                    zone_permitted_uses=uses,
                    flags=[t for t, _ in flag_index.hits(cell)],
                )
            )
        # сначала «чистые» кандидаты без флагов
        candidates.sort(key=lambda c: (len(c.flags), c.id))
        for i, c in enumerate(candidates, 1):
            c.id = i

        stats = {
            "aoi_km2": round(km2, 2),
            "parcels_in_egrn": len(parcels),
            "free_parcels_layer": len(free_raw),
            "free_parcels_matching_filter": len(free_parcels),
            "terr_zones": len(zones),
            "exclusions": {k: len(v) for k, v in exclusions.items()},
            "flags": {k: len(v) for k, v in flags.items()},
            "free_area_ha": round(area_m2(free) / 10_000, 2) if not free.is_empty else 0,
            "free_patches_ge_1ha": len(patches),
            "hectare_candidates": len(candidates),
            "nspd_requests": self.client.request_count - req0,
        }
        return ScanResult(bbox, parcels, free_parcels, zones, exclusions, flags, free, candidates, stats)


def _polygons(g: BaseGeometry) -> list[Polygon]:
    if g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if hasattr(g, "geoms"):
        out = []
        for part in g.geoms:
            out += _polygons(part)
        return out
    return []


class _Index:
    """STRtree-индекс (title, geom, extra) для быстрых пересечений."""

    def __init__(self, items: list[tuple[str, Optional[BaseGeometry], list]]):
        self.items = [(t, g, x) for t, g, x in items if g is not None and not g.is_empty]
        self.tree = STRtree([g for _, g, _ in self.items]) if self.items else None

    def hits(self, geom: BaseGeometry) -> list[tuple[str, list]]:
        if not self.tree:
            return []
        idx = self.tree.query(geom, predicate="intersects")
        seen, out = set(), []
        for i in idx:
            t, _, x = self.items[int(i)]
            if t not in seen:
                seen.add(t)
                out.append((t, x))
        return out


def geometry_to_geojson(g: BaseGeometry) -> dict:
    return mapping(g)
