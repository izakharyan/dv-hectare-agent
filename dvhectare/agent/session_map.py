"""Карта сессии агента: собирает всё, что агент нашёл, и в конце рисует одну карту.

Каждый инструмент (get_parcel, what_is_here, zones_in_area, scan_for_hectares)
складывает сюда свои геометрии. После ответа модели `save()` пишет
out/agent_<дата>/map.html + answer.md.
"""
from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from shapely.geometry import mapping

from ..analysis import ScanResult, parcel_record, zone_title
from ..export import _nspd_fc, layer_kind, render_map, scan_layers
from ..geo import BBox

log = logging.getLogger(__name__)


@dataclass
class SessionMap:
    layers: list[tuple[str, str, dict]] = field(default_factory=list)
    markers: list[tuple[float, float, str]] = field(default_factory=list)
    rectangles: list[tuple[BBox, str]] = field(default_factory=list)
    scans: int = 0

    @property
    def empty(self) -> bool:
        return not (self.markers or self.rectangles or any(fc["features"] for _, _, fc in self.layers))

    # ------------------------------------------------------------ наполнение
    def add_parcel(self, feature: dict) -> None:
        r = parcel_record(feature)
        g = r.pop("geometry")
        if g is None:
            return
        props = {k: ("" if v is None else str(v)) for k, v in r.items()}
        self._add("Запрошенные участки", "parcel_focus", {"type": "Feature", "geometry": mapping(g), "properties": props})
        c = g.representative_point()
        self.markers.append((c.y, c.x, f"<b>{html.escape(r.get('cad_num') or '')}</b><br>{html.escape(r.get('permitted_use') or '')}<br>{r.get('area_m2') or ''} м²"))

    def add_point(self, lat: float, lon: float, found: dict[str, list], raw: dict[str, list[dict]]) -> None:
        lines = [f"<b>Точка {lat:.5f}, {lon:.5f}</b>"]
        for layer, items in found.items():
            if items:
                titles = [i.get("cad_num") if isinstance(i, dict) else str(i) for i in items]
                lines.append(f"{html.escape(layer)}: {html.escape('; '.join(t for t in titles if t))}")
        self.markers.append((lat, lon, "<br>".join(lines)))
        for layer, feats in raw.items():
            for f in _nspd_fc(feats, layer)["features"]:
                self._add("Объекты в точках", "point_objects", f)

    def add_zones(self, bbox: BBox, zones: list[dict]) -> None:
        for f in _nspd_fc(zones, "terr_zones")["features"]:
            self._add("Территориальные зоны", "terr_zones", f)
        self.rectangles.append((bbox, "Область поиска зон"))

    def add_scan(self, res: ScanResult) -> None:
        self.scans += 1
        prefix = f"Скан {self.scans}: " if self.scans > 1 else ""
        names = {
            "candidates": "Кандидаты 1 га",
            "free_area": "Свободная территория",
            "parcels": "Участки ЕГРН",
            "free_parcels_filtered": "Свободные от прав (фильтр)",
            "terr_zones": "Территориальные зоны",
        }
        for name, fc in scan_layers(res).items():
            label = names.get(name) or name.replace("excl_", "Исключено: ").replace("flag_", "Проверить: ")
            self.layers.append((prefix + label, layer_kind(name), fc))
        self.rectangles.append((res.bbox, f"{prefix}область скана"))
        for c in res.candidates[:10]:
            self.markers.append((c.lat, c.lon, f"<b>Кандидат #{c.id}</b><br>{html.escape('; '.join(c.zones) or 'зона не определена')}<br>{html.escape('; '.join(c.flags) or 'без флагов')}"))

    def _add(self, name: str, kind: str, feature: dict) -> None:
        for n, _, fc in self.layers:
            if n == name:
                fc["features"].append(feature)
                return
        self.layers.append((name, kind, {"type": "FeatureCollection", "features": [feature]}))

    # ------------------------------------------------------------ вывод
    def save(self, out_dir: str | Path, question: str, answer: str) -> Path | None:
        """Сохраняет карту и ответ. None — если на карте нечего показать или нет folium."""
        if self.empty:
            return None
        d = Path(out_dir) / f"agent_{datetime.now():%Y%m%d_%H%M%S}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "answer.md").write_text(f"# Запрос\n\n{question}\n\n# Ответ агента\n\n{answer}\n", encoding="utf-8")
        note = (
            '<div style="position:fixed;bottom:12px;left:12px;z-index:9999;max-width:420px;max-height:45vh;'
            "overflow:auto;background:rgba(255,255,255,.95);padding:10px 12px;border-radius:8px;"
            'box-shadow:0 2px 8px rgba(0,0,0,.3);font:13px/1.4 sans-serif;white-space:pre-wrap">'
            f"<b>{html.escape(question)}</b>\n\n{html.escape(answer)}</div>"
        )
        try:
            return render_map(self.layers, d / "map.html", markers=self.markers, rectangles=self.rectangles, note_html=note)
        except ImportError:
            log.warning("folium не установлен — карта не построена (pip install -e .[geo])")
            return None
