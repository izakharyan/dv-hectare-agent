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


def scan_layers(res: ScanResult) -> dict[str, dict]:
    """Все слои скана как FeatureCollection (для GeoJSON/GPKG/карты)."""
    layers: dict[str, dict] = {
        "candidates": _fc(
            {"type": "Feature", "geometry": mapping(c.geometry), "properties": {**c.as_dict(), "zones": "; ".join(c.zones), "flags": "; ".join(c.flags), "zone_permitted_uses": "; ".join(c.zone_permitted_uses),
                                                                                 "corners": "; ".join(f"{la}, {lo}" for la, lo in c.corners())}}
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
    return layers


def layer_kind(name: str) -> str:
    """Тип слоя → стиль на карте."""
    if name.startswith("excl_"):
        return "exclusion"
    if name.startswith("flag_"):
        return "flag"
    return name


def save_scan(res: ScanResult, out_dir: str | Path, *, gpkg: bool = True, html: bool = True) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    d = Path(out_dir) / f"scan_{stamp}"
    d.mkdir(parents=True, exist_ok=True)
    layers = scan_layers(res)

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
    render_map([(n, layer_kind(n), fc) for n, fc in layers.items()], path, bbox=res.bbox)


STYLES: dict[str, dict] = {
    "parcels": {"color": "#666", "weight": 1, "fillOpacity": 0.05},
    "free_area": {"color": "#2e7d32", "weight": 0, "fillOpacity": 0.25},
    "candidates": {"color": "#1565c0", "weight": 2, "fillOpacity": 0.35},
    "terr_zones": {"color": "#8e24aa", "weight": 1, "fillOpacity": 0.0, "dashArray": "4"},
    "free_parcels_filtered": {"color": "#f9a825", "weight": 2, "fillOpacity": 0.3},
    "exclusion": {"color": "#c62828", "weight": 1, "fillOpacity": 0.2},
    "flag": {"color": "#ef6c00", "weight": 1, "fillOpacity": 0.1},
    "parcel_focus": {"color": "#d81b60", "weight": 3, "fillOpacity": 0.2},
    "point_objects": {"color": "#00838f", "weight": 2, "fillOpacity": 0.1},
}


POPUP_JS = r"""
<style>
  .dv-pop { font: 13px/1.4 "Segoe UI", system-ui, sans-serif; min-width: 230px; }
  .dv-pop h4 { margin: 0 0 6px; font-size: 14px; }
  .dv-pop .tag { display: inline-block; font-size: 11px; padding: 1px 6px; border-radius: 8px; background: #e8f5e9; color: #2e7d32; margin-bottom: 6px; }
  .dv-pop .tag.muted { background: #eef1f5; color: #4b5563; }
  .dv-pop .cn { display: flex; align-items: center; gap: 6px; margin: 2px 0 6px; }
  .dv-pop .cn code { font: 600 14px Consolas, monospace; }
  .dv-pop button { font: 12px "Segoe UI", sans-serif; border: 1px solid #c5cbd3; background: #fff; border-radius: 5px; padding: 2px 8px; cursor: pointer; }
  .dv-pop button:hover { background: #eef1f5; }
  .dv-pop button.ok { border-color: #2e7d32; color: #2e7d32; }
  .dv-pop table { border-collapse: collapse; margin-top: 4px; }
  .dv-pop td { padding: 1px 6px 1px 0; vertical-align: top; }
  .dv-pop td:first-child { color: #6b7280; white-space: nowrap; }
  .dv-pop a { color: #1565c0; }
</style>
<script>
(function () {
  var LABELS = {
    permitted_use: "ВРИ", category: "Категория", area_m2: "Площадь, м²", ownership: "Собственность",
    status: "Статус", address: "Адрес", cost_rub: "Кад. стоимость, ₽", zones: "Терзона",
    flags: "Проверить", zone_permitted_uses: "ВРИ зоны", permitted_uses: "ВРИ зоны", title: "Название"
  };
  function esc(v) { return String(v).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }

  window.dvCopy = function (btn) {
    var text = btn.getAttribute("data-copy");
    function done() { var t = btn.textContent; btn.textContent = "Скопировано ✓"; btn.classList.add("ok");
                      setTimeout(function () { btn.textContent = t; btn.classList.remove("ok"); }, 1500); }
    function legacy() {
      var ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); done(); } catch (e) { prompt("Скопируйте вручную:", text); }
      document.body.removeChild(ta);
    }
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done, legacy);
    else legacy();
  };

  function copyRow(label, value) {
    return '<div class="cn"><span>' + label + '</span><code>' + esc(value) + '</code>' +
           '<button data-copy="' + esc(value) + '" onclick="dvCopy(this)">Копировать</button></div>';
  }
  function table(p, keys) {
    var rows = keys.filter(function (k) { return p[k] !== undefined && p[k] !== null && p[k] !== "" && p[k] !== "None"; })
      .map(function (k) { var v = String(p[k]); if (v.length > 300) v = v.slice(0, 300) + "…";
                          return "<tr><td>" + (LABELS[k] || k) + "</td><td>" + esc(v) + "</td></tr>"; });
    return rows.length ? "<table>" + rows.join("") + "</table>" : "";
  }

  window.dvBind = function (f, layer, kind, layerName) {
    var p = f.properties || {}, html, tip;
    if (kind === "candidates") {
      tip = "Кандидат #" + p.id;
      html = "<h4>Кандидат #" + esc(p.id) + " · 1 га</h4>" +
             '<span class="tag">Свободная территория — участка в ЕГРН ещё нет</span>' +
             (p.quarter ? copyRow("Квартал:", p.quarter) : "") +
             copyRow("Центр:", p.lat + ", " + p.lon) +
             '<div class="cn"><span>Углы:</span><button data-copy="' + esc(p.corners || "") + '" onclick="dvCopy(this)">Копировать координаты углов</button></div>' +
             table(p, ["zones", "flags", "zone_permitted_uses"]) +
             (p.nspd_link ? '<div><a href="' + esc(p.nspd_link) + '" target="_blank">Открыть на карте НСПД</a></div>' : "");
    } else if (p.cad_num) {
      var free = kind === "free_parcels_filtered";
      tip = p.cad_num;
      html = "<h4>Земельный участок</h4>" +
             '<span class="tag' + (free ? "" : " muted") + '">' + (free ? "Свободен от прав третьих лиц" : esc(layerName)) + "</span>" +
             copyRow("КН:", p.cad_num) +
             table(p, ["permitted_use", "category", "area_m2", "ownership", "status", "address", "cost_rub"]);
    } else if (p.title || p.reg_numb_border) {
      tip = p.title || p.reg_numb_border;
      html = "<h4>" + esc(layerName) + "</h4>" +
             (p.reg_numb_border ? copyRow("Реестровый №:", p.reg_numb_border) : "") +
             table(p, ["title", "permitted_uses"]);
    } else {
      return;
    }
    layer.bindTooltip(esc(tip), {sticky: true});
    layer.bindPopup('<div class="dv-pop">' + html + "</div>", {maxWidth: 380});
  };
})();
</script>
"""


def render_map(
    layers: list[tuple[str, str, dict]],
    path: Path,
    *,
    bbox: tuple[float, float, float, float] | None = None,
    markers: list[tuple[float, float, str]] | None = None,
    rectangles: list[tuple[tuple[float, float, float, float], str]] | None = None,
    note_html: str | None = None,
) -> Path:
    """Универсальная HTML-карта. layers: (название в легенде, тип стиля, FeatureCollection)."""
    import folium

    m = folium.Map(location=[43.12, 131.9], zoom_start=12, tiles=None)
    folium.TileLayer("OpenStreetMap", name="OSM").add_to(m)
    folium.TileLayer(
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="Спутник",
    ).add_to(m)

    bounds: list[list[float]] = []
    for name, kind, fc in layers:
        if not fc["features"]:
            continue
        st = STYLES.get(kind) or STYLES["flag"]
        gj = folium.GeoJson(
            fc,
            name=name,
            style_function=lambda _f, st=st: st,
            # подпись при наведении + карточка по клику с кнопками «Копировать» (см. POPUP_JS)
            on_each_feature=folium.JsCode(f"function(f, l) {{ dvBind(f, l, {json.dumps(kind)}, {json.dumps(name)}); }}"),
            show=kind != "parcels",
        ).add_to(m)
        try:
            (sy, sx), (ny, nx) = gj.get_bounds()
            bounds += [[sy, sx], [ny, nx]]
        except Exception:
            pass
    for (x1, y1, x2, y2), label in rectangles or []:
        folium.Rectangle([[y1, x1], [y2, x2]], color="#000", weight=1, fill=False, tooltip=label).add_to(m)
        bounds += [[y1, x1], [y2, x2]]
    if bbox:
        x1, y1, x2, y2 = bbox
        folium.Rectangle([[y1, x1], [y2, x2]], color="#000", weight=1, fill=False, name="AOI").add_to(m)
        bounds += [[y1, x1], [y2, x2]]
    for lat, lon, popup in markers or []:
        folium.Marker([lat, lon], popup=folium.Popup(popup, max_width=400), tooltip=popup.split("<br>")[0]).add_to(m)
        bounds.append([lat, lon])

    if bounds:
        lats = [b[0] for b in bounds]
        lons = [b[1] for b in bounds]
        if max(lats) - min(lats) < 1e-4 and max(lons) - min(lons) < 1e-4:  # одна точка
            m.location, m.options["zoom"] = [lats[0], lons[0]], 17
        else:
            m.fit_bounds([[min(lats), min(lons)], [max(lats), max(lons)]])
    m.get_root().header.add_child(folium.Element(POPUP_JS))
    if note_html:
        m.get_root().html.add_child(folium.Element(note_html))
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(str(path))
    return path
