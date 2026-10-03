"""Агент на Gemini API (function calling): естественный язык → вызовы НСПД → ответ.

    $env:GEMINI_API_KEY = "..."
    python -m dvhectare agent "Найди свободные гектары в 1 км от 43.35, 132.18 в сельхоз-зонах"
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

from .session_map import SessionMap
from .tools import TOOL_SCHEMAS, make_handlers

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.8-flash"

SYSTEM_PROMPT = """Ты — ассистент по подбору земли под «Дальневосточный гектар» (119-ФЗ).
У тебя есть инструменты доступа к Национальной системе пространственных данных (НСПД, бывшая ПКК).
Правила:
- Если пользователь называет место словами, сам определи приблизительные координаты и скажи, какие взял.
- scan_for_hectares — дорогой; начинай с радиуса 0.5–1 км, расширяй только по просьбе.
- Объясняй результат: сколько свободной территории, лучшие кандидаты (координаты + ссылка НСПД),
  в какой они территориальной зоне и какие флаги (ЗОУИТ, лесничество, НП, водоохрана) требуют проверки.
- Если инструмент вернул ошибку BlockedIP / 403 от НСПД — НЕ вызывай инструменты повторно,
  сразу сообщи пользователю текст ошибки и что делать.
- Всегда напоминай, что окончательную проверку делает уполномоченный орган на надальнийвосток.рф,
  а участки без границ в ЕГРН на карте не видны.
Отвечай по-русски, кратко и по делу."""


def make_client(proxy: str | None = None, timeout: float = 120.0, api_key: str | None = None):
    """Клиент Gemini. Ключ: api_key из config.yaml → иначе GEMINI_API_KEY / GOOGLE_API_KEY.

    proxy — отдельный прокси только для Gemini (http://host:port, socks5://host:port).
    Запросы к НСПД он не затрагивает.
    """
    from google import genai
    from google.genai import types

    http = {"timeout": int(timeout * 1000)}  # SDK ждёт миллисекунды
    if proxy:
        http["client_args"] = {"proxy": proxy}
        http["async_client_args"] = {"proxy": proxy}
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("Нет ключа Gemini: укажите gemini.api_key в config.yaml или переменную GEMINI_API_KEY")
    return genai.Client(api_key=key, http_options=types.HttpOptions(**http))


def _function_declarations(types) -> list:
    return [
        types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters_json_schema=t["input_schema"],
        )
        for t in TOOL_SCHEMAS
    ]


def _json_safe(obj: Any, limit: int = 60_000) -> dict:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) > limit:
        return {"truncated": True, "text": text[:limit]}
    data = json.loads(text)
    return data if isinstance(data, dict) else {"result": data}


class Conversation:
    """Диалог с агентом: Gemini помнит прошлые запросы, ответы и результаты инструментов,
    а карта накапливает всё найденное за диалог.

    История ограничена последними `max_history` запросами пользователя; объёмные результаты
    инструментов из уже завершённых шагов сжимаются до сводки — иначе каждый следующий
    запрос становился бы всё дороже.
    """

    def __init__(self, max_history: int = 12, compact_limit: int = 4000):
        self.contents: list = []
        self.session_map = SessionMap()
        self.turns = 0
        self.max_history = max_history
        self.compact_limit = compact_limit
        self._last_model: str | None = None

    def ask(
        self,
        prompt: str,
        scanner,
        out_dir: str,
        *,
        model: str | None = None,
        client: Any = None,
        proxy: str | None = None,
        timeout: float = 120.0,
        api_key: str | None = None,
        max_steps: int = 12,
        fallback_models: list[str] | None = None,
    ) -> str:
        from google.genai import types

        model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
        if self._last_model and model != self._last_model:
            _drop_thought_signatures(self.contents)  # подписи привязаны к модели, при смене — убираем
        self._last_model = model

        start = len(self.contents)
        self.contents.append(types.Content(role="user", parts=[types.Part.from_text(text=prompt)]))
        self.turns += 1
        try:
            chain = _model_chain(model, fallback_models)
            answer, used = _loop(self.contents, scanner, out_dir, chain, max_steps, client, proxy, timeout, self.session_map, api_key)
            self._last_model = used
        except Exception as e:
            log.exception("Агент завершился с ошибкой")
            answer = _friendly_error(e)
            # незавершённый шаг (вызов без ответа инструмента) сломает следующий запрос — откатываем,
            # оставляя в истории сам вопрос и текст ошибки
            del self.contents[start + 1:]
            self.contents.append(types.Content(role="model", parts=[types.Part.from_text(text=answer)]))
        self._compact(start)
        self._trim()
        return answer

    # --- сохранение между запусками программы
    def to_dict(self) -> dict:
        return {
            "version": 1,
            "turns": self.turns,
            "last_model": self._last_model,
            "contents": [c.model_dump(mode="json", exclude_none=True) for c in self.contents],
        }

    @classmethod
    def from_dict(cls, d: dict, session_map: SessionMap | None = None) -> "Conversation":
        from google.genai import types

        conv = cls()
        conv.turns = d.get("turns", 0)
        conv._last_model = d.get("last_model")
        conv.contents = [types.Content.model_validate(c) for c in d.get("contents", [])]
        if session_map is not None:
            conv.session_map = session_map
        return conv

    def save_map(self, out_dir: str, prompt: str, answer: str, note: bool = True) -> Path | None:
        return self.session_map.save(out_dir, prompt, answer, note=note)

    # --- обслуживание истории
    def _compact(self, start: int) -> None:
        """Сжимает результаты инструментов завершённого шага до сводки."""
        for c in self.contents[start:]:
            for part in c.parts or []:
                fr = getattr(part, "function_response", None)
                if fr is not None and fr.response is not None:
                    fr.response = _compact_result(fr.response, self.compact_limit)

    def _trim(self) -> None:
        """Оставляет последние max_history запросов (режем только по границе вопроса пользователя)."""
        starts = [i for i, c in enumerate(self.contents) if c.role == "user" and any(getattr(p, "text", None) for p in c.parts or [])]
        if len(starts) > self.max_history:
            del self.contents[: starts[-self.max_history]]


def _drop_thought_signatures(contents: list) -> None:
    for c in contents:
        for part in c.parts or []:
            if getattr(part, "thought_signature", None):
                part.thought_signature = None


def _compact_result(result: dict, limit: int) -> dict:
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return result
    if "top_candidates" in result:  # результат скана: оставляем статистику и 5 лучших кандидатов
        keep = {k: v for k, v in result.items() if k not in ("top_candidates", "free_parcels_matching_filter")}
        keep["top_candidates"] = result.get("top_candidates", [])[:5]
        keep["free_parcels_matching_filter"] = result.get("free_parcels_matching_filter", [])[:5]
        keep["_note"] = "сокращено для истории диалога"
        if len(json.dumps(keep, ensure_ascii=False, default=str)) <= limit * 2:
            return keep
    return {"truncated": True, "text": text[:limit]}


def run_agent(
    prompt: str,
    scanner,
    out_dir: str,
    model: str | None = None,
    max_turns: int = 12,
    client: Any = None,  # для тестов можно подставить фейковый клиент
    proxy: str | None = None,
    timeout: float = 120.0,
    api_key: str | None = None,
    make_map: bool = True,
    open_map: bool = False,
    map_note: bool = True,  # панель с ответом поверх карты (в окне программы не нужна)
    conversation: Conversation | None = None,
    fallback_models: list[str] | None = None,
) -> str:
    """Запускает агента. В конце строит карту всего найденного (out/agent_<дата>/map.html)
    и дописывает путь к ней в ответ. open_map=True — сразу открыть карту в браузере.

    conversation — продолжить диалог (контекст прошлых запросов и накопленная карта);
    без него каждый вызов — отдельный разговор с чистого листа."""
    conv = conversation or Conversation()
    answer = conv.ask(prompt, scanner, out_dir, model=model, client=client, proxy=proxy,
                      timeout=timeout, api_key=api_key, max_steps=max_turns, fallback_models=fallback_models)
    if not make_map:
        return answer
    path = conv.save_map(out_dir, prompt, answer, note=map_note)
    if path is None:
        return answer
    if open_map:
        import webbrowser

        webbrowser.open(path.resolve().as_uri())
    return f"{answer}\n\nКарта: {path}"


def _loop(contents, scanner, out_dir, chain, max_turns, client, proxy, timeout, sm, api_key=None) -> tuple[str, str]:
    """Шаги агента. chain — модель и запасные модели. Возвращает (ответ, модель, которая отвечала)."""
    try:
        from google import genai  # noqa: F401
        from google.genai import types
    except ImportError as e:
        raise SystemExit('Установите extras: pip install -e ".[agent]"') from e

    client = client or make_client(proxy=proxy, timeout=timeout, api_key=api_key)
    handlers = make_handlers(scanner, out_dir, session_map=sm)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=_function_declarations(types))],
        # вызовы инструментов выполняем сами, чтобы логировать их и ловить ошибки
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )

    for _ in range(max_turns):
        resp, chain = _generate(client, chain, contents, config)
        if not resp.candidates or resp.candidates[0].content is None:
            text = "Модель не вернула ответ (возможно, сработал фильтр безопасности)."
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
            return text, chain[0]
        # добавляем ответ модели целиком — в нём thought signatures, без них Gemini 3 теряет контекст
        contents.append(resp.candidates[0].content)

        calls = resp.function_calls or []
        if not calls:
            return resp.text or "", chain[0]

        from ..nspd.client import BlockedIP

        parts = []
        blocked: BlockedIP | None = None
        for fc in calls:
            args = dict(fc.args or {})
            log.info("→ %s %s", fc.name, args)
            try:
                result = _json_safe(handlers[fc.name](**args))
            except BlockedIP as e:  # НСПД заблокировал — дальше крутить агента бессмысленно (и вредно)
                blocked = e
                result = {"error": f"BlockedIP: {e}"}
            except Exception as e:  # остальные ошибки отдаём модели, пусть решит, что делать
                result = {"error": f"{type(e).__name__}: {e}"}
            parts.append(types.Part.from_function_response(name=fc.name, response=result))
        contents.append(types.Content(role="user", parts=parts))
        if blocked is not None:
            text = f"Не удалось получить данные: {blocked}"
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
            return text, chain[0]

    text = "Достигнут лимит шагов агента."
    contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
    return text, chain[0]


# ---------------------------------------------------------------- перегрузка Gemini (503/429)
RETRY_CODES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (2, 5, 12)  # секунды между повторами на одной модели
DEFAULT_FALLBACKS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


def _model_chain(model: str, fallbacks: list[str] | None) -> list[str]:
    chain = [model]
    for m in DEFAULT_FALLBACKS if fallbacks is None else fallbacks:
        if m and m not in chain:
            chain.append(m)
    return chain


def _error_code(e: Exception) -> int | None:
    code = getattr(e, "code", None)
    return code if isinstance(code, int) else None


def _generate(client, chain: list[str], contents: list, config):
    """generate_content с повторами при перегрузке и переходом на запасную модель.
    Возвращает (ответ, оставшаяся цепочка — первая модель в ней та, что ответила)."""
    last: Exception | None = None
    for i, model in enumerate(chain):
        if i > 0:
            log.info("Модель %s перегружена — переключаюсь на %s", chain[i - 1], model)
            _drop_thought_signatures(contents)  # подписи прежней модели новой не подходят
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return client.models.generate_content(model=model, contents=contents, config=config), chain[i:]
            except Exception as e:
                code = _error_code(e)
                if code not in RETRY_CODES:
                    raise
                last = e
                if attempt < len(RETRY_DELAYS):
                    delay = RETRY_DELAYS[attempt] * random.uniform(0.8, 1.3)
                    log.info("Gemini %s ответил %s (перегрузка) — повтор через %.0f с", model, code, delay)
                    time.sleep(delay)
    raise last  # type: ignore[misc]


def _friendly_error(e: Exception) -> str:
    code = _error_code(e)
    if code == 503 or code in (500, 502, 504):
        return ("Gemini сейчас перегружен (ошибка %s), повторы и запасные модели не помогли. "
                "Попробуйте через пару минут или выберите другую модель." % code)
    if code == 429:
        return ("Превышен лимит запросов Gemini (429). На бесплатном тарифе есть ограничения в минуту и в день — "
                "подождите или выберите другую модель.")
    if code in (401, 403):
        return f"Gemini отклонил ключ ({code}). Проверьте ключ API."
    return f"Агент остановился с ошибкой: {type(e).__name__}: {e}"
