"""Агент на Claude API (tool use): естественный язык → вызовы НСПД → ответ.

    set ANTHROPIC_API_KEY=sk-ant-...
    python -m dvhectare agent "Найди свободные гектары в 1 км от 43.35, 132.18 в сельхоз-зонах"
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from .tools import TOOL_SCHEMAS, make_handlers

log = logging.getLogger(__name__)

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


def run_agent(prompt: str, scanner, out_dir: str, model: str | None = None, max_turns: int = 12) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise SystemExit("Установите extras: pip install -e .[agent]") from e

    client = anthropic.Anthropic()  # ключ из ANTHROPIC_API_KEY
    model = model or os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
    handlers = make_handlers(scanner, out_dir)
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]

    for _ in range(max_turns):
        resp = client.messages.create(
            model=model, max_tokens=4096, system=SYSTEM_PROMPT, tools=TOOL_SCHEMAS, messages=messages
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text")

        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            log.info("→ %s %s", block.name, block.input)
            try:
                out = handlers[block.name](**block.input)
                content, is_error = json.dumps(out, ensure_ascii=False, default=str)[:60_000], False
            except Exception as e:  # ошибку отдаём модели, пусть решит, что делать
                content, is_error = f"{type(e).__name__}: {e}", True
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})
    return "Достигнут лимит шагов агента."
