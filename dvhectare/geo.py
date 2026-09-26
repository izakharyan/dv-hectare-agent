"""Геометрия: разбор ответов НСПД, проекции, bbox, сетка «гектаров»."""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Iterable, Optional

from pyproj import CRS, Transformer
from shapely import make_valid
from shapely.geometry import Point, box, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

WGS84 = "EPSG:4326"

BBox = tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max


# ---------------------------------------------------------------- проекции
@lru_cache(maxsize=64)
def _transformer(src: str, dst: str) -> Transformer:
    return Transformer.from_crs(CRS(src), CRS(dst), always_xy=True)


def reproject(geom: BaseGeometry, src: str, dst: str) -> BaseGeometry:
    if src == dst:
        return geom
    return transform(_transformer(src, dst).transform, geom)


def utm_crs_for(lon: float, lat: float) -> str:
    """Метрическая UTM-зона для точки (для Приморья — 32652/32653)."""
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{32600 + zone if lat >= 0 else 32700 + zone}"


def to_metric(geom: BaseGeometry) -> tuple[BaseGeometry, str]:
    c = geom.centroid
    crs = utm_crs_for(c.x, c.y)
    return reproject(geom, WGS84, crs), crs


def area_m2(geom_4326: BaseGeometry) -> float:
    g, _ = to_metric(geom_4326)
    return g.area


# ---------------------------------------------------------------- bbox
def parse_bbox(text: str) -> BBox:
    parts = [float(x) for x in text.replace(";", ",").split(",")]
    if len(parts) != 4:
        raise ValueError("bbox: ожидается lon_min,lat_min,lon_max,lat_max")
    x1, y1, x2, y2 = parts
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def bbox_around(lat: float, lon: float, radius_km: float) -> BBox:
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * math.cos(math.radians(lat)))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def bbox_area_km2(bbox: BBox) -> float:
    return area_m2(box(*bbox)) / 1e6


# ---------------------------------------------------------------- фичи НСПД
def feature_geometry(feature: dict) -> Optional[BaseGeometry]:
    """Геометрия объекта НСПД → shapely в WGS84.
    НСПД часто отдаёт EPSG:3857 и указывает это в geometry.crs."""
    g = feature.get("geometry")
    if not g or not g.get("coordinates"):
        return None
    crs_name = ((g.get("crs") or {}).get("properties") or {}).get("name") or WGS84
    crs_name = crs_name.replace("urn:ogc:def:crs:", "").replace("::", ":")
    if crs_name.upper() in ("EPSG:4326", "OGC:1.3:CRS84", "CRS84"):
        crs_name = WGS84
    geom = shape({"type": g["type"], "coordinates": g["coordinates"]})
    if crs_name != WGS84:
        geom = reproject(geom, crs_name, WGS84)
    if not geom.is_valid:
        geom = make_valid(geom)
    return geom


def feature_options(feature: dict) -> dict:
    return ((feature.get("properties") or {}).get("options")) or {}


def feature_uid(feature: dict) -> str:
    props = feature.get("properties") or {}
    return f"{props.get('category')}:{feature.get('id')}"


def dedupe(features: Iterable[dict]) -> list[dict]:
    seen, out = set(), []
    for f in features:
        uid = feature_uid(f)
        if uid in seen:
            continue
        seen.add(uid)
        out.append(f)
    return out


def union_of(geoms: Iterable[Optional[BaseGeometry]]) -> BaseGeometry:
    gs = [g for g in geoms if g is not None and not g.is_empty]
    return unary_union(gs) if gs else box(0, 0, 0, 0).buffer(0)


# ---------------------------------------------------------------- гектары
def hectare_cells(
    free_area_4326: BaseGeometry,
    *,
    side_m: float = 100.0,
    step_m: Optional[float] = None,
    inset_m: float = 5.0,
    max_cells: int = 500,
) -> list[BaseGeometry]:
    """Квадраты side_m×side_m, целиком помещающиеся в свободную территорию.

    inset_m — отступ от чужих границ (погрешность координат, чтобы участок
    при постановке на учёт не «налез» на соседа).
    """
    if free_area_4326.is_empty:
        return []
    metric, crs = to_metric(free_area_4326)
    safe = metric.buffer(-inset_m) if inset_m else metric
    if safe.is_empty:
        return []
    step = step_m or side_m / 4  # шаг сканирования по X: мельче стороны → плотнее укладка
    xmin, ymin, xmax, ymax = safe.bounds
    cells: list[BaseGeometry] = []
    y = ymin
    while y + side_m <= ymax:
        x = xmin
        while x + side_m <= xmax:
            cell = box(x, y, x + side_m, y + side_m)
            if safe.contains(cell):
                cells.append(cell)
                if len(cells) >= max_cells:
                    return [reproject(c, crs, WGS84) for c in cells]
                x += side_m  # не перекрываем соседние квадраты
                continue
            x += step
        y += side_m  # ряды не перекрываются
    return [reproject(c, crs, WGS84) for c in cells]


def point_wgs(lon: float, lat: float) -> Point:
    return Point(lon, lat)
