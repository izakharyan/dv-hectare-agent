"""Функции-инструменты для LLM-агента. Каждая возвращает JSON-сериализуемый dict."""
from __future__ import annotations

from typing import Any, Callable

from ..analysis import Scanner, parcel_record, zone_title
from ..export import save_scan
from ..geo import bbox_around, feature_geometry
from ..layers import LAYERS, get_layer

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_parcel",
        "description": "Сведения о земельном участке из ЕГРН (НСПД) по кадастровому номеру: категория, ВРИ, площадь, форма собственности, статус, центр.",
        "input_schema": {
            "type": "object",
            "properties": {"cad_number": {"type": "string", "description": "Например 25:28:010013:31"}},
            "required": ["cad_number"],
        },
    },
    {
        "name": "what_is_here",
        "description": "Что находится в точке: участки ЕГРН, территориальная зона, ЗОУИТ, ООПТ, населённый пункт и т.д.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "layers": {"type": "array", "items": {"type": "string", "enum": list(LAYERS)}, "description": "По умолчанию parcels, terr_zones, zouit, oopt, settlements, forestry"},
            },
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "zones_in_area",
        "description": "Территориальные зоны ПЗЗ в радиусе от точки, с перечнем видов разрешённого использования.",
        "input_schema": {
            "type": "object",
            "properties": {"lat": {"type": "number"}, "lon": {"type": "number"}, "radius_km": {"type": "number", "default": 1}},
            "required": ["lat", "lon"],
        },
    },
    {
        "name": "scan_for_hectares",
        "description": (
            "Полный скан области вокруг точки: вычитает все участки ЕГРН и исключающие зоны, нарезает свободную "
            "территорию на кандидатов 1 га, помечает ЗОУИТ/лес/НП, ищет участки из слоя «свободные от прав». "
            "Дорого: радиус держи ≤ 2 км. Сохраняет GeoJSON/GPKG/HTML-карту."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat": {"type": "number"},
                "lon": {"type": "number"},
                "radius_km": {"type": "number", "default": 1},
                "allowed_zone_patterns": {"type": "array", "items": {"type": "string"}, "description": "Регулярки по названию зоны, напр. ['СХ', 'Сельскохоз']"},
            },
            "required": ["lat", "lon"],
        },
    },
]


def make_handlers(scanner: Scanner, out_dir: str) -> dict[str, Callable[..., Any]]:
    client = scanner.client

    def get_parcel(cad_number: str) -> dict:
        f = client.find_parcel(cad_number)
        if not f:
            return {"found": False, "cad_number": cad_number}
        r = parcel_record(f)
        g = r.pop("geometry")
        if g is not None:
            c = g.centroid
            r["center"] = {"lat": round(c.y, 6), "lon": round(c.x, 6)}
        return {"found": True, **r}

    def what_is_here(lat: float, lon: float, layers: list[str] | None = None) -> dict:
        keys = layers or ["parcels", "terr_zones", "zouit", "oopt", "settlements", "forestry"]
        out: dict[str, Any] = {}
        for k in keys:
            feats = client.features_at_point(lon, lat, get_layer(k).layer_id)
            if k == "parcels":
                out[k] = [{kk: vv for kk, vv in parcel_record(f).items() if kk != "geometry"} for f in feats]
            else:
                out[k] = [zone_title(f) for f in feats]
        return out

    def zones_in_area(lat: float, lon: float, radius_km: float = 1) -> dict:
        zs = scanner.zones(bbox_around(lat, lon, radius_km))
        return {
            "zones": [
                {"title": f["properties"]["_title"], "permitted_uses": (f["properties"].get("_permitted_uses") or [])[:20]}
                for f in zs
                if feature_geometry(f) is not None
            ][:40]
        }

    def scan_for_hectares(lat: float, lon: float, radius_km: float = 1, allowed_zone_patterns: list[str] | None = None) -> dict:
        if allowed_zone_patterns is not None:
            scanner.s.scan.allowed_zone_patterns = allowed_zone_patterns
        res = scanner.scan(bbox_around(lat, lon, radius_km))
        path = save_scan(res, out_dir)
        return {"saved_to": str(path), **res.summary()}

    return {
        "get_parcel": get_parcel,
        "what_is_here": what_is_here,
        "zones_in_area": zones_in_area,
        "scan_for_hectares": scan_for_hectares,
    }
