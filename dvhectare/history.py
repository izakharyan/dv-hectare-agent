"""История диалогов окна программы: переписка, контекст агента и карта — на диске, между запусками.

    history/
      <id>/dialog.json     — заголовок, даты, сообщения ленты, контекст Gemini
      <id>/map_state.json  — слои карты (чтобы следующие запросы дополняли её, а не начинали заново)
      <id>/map.html        — готовая карта для показа
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, data: Any) -> None:
    """Атомарная запись: сбой посреди записи не испортит старый файл."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class Dialog:
    id: str = field(default_factory=lambda: datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6])
    title: str = "Новый диалог"
    created: str = field(default_factory=_now)
    updated: str = field(default_factory=_now)
    model: Optional[str] = None
    messages: list[dict] = field(default_factory=list)  # {"role": "user"|"bot", "text", "steps"?, "error"?}
    conversation: Any = None  # dvhectare.agent.Conversation (или None без google-genai)
    saved: bool = False

    @property
    def turns(self) -> int:
        return sum(1 for m in self.messages if m.get("role") == "user")

    def meta(self) -> dict:
        return {"id": self.id, "title": self.title, "updated": self.updated, "turns": self.turns}


class DialogStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, dialog_id: str) -> Path:
        d = (self.root / dialog_id).resolve()
        if self.root.resolve() not in d.parents:  # id приходит из окна — не выпускаем за пределы history/
            raise ValueError(f"Некорректный id диалога: {dialog_id}")
        return d

    def map_path(self, dialog_id: str) -> Path:
        return self.dir(dialog_id) / "map.html"

    # ---------------------------------------------------------------- список
    def list(self) -> list[dict]:
        out = []
        for f in self.root.glob("*/dialog.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                out.append({"id": d["id"], "title": d.get("title") or "Без названия", "updated": d.get("updated", ""),
                            "turns": sum(1 for m in d.get("messages", []) if m.get("role") == "user")})
            except Exception:
                continue  # битый файл не должен ломать список
        return sorted(out, key=lambda x: x["updated"], reverse=True)

    # ---------------------------------------------------------------- чтение / запись
    def load(self, dialog_id: str) -> Dialog:
        d = self.dir(dialog_id)
        data = json.loads((d / "dialog.json").read_text(encoding="utf-8"))
        conv = None
        try:
            from .agent import Conversation
            from .agent.session_map import SessionMap

            sm = None
            if (d / "map_state.json").exists():
                sm = SessionMap.from_dict(json.loads((d / "map_state.json").read_text(encoding="utf-8")))
            conv = Conversation.from_dict(data.get("conversation") or {}, session_map=sm)
        except ImportError:
            pass
        return Dialog(id=data["id"], title=data.get("title", ""), created=data.get("created", ""),
                      updated=data.get("updated", ""), model=data.get("model"), messages=data.get("messages", []),
                      conversation=conv, saved=True)

    def save(self, dialog: Dialog) -> None:
        d = self.dir(dialog.id)
        d.mkdir(parents=True, exist_ok=True)
        dialog.updated = _now()
        conv = dialog.conversation
        _write_json(d / "dialog.json", {
            "id": dialog.id, "title": dialog.title, "created": dialog.created, "updated": dialog.updated,
            "model": dialog.model, "messages": dialog.messages,
            "conversation": conv.to_dict() if conv is not None else None,
        })
        if conv is not None:
            _write_json(d / "map_state.json", conv.session_map.to_dict())
        dialog.saved = True

    def delete(self, dialog_id: str) -> None:
        shutil.rmtree(self.dir(dialog_id), ignore_errors=True)


def make_title(prompt: str, limit: int = 60) -> str:
    t = " ".join(prompt.split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"
