"""Сохранение результатов: GeoJSON (всегда), GeoPackage и HTML-карта (если установлены extras)."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from shapely.geometry import mapping

from .analysis import ScanResult, zone_title
from .geo import feature_geometry

log = logging.getLogger(__name__)


def _fc(features: Iterable[dict]) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


def _clean(v: Any) -> Any:
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def _records_fc(records: list[dict]) -> dict:
    return _fc(
        {"type": "Feature", "geometry": mapping(r["geometry"]), "properties": {k: _clean(v) for k, v in r.items() if k != "geometry"}}
        for r in records
        if r.get("geometry") is not None
    )


def _nspd_fc(features: list[dict], layer: str) -> dict:
    out = []
    for f in features:
        g = feature_geometry(f)
        if g is None:
            continue
        props = f.get("properties") or {}
        flat = {"layer": layer, "title": zone_title(f), "nspd_id": f.get("id")}
        flat.update({k: _clean(v) for k, v in (props.get("options") or {}).items()})
        if props.get("_permitted_uses"):
            flat["permitted_uses"] = "; ".join(props["_permitted_uses"])
        out.append({"type": "Feature", "geometry": mapping(g), "properties": flat})
    return _fc(out)


def save_scan(res: ScanResult, out_dir: str | Path, *, gpkg: bool = True, html: bool = True) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = Path(out_dir) / f"scan_{stamp}"
    d.mkdir(parents=True, exist_ok=True)

    layers: dict[str, dict] = {
        "candidates": _fc(
            {"type": "Feature", "geometry": mapping(c.geometry), "properties": {**c.as_dict(), "zones": "; ".join(c.zones), "flags": "; ".join(c.flags), "zone_permitted_uses": "; ".join(c.zone_permitted_uses)}}
            for c in res.candidates
        ),
        "free_area": _fc([{"type": "Feature", "geometry": mapping(res.free_area), "properties": {}}] if not res.free_area.is_empty else []),
        "parcels": _records_fc(res.parcels),
        "free_parcels_filtered": _records_fc(res.free_parcels),
        "terr_zones": _nspd_fc(res.zones, "terr_zones"),
    }
    for k, fs in res.exclusions.items():
        layers[f"excl_{k}"] = _nspd_fc(fs, k)
    for k, fs in res.flags.items():
        layers[f"flag_{k}"] = _nspd_fc(fs, k)

    for name, fc in layers.items():
        (d / f"{name}.geojson").write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    (d / "summary.json").write_text(json.dumps(res.summary(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if gpkg:
        try:
            import geopandas as gpd

            path = d / "scan.gpkg"
            for name, fc in layers.items():
                if fc["features"]:
                    gpd.GeoDataFrame.from_features(fc["features"], crs=4326).to_file(path, layer=name, driver="GPKG")
        except ImportError:
            log.info("geopandas не установлен — GeoPackage пропущен (pip install .[geo])")
    if html:
        try:
            write_map(res, layers, d / "map.html")
        except ImportError:
            log.info("folium не установлен — HTML-карта пропущена (pip install .[geo])")
    return d


def write_map(res: ScanResult, layers: dict[str, dict], path: Path) -> None:
    import folium

    x1, y1, x2, y2 = res.bbox
    m = folium.Map(location=[(y1 + y2) / 2, (x1 + x2) / 2], zoom_start=14, tiles=None)
    folium.TileLayer("OpenStreetMap", name="OSM").add_to(m)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Спутник",
    ).add_to(m)
    styles = {
        "parcels": {"color": "#666", "weight": 1, "fillOpacity": 0.05},
        "free_area": {"color": "#2e7d32", "weight": 0, "fillOpacity": 0.25},
        "candidates": {"color": "#1565c0", "weight": 2, "fillOpacity": 0.35},
        "terr_zones": {"color": "#8e24aa", "weight": 1, "fillOpacity": 0.0, "dashArray": "4"},
        "free_parcels_filtered": {"color": "#f9a825", "weight": 2, "fillOpacity": 0.3},
    }
    for name, fc in layers.items():
        if not fc["features"]:
            continue
        st = styles.get(name) or ({"color": "#c62828", "weight": 1, "fillOpacity": 0.2} if name.startswith("excl_") else {"color": "#ef6c00", "weight": 1, "fillOpacity": 0.1})
        fields = [k for k in list(fc["features"][0]["properties"].keys())[:6]]
        folium.GeoJson(
            fc,
            name=name,
            style_function=lambda _f, st=st: st,
            tooltip=folium.GeoJsonTooltip(fields=fields) if fields else None,
            show=name not in ("parcels",),
        ).add_to(m)
    folium.Rectangle([[y1, x1], [y2, x2]], color="#000", weight=1, fill=False, name="AOI").add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(str(path))
