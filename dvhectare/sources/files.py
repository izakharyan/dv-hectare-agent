"""Локальный файл как источник слоя: GeoJSON (без зависимостей) или KML/GPKG/SHP (через geopandas).

Удобно для слоёв, у которых нет API: например, экспорт серых зон с надальнийвосток.рф,
выгрузка ПЗЗ муниципалитета, собственная разметка в QGIS.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from shapely.geometry import box, mapping

from ..geo import BBox, feature_geometry


@dataclass
class FileSource:
    name: str
    path: str
    _cache: list[dict] | None = field(default=None, init=False, repr=False)

    def _load(self) -> list[dict]:
        if self._cache is not None:
            return self._cache
        p = Path(self.path)
        if p.suffix.lower() in (".geojson", ".json"):
            feats = json.loads(p.read_text(encoding="utf-8")).get("features") or []
        else:
            import geopandas as gpd  # опциональная зависимость

            gdf = gpd.read_file(p).to_crs(4326)
            feats = json.loads(gdf.to_json()).get("features") or []
        out = []
        for f in feats:
            g = feature_geometry(f)
            if g is not None:
                out.append({"type": "Feature", "geometry": mapping(g), "properties": {**(f.get("properties") or {}), "_source": self.name}})
        self._cache = out
        return out

    def fetch(self, bbox: BBox) -> list[dict]:
        area = box(*bbox)
        return [f for f in self._load() if feature_geometry(f).intersects(area)]
