"""Офлайн-тесты на фейковом НСПД: `pytest -q`."""
from __future__ import annotations

import json

import httpx
import pytest
from shapely.geometry import box

from dvhectare.analysis import Scanner
from dvhectare.config import Settings
from dvhectare.export import save_scan
from dvhectare.geo import area_m2, feature_geometry, hectare_cells, parse_bbox
from dvhectare.nspd.client import NspdClient

from .fake_nspd import AOI, CENTER, FakeNspd


@pytest.fixture
def fake():
    return FakeNspd()


@pytest.fixture
def client(fake):
    c = NspdClient(min_delay=0, cache_path=None, transport=httpx.MockTransport(fake.handler))
    yield c
    c.close()


def test_geometry_3857_to_4326(fake):
    g = feature_geometry(fake.features[0])
    x1, y1, *_ = g.bounds
    assert abs(x1 - AOI[0]) < 1e-6 and abs(y1 - AOI[1]) < 1e-6


def test_find_parcel(client):
    f = client.find_parcel("25:27:000000:2")
    assert f and f["properties"]["options"]["specified_area"] == 5000
    assert client.find_parcel("25:27:000000:999") is None


def test_quadtree_split_on_too_big(client, fake):
    feats = list(client.iter_bbox(AOI, 36368))
    assert len(fake.calls) > 1, "большой bbox должен был раздробиться"
    assert {f["id"] for f in feats} == {1, 2}


def test_point_lookup(client):
    lat, lon = CENTER
    feats = client.features_at_point(lon, lat, 875838)  # terr_zones
    assert feats and "СХ-1" in feats[0]["properties"]["label"]


def test_hectare_grid():
    cells = hectare_cells(box(*parse_bbox("132.18,43.35,132.19,43.36")), inset_m=0, max_cells=1000)
    assert len(cells) > 50
    assert all(abs(area_m2(c) - 10_000) < 50 for c in cells[:5])
    # квадраты не перекрываются
    assert cells[0].intersection(cells[1]).area < 1e-12


def test_full_scan(client, tmp_path):
    s = Settings()
    s.scan.hectare.max_cells = 200
    res = Scanner(client, s, extra_sources=[]).scan(AOI)

    assert res.stats["parcels_in_egrn"] == 2
    assert res.stats["free_parcels_matching_filter"] == 1
    assert res.candidates, "должны найтись кандидаты"
    occupied = feature_geometry(next(f for f in FakeNspd().features if f["id"] == 1))
    oopt = feature_geometry(next(f for f in FakeNspd().features if f["id"] == 4))
    for c in res.candidates:
        assert not c.geometry.intersects(occupied.buffer(-1e-7))
        assert not c.geometry.intersects(oopt.buffer(-1e-7))
    # чистые кандидаты раньше помеченных
    flags = [len(c.flags) for c in res.candidates]
    assert flags == sorted(flags)
    assert any("СХ-1" in z for c in res.candidates for z in c.zones)
    assert any("Растениеводство" in u for c in res.candidates for u in c.zone_permitted_uses)

    out = save_scan(res, tmp_path, gpkg=False, html=True)
    assert (out / "candidates.geojson").exists()
    try:
        import folium  # noqa: F401

        assert (out / "map.html").exists()
    except ImportError:
        pass
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["hectare_candidates"] == len(res.candidates)


def test_zone_filter(client):
    s = Settings()
    s.scan.allowed_zone_patterns = ["^Р-1"]
    res = Scanner(client, s, extra_sources=[]).scan(AOI)
    assert res.candidates and all(any(z.startswith("Р-1") for z in c.zones) for c in res.candidates)


def test_area_limit(client):
    s = Settings()
    s.scan.max_area_km2 = 0.1
    with pytest.raises(ValueError):
        Scanner(client, s, extra_sources=[]).scan(AOI)


def test_agent_tool_handlers(client, tmp_path):
    from dvhectare.agent.tools import TOOL_SCHEMAS, make_handlers

    h = make_handlers(Scanner(client, Settings(), extra_sources=[]), str(tmp_path))
    assert set(h) == {t["name"] for t in TOOL_SCHEMAS}
    p = h["get_parcel"]("25:27:000000:2")
    assert p["found"] and p["center"]["lat"] > 43
    here = h["what_is_here"](*CENTER, layers=["terr_zones", "zouit"])
    assert here["terr_zones"]
    json.dumps(h["zones_in_area"](*CENTER, radius_km=0.5), ensure_ascii=False)


def test_gemini_agent_loop(client, tmp_path):
    pytest.importorskip("google.genai")
    from google.genai import types

    from dvhectare.agent import run_agent

    class FakeModels:
        def __init__(self):
            self.calls = []

        def generate_content(self, model, contents, config):
            self.calls.append(contents)
            if len(self.calls) == 1:
                part = types.Part(function_call=types.FunctionCall(name="get_parcel", args={"cad_number": "25:27:000000:2"}))
            else:
                last = contents[-1].parts[0].function_response
                assert last.name == "get_parcel" and last.response["found"] is True
                part = types.Part.from_text(text="Участок найден, 5000 м².")
            return types.GenerateContentResponse(
                candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))]
            )

    class FakeClient:
        models = FakeModels()

    out = run_agent("Что за участок 25:27:000000:2?", Scanner(client, Settings(), extra_sources=[]), str(tmp_path), client=FakeClient())
    assert "5000" in out
    # декларации инструментов собираются без ошибок
    from dvhectare.agent.gemini_agent import _function_declarations

    assert len(_function_declarations(types)) == 4
