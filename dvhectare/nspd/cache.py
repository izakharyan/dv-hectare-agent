"""Простой дисковый кэш ответов (SQLite) — чтобы не долбить НСПД повторными запросами."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class ResponseCache:
    def __init__(self, path: str | Path, ttl_seconds: int = 7 * 24 * 3600):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL, body TEXT)"
        )
        self._conn.commit()

    @staticmethod
    def make_key(method: str, url: str, params: Any = None, body: Any = None) -> str:
        raw = json.dumps([method.upper(), url, params, body], sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        row = self._conn.execute("SELECT ts, body FROM cache WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        ts, body = row
        if self.ttl and time.time() - ts > self.ttl:
            return None
        return json.loads(body)

    def set(self, key: str, value: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, ts, body) VALUES (?, ?, ?)",
            (key, time.time(), json.dumps(value, ensure_ascii=False)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
