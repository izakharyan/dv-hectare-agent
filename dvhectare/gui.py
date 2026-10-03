"""Окно программы: модель Gemini + запрос + «Искать», карта — прямо в окне.

Запуск из исходников:  python -m dvhectare.gui
Сборка в exe:          .\\build_exe.ps1   (см. README)

Окно — это встроенный в Windows браузерный движок (WebView2, через pywebview).
Интерфейс (gui_static/index.html) и карты (out/...) отдаёт маленький локальный
HTTP-сервер, доступный только с этого компьютера (127.0.0.1).

Рядом с exe должны лежать config.yaml и папка certs\\ — их читает программа.
Диалоги с картами сохраняются в history\\ рядом с exe и открываются после перезапуска;
промежуточные файлы сканов — во временной папке (%TEMP%\\dvhectare_*), удаляются при закрытии.
Кэш НСПД (.cache\\) создаётся рядом с exe.
"""
from __future__ import annotations

import atexit
import functools
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import traceback
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

MODELS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview",
]
EXAMPLE_PROMPT = "Найди свободные гектары в сельхоз-зонах в радиусе 1 км от 43.35, 132.18"
STATIC_DIR = Path(__file__).resolve().parent / "gui_static"

log = logging.getLogger(__name__)


def base_dir() -> Path:
    """Папка программы: рядом с exe, а при запуске из исходников — текущая папка."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


# ---------------------------------------------------------------- поиск
def run_search(
    model: str,
    prompt: str,
    root: Path,
    api_key: str | None = None,
    out_dir: Path | None = None,
    conversation=None,
) -> tuple[str, Path | None]:
    """Один запрос к агенту. Возвращает (текст ответа, путь к карте или None).

    api_key — ключ из окна (важнее config.yaml); out_dir — куда писать карты (в окне — временная папка);
    conversation — диалог (Conversation): агент помнит прошлые запросы, карта копит найденное.
    """
    from .analysis import Scanner
    from .agent import run_agent
    from .cli import _client
    from .config import load_settings

    cfg = root / "config.yaml"
    if not cfg.exists():
        raise FileNotFoundError(f"Не найден {cfg}. Скопируйте config.example.yaml в config.yaml и впишите ключ Gemini.")
    s = load_settings(cfg)
    out_dir = str(out_dir or (root / s.output_dir))
    if s.nspd.cache_path and not Path(s.nspd.cache_path).is_absolute():
        s.nspd.cache_path = str(root / s.nspd.cache_path)
    g = s.gemini
    with _client(s) as client:
        answer = run_agent(
            prompt, Scanner(client, s), out_dir,
            model=model or g.model, proxy=g.proxy, timeout=g.timeout, api_key=api_key or g.api_key, fallback_models=g.fallback_models,
            make_map=True, open_map=False, map_note=False,  # карту показываем в окне, не в браузере
            conversation=conversation,
        )
    m = re.search(r"\n\nКарта: (.+)$", answer)
    if m:
        return answer[: m.start()], Path(m.group(1).strip())
    return answer, None


def save_api_key(cfg: Path, key: str) -> None:
    """Записывает gemini.api_key в config.yaml, не трогая остальное содержимое и комментарии."""
    value = json.dumps(key)  # строка в двойных кавычках — валидный YAML
    lines = cfg.read_text(encoding="utf-8").splitlines() if cfg.exists() else []
    start = next((i for i, l in enumerate(lines) if re.match(r"^gemini:\s*(#.*)?$", l)), None)
    if start is None:
        lines += ["", "gemini:", f"  api_key: {value}"]
    else:
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i])), len(lines))
        for i in range(start + 1, end):
            m = re.match(r"^(\s+api_key:\s*)([^#]*?)(\s+#.*)?$", lines[i])
            if m:
                lines[i] = f"{m.group(1)}{value}{m.group(3) or ''}"
                break
        else:
            lines.insert(start + 1, f"  api_key: {value}")
    cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- локальный сервер
class _Handler(SimpleHTTPRequestHandler):
    """/            → интерфейс (gui_static)
       /out/...     → временные результаты текущего запуска
       /history/... → сохранённые диалоги с картами"""

    out_dir: Path = Path("out")
    history_dir: Path | None = None

    def translate_path(self, path: str) -> str:
        if path.startswith("/out/"):
            return self._under(self.out_dir, path[len("/out"):])
        if path.startswith("/history/") and self.history_dir is not None:
            return self._under(self.history_dir, path[len("/history"):])
        return self._under(STATIC_DIR, path)

    def _under(self, root: Path, path: str) -> str:
        # штатная нормализация SimpleHTTPRequestHandler (защита от ../), но от нужного корня
        self.directory = str(root)
        return SimpleHTTPRequestHandler.translate_path(self, path)

    def log_message(self, *args) -> None:  # не засоряем лог
        pass


def start_server(out_dir: Path, history_dir: Path | None = None) -> tuple[ThreadingHTTPServer, int]:
    handler = type("Handler", (_Handler,), {"out_dir": out_dir, "history_dir": history_dir})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(handler, directory=str(STATIC_DIR)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ---------------------------------------------------------------- API для окна (JS → Python)
class _ListHandler(logging.Handler):
    def __init__(self, sink: list, sink_lock: threading.Lock):
        super().__init__()
        # не self.lock: это имя занято собственной блокировкой logging.Handler → была взаимоблокировка
        self.sink, self.sink_lock = sink, sink_lock

    def emit(self, record: logging.LogRecord) -> None:
        with self.sink_lock:
            self.sink.append({"type": "log", "text": self.format(record)})


class Api:
    """Методы вызываются из index.html: window.pywebview.api.<метод>().

    Диалоги (переписка + контекст агента + карта) хранятся в history/ рядом с программой
    и открываются снова после перезапуска.
    """

    def __init__(self, root: Path, search_fn=run_search, out_dir: Path | None = None, history_dir: Path | None = None):
        from .history import DialogStore

        self.root = root
        # промежуточные файлы сканов — во временной папке, удаляется при закрытии программы
        self.out_dir = out_dir or Path(tempfile.mkdtemp(prefix="dvhectare_"))
        self.store = DialogStore(history_dir or (root / "history"))
        self.search_fn = search_fn
        self.state_file = root / "gui_state.json"
        self.events: list[dict] = []
        self.sink_lock = threading.Lock()
        self.busy = False
        self.dialog = self._new_dialog()
        h = _ListHandler(self.events, self.sink_lock)
        h.setFormatter(logging.Formatter("%(message)s"))
        self._log_handler = h

    # --- начальные данные
    def init(self) -> dict:
        st = self._load_state()
        model = st.get("model") or self._config_model() or MODELS[0]
        last = st.get("dialog")
        current = None
        if last:
            try:
                current = self.open_dialog(last)
            except Exception:
                current = None
        return {
            "models": MODELS,
            "model": model,
            "prompt": "" if current else (st.get("prompt") or EXAMPLE_PROMPT),
            "api_key": self._config_value("api_key") or "",
            "dialogs": self.store.list(),
            "current": current or self._payload(self.dialog),
        }

    # --- диалоги
    def _new_dialog(self):
        from .history import Dialog

        d = Dialog()
        try:
            from .agent import Conversation

            d.conversation = Conversation()
        except Exception:  # без google-genai (тесты окна) — контекст не нужен
            d.conversation = None
        return d

    @property
    def conversation(self):
        return self.dialog.conversation

    def list_dialogs(self) -> list[dict]:
        return self.store.list()

    def new_dialog(self) -> dict:
        if self.busy:
            return {"ok": False, "error": "Дождитесь окончания поиска"}
        self.dialog = self._new_dialog()
        self._save_state(dialog=None)
        return {"ok": True, "current": self._payload(self.dialog)}

    reset = new_dialog  # старое имя

    def open_dialog(self, dialog_id: str) -> dict:
        if self.busy:
            raise RuntimeError("Дождитесь окончания поиска")
        self.dialog = self.store.load(dialog_id)
        self._save_state(dialog=dialog_id)
        return self._payload(self.dialog)

    def delete_dialog(self, dialog_id: str) -> dict:
        if self.busy:
            return {"ok": False, "error": "Дождитесь окончания поиска"}
        self.store.delete(dialog_id)
        current_deleted = self.dialog.id == dialog_id
        if current_deleted:
            self.dialog = self._new_dialog()
            self._save_state(dialog=None)
        return {"ok": True, "dialogs": self.store.list(), "current": self._payload(self.dialog) if current_deleted else None}

    def _payload(self, d) -> dict:
        mp = self.store.map_path(d.id) if d.saved else None
        return {
            "id": d.id,
            "title": d.title,
            "saved": d.saved,
            "messages": d.messages,
            "turns": d.turns,
            "map": self._history_url(d.id) if mp and mp.exists() else None,
        }

    def _history_url(self, dialog_id: str) -> str:
        return f"/history/{quote(dialog_id)}/map.html"

    def context_size(self) -> int:
        return self.dialog.turns

    # --- поиск
    def search(self, model: str, prompt: str, api_key: str = "") -> dict:
        prompt = (prompt or "").strip()
        api_key = (api_key or "").strip()
        if self.busy:
            return {"ok": False, "error": "Поиск уже идёт"}
        if not prompt:
            return {"ok": False, "error": "Введите запрос"}
        if not api_key:
            return {"ok": False, "error": "Введите ключ Gemini"}
        if api_key != (self._config_value("api_key") or ""):
            try:
                save_api_key(self.root / "config.yaml", api_key)
            except Exception as e:
                log.warning("Не удалось сохранить ключ в config.yaml: %s", e)
        self._save_state(model=model, prompt="")
        with self.sink_lock:
            self.events.clear()
        self.busy = True
        threading.Thread(target=self._worker, args=(model.strip(), prompt, api_key), daemon=True).start()
        return {"ok": True}

    def poll(self, since: int = 0) -> dict:
        with self.sink_lock:
            return {"events": self.events[since:], "next": len(self.events), "busy": self.busy}

    def _worker(self, model: str, prompt: str, api_key: str) -> None:
        from .history import make_title

        root_logger = logging.getLogger()
        prev_level = root_logger.level
        root_logger.setLevel(min(prev_level or logging.WARNING, logging.INFO))  # шаги агента пишутся на INFO
        root_logger.addHandler(self._log_handler)
        d = self.dialog
        bot: dict = {"role": "bot"}
        try:
            answer, path = self.search_fn(
                model, prompt, self.root, api_key=api_key, out_dir=self.out_dir, conversation=d.conversation
            )
            bot["text"] = answer
        except BaseException as e:  # SystemExit (нет ключа) тоже показываем в окне
            log.debug(traceback.format_exc())
            answer, path = None, None
            bot["error"] = str(e) if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        finally:
            root_logger.removeHandler(self._log_handler)
            root_logger.setLevel(prev_level)

        with self.sink_lock:
            steps = [ev["text"] for ev in self.events if ev["type"] == "log"]
        if steps:
            bot["steps"] = steps
        if not d.messages:
            d.title = make_title(prompt)
        d.model = model
        d.messages += [{"role": "user", "text": prompt}, bot]

        map_url = None
        try:
            self.store.save(d)
            if path and Path(path).exists():
                shutil.copyfile(path, self.store.map_path(d.id))
            mp = self.store.map_path(d.id)
            map_url = self._history_url(d.id) if mp.exists() else None
            self._save_state(dialog=d.id)
        except Exception as e:
            log.warning("Не удалось сохранить диалог: %s", e)
            map_url = self._map_url(path)

        if "error" in bot:
            ev = {"type": "error", "text": bot["error"]}
        else:
            ev = {"type": "done", "answer": answer, "map": map_url}
        ev.update(turns=d.turns, dialog=self._payload(d) | {"messages": None}, dialogs=self.store.list())
        with self.sink_lock:
            self.events.append(ev)
        self.busy = False

    # --- карты
    def cleanup(self) -> None:
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def _map_url(self, path: Path | None) -> str | None:
        if not path:
            return None
        try:
            rel = Path(path).resolve().relative_to(self.out_dir.resolve())
        except ValueError:
            return None
        return "/out/" + quote(rel.as_posix())

    # --- состояние окна
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    _UNSET = object()

    def _save_state(self, model: str | None = None, prompt: str | None = None, dialog=_UNSET) -> None:
        st = self._load_state()
        if model is not None:
            st["model"] = model
        if prompt is not None:
            st["prompt"] = prompt
        if dialog is not Api._UNSET:
            st["dialog"] = dialog
        try:
            self.state_file.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _config_model(self) -> str | None:
        return self._config_value("model")

    def _config_value(self, name: str) -> str | None:
        """gemini.<name> из config.yaml (без проверки сертификата и прочего — только чтение)."""
        try:
            import yaml

            data = yaml.safe_load((self.root / "config.yaml").read_text(encoding="utf-8")) or {}
            v = (data.get("gemini") or {}).get(name)
            return str(v) if v else None
        except Exception:
            return None


# ---------------------------------------------------------------- запуск
def selftest(path: str) -> int:
    """Проверка собранного exe без сети и без окна: все библиотеки и данные на месте."""
    lines = []
    try:
        import folium
        import google.genai  # noqa: F401
        import pyproj
        import webview  # noqa: F401
        from shapely.geometry import Point

        from .geo import reproject

        p = reproject(Point(132.18, 43.35), "EPSG:4326", "EPSG:32653")
        lines.append(f"pyproj ok: {p.x:.0f},{p.y:.0f} (proj {pyproj.proj_version_str})")
        out = Path(path).with_suffix(".html")
        folium.Map(location=[43.35, 132.18]).save(str(out))
        lines.append(f"folium ok: {out.stat().st_size} байт")
        assert (STATIC_DIR / "index.html").exists(), f"нет {STATIC_DIR / 'index.html'}"
        lines.append("ui ok")
        lines.append("OK")
        code = 0
    except Exception:
        lines.append(traceback.format_exc())
        code = 1
    Path(path).write_text("\n".join(lines), encoding="utf-8")
    return code


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))

    import webview

    root = base_dir()
    os.chdir(root)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    api = Api(root)
    atexit.register(api.cleanup)
    _srv, port = start_server(api.out_dir, api.store.root)
    webview.create_window(
        "ДВ-гектар — поиск земли",
        f"http://127.0.0.1:{port}/index.html",
        js_api=api,
        width=1440,
        height=860,
        min_size=(1100, 600),
    )
    webview.start()
    api.cleanup()


if __name__ == "__main__":
    main()
