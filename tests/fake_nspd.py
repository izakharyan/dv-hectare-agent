"""Фейковый сервер НСПД для офлайн-тестов (httpx.MockTransport).

Имитирует: ответы в EPSG:3857 с полем crs, отказ на больших контурах (код 400104),
поиск по КН, GetFeatureInfo и вкладку ВРИ.
"""
from __future__ import annotations

import json

import httpx
from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely.ops import transform

from dvhectare.geo import bbox_around
from dvhectare.layers import LAYERS

TO_3857 = Transformer.from_crs(4326, 3857, always_xy=True).transform

CENTER = (43.35, 132.18)  # lat, lon — окрестности Артёма
AOI = bbox_around(*CENTER, 0.6)  # ~1.2×1.2 км


def _f(fid, category, geom4326, options, label=None):
    g = mapping(transform(TO_3857, geom4326))
    g = {"type": g["type"], "coordinates": json.loads(json.dumps(g["coordinates"])), "crs": {"type": "name", "properties": {"name": "EPSG:3857"}}}
    return {"type": "Feature", "id": fid, "geometry": g, "properties": {"category": category, "label": label, "options": options}}


def world():
    x1, y1, x2, y2 = AOI
    w, h = x2 - x1, y2 - y1
    cat = {k: v.category_id for k, v in LAYERS.items()}
    return [
        # занятые участки ЕГРН — левая треть
        _f(1, cat["parcels"], box(x1, y1, x1 + w * 0.33, y2), {"cad_num": "25:27:000000:1", "permitted_use_established_by_document": "Для ведения личного подсобного хозяйства", "specified_area": 400000, "status": "Учтенный", "ownership_type": "Частная"}),
        # «свободный от прав» участок (он же есть в ЕГРН)
        _f(2, cat["parcels"], box(x1 + w * 0.40, y1, x1 + w * 0.45, y1 + h * 0.05), {"cad_num": "25:27:000000:2", "permitted_use_established_by_document": "Для сельскохозяйственного производства", "specified_area": 5000, "status": "Учтенный"}),
        _f(3, cat["free_parcels"], box(x1 + w * 0.40, y1, x1 + w * 0.45, y1 + h * 0.05), {"cad_num": "25:27:000000:2", "permitted_use_established_by_document": "Для сельскохозяйственного производства", "specified_area": 5000, "status": "Учтенный"}),
        # ООПТ — верхняя правая четверть (исключение)
        _f(4, cat["oopt"], box(x1 + w * 0.66, y1 + h * 0.5, x2, y2), {"reg_numb_border": "25:00-6.1"}, label="Заказник"),
        # ЗОУИТ — полоса по центру (флаг)
        _f(5, cat["zouit"], box(x1 + w * 0.5, y1, x1 + w * 0.55, y2), {"reg_numb_border": "25:27-6.99"}, label="Охранная зона ЛЭП"),
        # территориальные зоны
        _f(6, cat["terr_zones"], box(x1, y1, x1 + w * 0.6, y2), {"reg_numb_border": "25:27-7.1"}, label="СХ-1 Зона сельскохозяйственного использования"),
        _f(7, cat["terr_zones"], box(x1 + w * 0.6, y1, x2, y2), {"reg_numb_border": "25:27-7.2"}, label="Р-1 Рекреационная зона"),
    ]


class FakeNspd:
    def __init__(self, max_area_deg2: float = 1e-4):
        self.features = world()
        self.max_area = max_area_deg2
        self.calls: list[str] = []

    def _geom4326(self, f):
        from dvhectare.geo import feature_geometry

        return feature_geometry(f)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path.endswith("/intersects"):
            body = json.loads(request.content)
            poly = shape(body["geom"]["features"][0]["geometry"])
            if poly.area > self.max_area:
                return httpx.Response(500, text='{"code":400104,"message":"too big"}')
            cats = {c["id"] for c in body["categories"]}
            feats = [f for f in self.features if f["properties"]["category"] in cats and self._geom4326(f).intersects(poly)]
            return httpx.Response(200, json={"type": "FeatureCollection", "features": feats})
        if path.endswith("/search/geoportal"):
            q = request.url.params["query"]
            feats = [f for f in self.features if f["properties"]["options"].get("cad_num") == q][:1]
            if not feats:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={"data": {"type": "FeatureCollection", "features": feats}})
        if "/wms" in path:
            layer_id = int(path.split("/")[-2])
            key = next(k for k, v in LAYERS.items() if v.layer_id == layer_id)
            x1, y1, x2, y2 = map(float, request.url.params["BBOX"].split(","))
            pt = box(x1, y1, x2, y2).centroid
            feats = [f for f in self.features if f["properties"]["category"] == LAYERS[key].category_id and self._geom4326(f).contains(pt)]
            return httpx.Response(200, json={"type": "FeatureCollection", "features": feats})
        if path.endswith("/tab-values-data"):
            gid = int(request.url.params["geomId"])
            uses = {6: ["Растениеводство (1.1)", "Животноводство (1.7)", "Ведение личного подсобного хозяйства на полевых участках (1.16)"], 7: ["Отдых (рекреация) (5.0)"]}
            return httpx.Response(200, json={"title": "ВРИ", "value": uses.get(gid, [""])})
        return httpx.Response(404, json={})
