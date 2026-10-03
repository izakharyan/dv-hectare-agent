"""Офлайн-тесты на фейковом НСПД: `pytest -q`."""
from __future__ import annotations

import json
import re

import httpx
import pytest
from shapely.geometry import box

from dvhectare.analysis import Scanner
from dvhectare.config import Settings
from dvhectare.export import save_scan
from dvhectare.geo import area_m2, feature_geometry, hectare_cells, parse_bbox
from dvhectare.nspd.client import NspdClient

from .fake_nspd import AOI, CENTER, FakeNspd


@pytest.fixture(autouse=True)
def _no_nspd_block():
    """Пауза после 403 — общая на процесс; не даём ей протекать между тестами."""
    from dvhectare.nspd.client import reset_block

    reset_block()
    yield
    reset_block()


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


def test_separate_proxies(monkeypatch, tmp_path):
    """НСПД не подхватывает системный прокси; у Gemini — свой прокси из конфига."""
    from dvhectare.config import load_settings

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "nspd:\n  proxy: null\ngemini:\n  proxy: socks5://127.0.0.1:10808\n  model: gemini-3.5-flash-lite\n",
        encoding="utf-8",
    )
    s = load_settings(cfg)
    assert s.nspd.proxy is None
    assert s.gemini.proxy == "socks5://127.0.0.1:10808" and s.gemini.model == "gemini-3.5-flash-lite"
    assert s.agent.make_map is True and s.agent.open_map is True  # значения по умолчанию
    cfg.write_text("agent:\n  open_map: false\n", encoding="utf-8")
    assert load_settings(cfg).agent.open_map is False

    # системный прокси (например, заведённый ради Gemini) НСПД игнорирует
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    c = NspdClient(min_delay=0, cache_path=None)
    assert c._http._trust_env is False
    assert not any(t for t in c._http._mounts.values() if t is not None)
    c.close()

    pytest.importorskip("google.genai")
    from dvhectare.agent.gemini_agent import make_client

    # ключ из конфига, без переменных окружения
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        make_client()
    cfg.write_text("gemini:\n  api_key: AIza-test\n", encoding="utf-8")
    assert load_settings(cfg).gemini.api_key == "AIza-test"
    g = make_client(proxy=s.gemini.proxy, api_key="AIza-test")
    assert g._api_client.api_key == "AIza-test"
    mounts = g._api_client._httpx_client._mounts
    assert any(t is not None for t in mounts.values()), "у Gemini должен быть свой прокси"


def test_agent_builds_final_map(client, tmp_path):
    """После ответа агента строится одна карта со всем найденным."""
    pytest.importorskip("google.genai")
    pytest.importorskip("folium")
    from google.genai import types

    from dvhectare.agent import run_agent

    script = [
        ("get_parcel", {"cad_number": "25:27:000000:2"}),
        ("what_is_here", {"lat": CENTER[0], "lon": CENTER[1], "layers": ["terr_zones", "zouit"]}),
        ("scan_for_hectares", {"lat": CENTER[0], "lon": CENTER[1], "radius_km": 0.6}),
    ]

    class FakeModels:
        n = 0

        def generate_content(self, model, contents, config):
            i, FakeModels.n = FakeModels.n, FakeModels.n + 1
            if i < len(script):
                name, args = script[i]
                part = types.Part(function_call=types.FunctionCall(name=name, args=args))
            else:
                part = types.Part.from_text(text="Нашёл кандидатов в зоне СХ-1.")
            return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])

    class FakeClient:
        models = FakeModels()

    out = run_agent("Найди гектар", Scanner(client, Settings(), extra_sources=[]), str(tmp_path), client=FakeClient())
    assert "Карта:" in out
    maps = list(tmp_path.glob("agent_*/map.html"))
    assert len(maps) == 1
    html = maps[0].read_text(encoding="utf-8")
    for label in ("Запрошенные участки", "Объекты в точках", "Кандидаты 1 га", "Нашёл кандидатов"):
        assert label in html or json.dumps(label)[1:-1] in html, label  # folium экранирует кириллицу в JS
    assert (maps[0].parent / "answer.md").exists()

    # без находок карта не строится
    class Silent:
        class models:
            @staticmethod
            def generate_content(model, contents, config):
                return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part.from_text(text="Привет")]))])

    assert "Карта:" not in run_agent("Привет", Scanner(client, Settings(), extra_sources=[]), str(tmp_path / "x"), client=Silent())


def test_gui_run_search(monkeypatch, tmp_path, fake):
    """Логика окна: config.yaml рядом с программой → агент → ответ + путь к карте."""
    pytest.importorskip("google.genai")
    pytest.importorskip("folium")
    from google.genai import types

    import dvhectare.agent.gemini_agent as ga
    import dvhectare.cli as cli
    from dvhectare import gui

    (tmp_path / "config.yaml").write_text("agent:\n  open_map: false\nnspd:\n  cache_path: null\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_client", lambda s: NspdClient(min_delay=0, cache_path=None, transport=httpx.MockTransport(fake.handler)))

    calls = []

    class FakeModels:
        def generate_content(self, model, contents, config):
            calls.append(model)
            if len(calls) == 1:
                part = types.Part(function_call=types.FunctionCall(name="get_parcel", args={"cad_number": "25:27:000000:2"}))
            else:
                part = types.Part.from_text(text="Готово: участок 5000 м².")
            return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])

    class FakeClient:
        models = FakeModels()

    monkeypatch.setattr(ga, "make_client", lambda **kw: FakeClient())
    answer, path = gui.run_search("gemini-3.5-flash-lite", "Что за участок?", tmp_path, api_key="k", out_dir=tmp_path / "tmpmaps")
    assert answer == "Готово: участок 5000 м²."
    assert path and path.exists() and path.name == "map.html"
    assert path.parent.parent == tmp_path / "tmpmaps"      # карты — в переданной (временной) папке
    assert calls == ["gemini-3.5-flash-lite"] * 2          # модель из окна

    with pytest.raises(FileNotFoundError):
        gui.run_search("x", "y", tmp_path / "нет_такой_папки")


def test_gui_api_and_server(tmp_path):
    """Окно: API для JS (init/search/poll), ключ Gemini, история диалогов, локальный сервер."""
    import logging
    import time
    import urllib.error
    import urllib.request

    import yaml

    from dvhectare import gui

    cfg = tmp_path / "config.yaml"
    cfg.write_text("nspd:\n  timeout: 20\n\ngemini:\n  api_key: null             # ключ\n  model: null\n\nagent:\n  open_map: true\n", encoding="utf-8")
    seen = {}

    def fake_search(model, prompt, root, api_key=None, out_dir=None, conversation=None):
        logging.getLogger("dvhectare.test").info("→ scan_for_hectares {}")
        seen.update(model=model, api_key=api_key, out_dir=out_dir)
        m = out_dir / f"agent_{len(seen)}_{time.time_ns()}" / "map.html"
        m.parent.mkdir(parents=True)
        m.write_text(f"<html>карта: {prompt}</html>", encoding="utf-8")
        return f"ответ на «{prompt}»", m

    api = gui.Api(tmp_path, search_fn=fake_search)
    tmp_maps = api.out_dir
    assert tmp_maps.exists() and tmp_path not in tmp_maps.parents  # системная временная папка
    st = api.init()
    assert st["models"] and st["api_key"] == "" and st["dialogs"] == [] and st["current"]["messages"] == []

    def wait(a):
        t = time.time()
        while a.poll()["busy"] and time.time() - t < 5:
            time.sleep(0.02)
        return a.poll()["events"]

    assert api.search("m", "  ", "k")["ok"] is False
    assert api.search("m", "q", "")["error"] == "Введите ключ Gemini"
    assert api.search("gemini-3.5-flash-lite", "найди гектар у Артёма", "AIza-123")["ok"] is True
    evs = wait(api)
    assert evs[0]["type"] == "log" and "scan_for_hectares" in evs[0]["text"]
    done = evs[-1]
    did = done["dialog"]["id"]
    assert done["type"] == "done" and done["answer"] == "ответ на «найди гектар у Артёма»"
    assert done["map"] == f"/history/{did}/map.html" and done["turns"] == 1
    assert done["dialogs"][0]["title"] == "найди гектар у Артёма"
    assert seen["model"] == "gemini-3.5-flash-lite" and seen["api_key"] == "AIza-123" and seen["out_dir"] == tmp_maps

    # второй запрос в том же диалоге
    api.search("gemini-3.5-flash-lite", "а севернее?", "AIza-123")
    wait(api)

    # ключ записан в config.yaml, остальное и комментарии не тронуты
    text = cfg.read_text(encoding="utf-8")
    assert 'api_key: "AIza-123"             # ключ' in text and "open_map: true" in text
    assert yaml.safe_load(text)["gemini"]["api_key"] == "AIza-123"

    # --- «перезапуск программы»: новый Api на той же папке открывает последний диалог
    api.cleanup()
    assert not tmp_maps.exists()
    api2 = gui.Api(tmp_path, search_fn=fake_search)
    st = api2.init()
    cur = st["current"]
    assert st["api_key"] == "AIza-123" and st["model"] == "gemini-3.5-flash-lite"
    assert cur["id"] == did and cur["turns"] == 2 and cur["map"] == f"/history/{did}/map.html"
    assert [m["role"] for m in cur["messages"]] == ["user", "bot", "user", "bot"]
    assert cur["messages"][1]["steps"] and "севернее" in cur["messages"][3]["text"]
    assert [d["id"] for d in st["dialogs"]] == [did]

    # новый диалог, затем возврат к старому
    assert api2.new_dialog()["current"]["messages"] == []
    api2.search("m", "второй диалог", "AIza-123")
    wait(api2)
    assert len(api2.list_dialogs()) == 2
    assert api2.open_dialog(did)["turns"] == 2

    def boom(*a, **k):
        raise SystemExit("Нет ключа Gemini")

    api3 = gui.Api(tmp_path, search_fn=boom)
    api3.new_dialog()
    api3.search("m", "q", "k")
    ev = wait(api3)[-1]
    assert ev["type"] == "error" and ev["text"] == "Нет ключа Gemini"
    assert api3.dialog.messages[-1] == {"role": "bot", "error": "Нет ключа Gemini"}  # ошибка тоже в истории
    api3.cleanup()

    srv, port = gui.start_server(api2.out_dir, api2.store.root)
    try:
        base = f"http://127.0.0.1:{port}"
        assert "Искать" in urllib.request.urlopen(base + "/index.html", timeout=5).read().decode("utf-8")
        assert "севернее" in urllib.request.urlopen(base + f"/history/{did}/map.html", timeout=5).read().decode("utf-8")
        for bad in ("/out/../config.yaml", "/out/%2e%2e/config.yaml", "/history/../config.yaml", "/history/%2e%2e/config.yaml"):
            with pytest.raises(urllib.error.HTTPError):  # за пределы папок не выйти
                urllib.request.urlopen(base + bad, timeout=5)
    finally:
        srv.shutdown()

    # удаление диалога
    r = api2.delete_dialog(did)
    assert r["ok"] and {d["title"] for d in r["dialogs"]} == {"второй диалог", "q"}  # диалог с ошибкой тоже сохранён
    assert not (tmp_path / "history" / did).exists()
    with pytest.raises(ValueError):
        api2.store.dir("../config")
    api2.cleanup()


def test_save_api_key_variants(tmp_path):
    from dvhectare.gui import save_api_key
    import yaml

    p = tmp_path / "c.yaml"
    save_api_key(p, "A1")                                   # файла нет
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["gemini"]["api_key"] == "A1"
    p.write_text("gemini:\n  model: x\nscan:\n  api_key: other\n", encoding="utf-8")
    save_api_key(p, "B2")                                   # в секции gemini нет api_key
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert d["gemini"] == {"api_key": "B2", "model": "x"} and d["scan"]["api_key"] == "other"


def test_conversation_keeps_context(client, tmp_path):
    """Диалог: второй запрос видит первый вопрос, ответ и результаты инструментов; карта копится."""
    pytest.importorskip("google.genai")
    pytest.importorskip("folium")
    from google.genai import types

    from dvhectare.agent import Conversation, run_agent

    seen = []

    def text_of(contents):
        out = []
        for c in contents:
            for p in c.parts or []:
                if p.text:
                    out.append(p.text)
                if p.function_response is not None:
                    out.append(json.dumps(p.function_response.response, ensure_ascii=False, default=str))
        return "\n".join(out)

    class FakeModels:
        def generate_content(self, model, contents, config):
            seen.append((model, text_of(contents), len(contents)))
            last = contents[-1].parts[0]
            if last.text == "Что за участок 25:27:000000:2?":
                part = types.Part(function_call=types.FunctionCall(name="get_parcel", args={"cad_number": "25:27:000000:2"}),
                                  thought_signature=b"sig")
            elif last.text == "А что вокруг него?":
                part = types.Part(function_call=types.FunctionCall(name="scan_for_hectares", args={"lat": CENTER[0], "lon": CENTER[1], "radius_km": 0.6}))
            elif last.function_response is not None:
                part = types.Part.from_text(text=f"Ответ на шаг {len(contents)}")
            else:
                raise RuntimeError("сбой API")
            return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])

    class FakeClient:
        models = FakeModels()

    conv = Conversation()
    sc = Scanner(client, Settings(), extra_sources=[])
    out1 = run_agent("Что за участок 25:27:000000:2?", sc, str(tmp_path), client=FakeClient(), conversation=conv, model="m1")
    out2 = run_agent("А что вокруг него?", sc, str(tmp_path), client=FakeClient(), conversation=conv, model="m1")
    assert conv.turns == 2
    # второй запрос получил весь контекст первого
    ctx = seen[2][1]
    assert "Что за участок 25:27:000000:2?" in ctx and "5000" in ctx and "Ответ на шаг 3" in ctx

    # карта второго шага содержит и участок из первого запроса, и скан
    m2 = re.search(r"Карта: (.+)$", out2).group(1)
    html = open(m2, encoding="utf-8").read()
    for label in ("Запрошенные участки", "Кандидаты 1 га"):
        assert label in html or json.dumps(label)[1:-1] in html
    assert "Карта:" in out1

    # большие результаты скана в истории сжаты
    scan_resp = [p.function_response.response for c in conv.contents for p in c.parts or []
                 if p.function_response is not None and p.function_response.name == "scan_for_hectares"][0]
    assert len(scan_resp.get("top_candidates", [])) <= 5

    # смена модели убирает thought signatures, сбой API не ломает историю
    n = len(conv.contents)
    ans = conv.ask("сломайся", sc, str(tmp_path), client=FakeClient(), model="m2")
    assert "ошибкой" in ans and len(conv.contents) == n + 2
    assert not any(p.thought_signature for c in conv.contents for p in c.parts or [])

    # история ограничена и начинается с вопроса пользователя
    small = Conversation(max_history=1)
    small.ask("Что за участок 25:27:000000:2?", sc, str(tmp_path), client=FakeClient())
    small.ask("А что вокруг него?", sc, str(tmp_path), client=FakeClient())
    assert small.contents[0].parts[0].text == "А что вокруг него?"


def test_map_popups_copy_cadastral(client, tmp_path):
    """На карте у свободных участков и кандидатов есть карточка с кнопкой «Копировать»."""
    pytest.importorskip("folium")
    res = Scanner(client, Settings(), extra_sources=[]).scan(AOI)
    assert res.candidates and all(c.quarter == "25:27:030101" for c in res.candidates)
    assert len(res.candidates[0].corners()) == 4
    out = save_scan(res, tmp_path, gpkg=False, html=True)
    html = (out / "map.html").read_text(encoding="utf-8")
    assert "window.dvBind" in html and "dvCopy" in html and "free_parcels_filtered" in html
    fc = json.loads((out / "free_parcels_filtered.geojson").read_text(encoding="utf-8"))
    assert fc["features"][0]["properties"]["cad_num"] == "25:27:000000:2"


def test_gemini_overload_retry_and_fallback(client, tmp_path, monkeypatch):
    """503 «model experiencing high demand»: повторы, затем запасная модель, понятное сообщение."""
    pytest.importorskip("google.genai")
    from google.genai import errors, types

    import dvhectare.agent.gemini_agent as ga
    from dvhectare.agent import Conversation

    monkeypatch.setattr(ga.time, "sleep", lambda s: None)
    overload = lambda: errors.ServerError(503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
    sc = Scanner(client, Settings(), extra_sources=[])

    def ok(text):
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part.from_text(text=text)]))])

    class Flaky:  # 2 раза 503, потом успех
        calls = []

        def generate_content(self, model, contents, config):
            Flaky.calls.append(model)
            if len(Flaky.calls) <= 2:
                raise overload()
            return ok("готово")

    class C1: models = Flaky()
    assert Conversation().ask("q", sc, str(tmp_path), client=C1(), model="main") == "готово"
    assert Flaky.calls == ["main", "main", "main"]

    class MainDown:  # основная модель лежит — отвечает запасная
        calls = []

        def generate_content(self, model, contents, config):
            MainDown.calls.append(model)
            if model == "main":
                raise overload()
            return ok(f"ответила {model}")

    class C2: models = MainDown()
    conv = Conversation()
    assert conv.ask("q", sc, str(tmp_path), client=C2(), model="main", fallback_models=["backup"]) == "ответила backup"
    assert MainDown.calls == ["main"] * 4 + ["backup"]

    class AllDown:
        def generate_content(self, model, contents, config):
            raise overload()

    class C3: models = AllDown()
    conv = Conversation()
    ans = conv.ask("q", sc, str(tmp_path), client=C3(), model="main", fallback_models=[])
    assert "перегружен" in ans and "503" in ans
    assert len(conv.contents) == 2  # история цела: вопрос + сообщение об ошибке

    class BadKey:
        def generate_content(self, model, contents, config):
            raise errors.ClientError(400, {"error": {"code": 400, "message": "bad", "status": "INVALID_ARGUMENT"}})

    class C4: models = BadKey()
    assert "ошибкой" in Conversation().ask("q", sc, str(tmp_path), client=C4(), model="main")  # 400 — без повторов


def test_markers_have_copy_popups(client, tmp_path):
    """Метки поверх кандидатов и запрошенных участков открывают ту же карточку с «Копировать»."""
    pytest.importorskip("folium")
    from dvhectare.agent.session_map import SessionMap

    sm = SessionMap()
    sm.add_parcel(client.find_parcel("25:27:000000:2"))
    sm.add_scan(Scanner(client, Settings(), extra_sources=[]).scan(AOI))
    assert not sm.markers  # старых «немых» маркеров больше нет
    layers = {name: (kind, fc) for name, kind, fc in sm.layers}
    kind, pins = layers["Лучшие кандидаты (метки)"]
    assert kind == "candidates" and pins["features"][0]["geometry"]["type"] == "Point"
    assert pins["features"][0]["properties"]["quarter"] == "25:27:030101"
    parcel_kinds = {f["geometry"]["type"] for f in layers["Запрошенные участки"][1]["features"]}
    assert parcel_kinds == {"Polygon", "Point"}
    path = sm.save(tmp_path, "q", "a", note=False)
    assert "dvBind" in path.read_text(encoding="utf-8")


def test_history_restores_agent_context(client, tmp_path):
    """После «перезапуска» агент помнит прошлый диалог, а карта продолжает копить слои."""
    pytest.importorskip("google.genai")
    pytest.importorskip("folium")
    from google.genai import types

    from dvhectare.agent import Conversation
    from dvhectare.history import Dialog, DialogStore

    seen = []

    class FakeModels:
        def generate_content(self, model, contents, config):
            seen.append([p.text for c in contents for p in c.parts or [] if p.text])
            last = contents[-1].parts[0]
            if last.text == "Что за участок 25:27:000000:2?":
                part = types.Part(function_call=types.FunctionCall(name="get_parcel", args={"cad_number": "25:27:000000:2"}),
                                  thought_signature=b"\x00\xffsig")
            elif last.function_response is not None:
                part = types.Part.from_text(text="Участок 5000 м²")
            else:
                part = types.Part.from_text(text="Помню: речь про 25:27:000000:2")
            return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[part]))])

    class C: models = FakeModels()
    sc = Scanner(client, Settings(), extra_sources=[])
    store = DialogStore(tmp_path / "history")
    d = Dialog(conversation=Conversation())
    d.conversation.ask("Что за участок 25:27:000000:2?", sc, str(tmp_path), client=C(), model="m")
    d.messages = [{"role": "user", "text": "Что за участок 25:27:000000:2?"}, {"role": "bot", "text": "Участок 5000 м²"}]
    store.save(d)

    d2 = DialogStore(tmp_path / "history").load(d.id)  # «новый запуск»
    conv = d2.conversation
    assert conv.turns == 1 and conv.session_map.layers  # карта восстановлена
    sig = [p.thought_signature for c in conv.contents for p in c.parts or [] if p.thought_signature]
    assert sig == [b"\x00\xffsig"]
    assert conv.ask("А о чём мы говорили?", sc, str(tmp_path), client=C(), model="m").startswith("Помню")
    assert "Что за участок 25:27:000000:2?" in seen[-1]  # прошлый вопрос ушёл в контекст
    assert conv.session_map.render(tmp_path / "m.html").exists()


def test_nspd_403_pauses_requests_and_stops_agent(tmp_path):
    """403: понятное сообщение, дальше в сеть не ходим, агент останавливается без лишних шагов."""
    pytest.importorskip("google.genai")
    from google.genai import types

    from dvhectare.agent import Conversation
    from dvhectare.nspd.client import BlockedIP, blocked_seconds_left

    hits = []

    def handler(request):
        hits.append(request.url.path)
        return httpx.Response(403, text="<html><body>Access denied. Please enable JavaScript (challenge)</body></html>")

    c = NspdClient(min_delay=0, cache_path=None, block_cooldown=600, transport=httpx.MockTransport(handler))
    with pytest.raises(BlockedIP) as e1:
        c.find_parcel("25:27:000000:2")
    assert "защита от ботов" in str(e1.value) and "30–60 минут" in str(e1.value)
    assert blocked_seconds_left() > 500
    with pytest.raises(BlockedIP) as e2:
        c.find_parcel("25:27:000000:3")
    assert "приостановлены" in str(e2.value) and len(hits) == 1  # второй раз в сеть не ходили

    calls = []

    class M:
        def generate_content(self, model, contents, config):
            calls.append(1)
            return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(name="get_parcel", args={"cad_number": "25:27:000000:2"}))]))])

    class Cl: models = M()
    conv = Conversation()
    ans = conv.ask("что за участок?", Scanner(c, Settings(), extra_sources=[]), str(tmp_path), client=Cl(), model="m")
    assert ans.startswith("Не удалось получить данные") and len(calls) == 1  # модель не гоняли по кругу
    assert [x.role for x in conv.contents] == ["user", "model", "user", "model"]  # история корректна
