"""Агент на Gemini API (function calling): естественный язык → вызовы НСПД → ответ.

    $env:GEMINI_API_KEY = "..."
    python -m dvhectare agent "Найди свободные гектары в 1 км от 43.35, 132.18 в сельхоз-зонах"
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

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
- Всегда напоминай, что окончательную проверку делает уполномоченный орган на надальнийвосток.рф,
  а участки без границ в ЕГРН на карте не видны.
Отвечай по-русски, кратко и по делу."""


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


def run_agent(
    prompt: str,
    scanner,
    out_dir: str,
    model: str | None = None,
    max_turns: int = 12,
    client: Any = None,  # для тестов можно подставить фейковый клиент
) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise SystemExit('Установите extras: pip install -e ".[agent]"') from e

    client = client or genai.Client()  # ключ из GEMINI_API_KEY (или GOOGLE_API_KEY)
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    handlers = make_handlers(scanner, out_dir)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        tools=[types.Tool(function_declarations=_function_declarations(types))],
        # вызовы инструментов выполняем сами, чтобы логировать их и ловить ошибки
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    contents: list = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]

    for _ in range(max_turns):
        resp = client.models.generate_content(model=model, contents=contents, config=config)
        if not resp.candidates:
            return "Модель не вернула ответ (возможно, сработал фильтр безопасности)."
        # добавляем ответ модели целиком — в нём thought signatures, без них Gemini 3 теряет контекст
        contents.append(resp.candidates[0].content)

        calls = resp.function_calls or []
        if not calls:
            return resp.text or ""

        parts = []
        for fc in calls:
            args = dict(fc.args or {})
            log.info("→ %s %s", fc.name, args)
            try:
                result = _json_safe(handlers[fc.name](**args))
            except Exception as e:  # ошибку отдаём модели, пусть решит, что делать
                result = {"error": f"{type(e).__name__}: {e}"}
            parts.append(types.Part.from_function_response(name=fc.name, response=result))
        contents.append(types.Content(role="user", parts=parts))
    return "Достигнут лимит шагов агента."
