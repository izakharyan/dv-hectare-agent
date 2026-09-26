"""Подключаемые источники слоёв помимо НСПД.

Каждый источник реализует `fetch(bbox) -> list[dict]` и возвращает GeoJSON-фичи в WGS84
(geometry + properties). Роль источника (exclude/flag/zones) задаётся в config.yaml.
"""
from __future__ import annotations

from typing import Protocol

from ..config import ExtraSource
from ..geo import BBox
from .files import FileSource
from .wfs import FgisTpSource, WfsSource


class LayerSource(Protocol):
    name: str

    def fetch(self, bbox: BBox) -> list[dict]: ...


def build_source(cfg: ExtraSource) -> LayerSource:
    if cfg.type == "wfs":
        cls = FgisTpSource if cfg.name.lower().startswith("fgistp") else WfsSource
        return cls(name=cfg.name, url=cfg.url or "", type_name=cfg.type_name or "", extra_params=cfg.params)
    if cfg.type == "file":
        return FileSource(name=cfg.name, path=cfg.path or "")
    raise ValueError(f"Неизвестный тип источника: {cfg.type}")


__all__ = ["LayerSource", "WfsSource", "FgisTpSource", "FileSource", "build_source"]
