"""Настройки проекта. Всё читается из config.yaml (см. config.example.yaml)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class NspdSettings:
    timeout: float = 20.0
    min_delay: float = 1.5          # пауза между запросами, сек
    block_cooldown_min: float = 30  # после 403 не обращаться к НСПД столько минут (не продлевать бан)
    retries: int = 3
    cache_path: Optional[str] = ".cache/nspd.sqlite"
    cache_ttl_days: int = 7
    ca_bundle: Optional[str] = None  # путь к russian_trusted_root_ca.pem
    proxy: Optional[str] = None      # прокси только для НСПД; null = напрямую (нужен российский IP)


@dataclass
class GeminiSettings:
    api_key: Optional[str] = None    # ключ Gemini; None → переменная GEMINI_API_KEY
    model: Optional[str] = None      # None → GEMINI_MODEL или gemini-3.8-flash
    proxy: Optional[str] = None      # прокси только для Gemini: http://host:port или socks5://host:port
    timeout: float = 120.0
    # запасные модели на случай перегрузки (503/429); null → gemini-3.5-flash-lite, gemini-3.1-flash-lite
    fallback_models: Optional[list[str]] = None


@dataclass
class AgentSettings:
    make_map: bool = True            # строить итоговую карту после ответа агента
    open_map: bool = True            # сразу открывать её в браузере


@dataclass
class HectareSettings:
    side_m: float = 100.0            # 100×100 = 1 га
    inset_m: float = 5.0             # отступ от чужих границ
    max_cells: int = 300
    min_patch_m2: float = 10_000     # игнорировать свободные куски меньше гектара


@dataclass
class ScanSettings:
    max_area_km2: float = 25.0       # защита от случайного «скана всего края»
    # Слои НСПД, которые ВЫРЕЗАЮТСЯ из свободной территории целиком
    exclude_layers: list[str] = field(
        default_factory=lambda: ["oopt", "tor", "oez", "okn", "planned_by_scheme", "planned_by_survey"]
    )
    # Слои, которые не вырезаются, а только помечают кандидата (нужна ручная проверка)
    flag_layers: list[str] = field(default_factory=lambda: ["zouit", "forestry", "settlements", "water"])
    fetch_zone_permitted_uses: bool = True
    # Регулярки по названию/индексу территориальной зоны (например "^СХ", "Сельскохоз").
    # Пусто — не фильтровать.
    allowed_zone_patterns: list[str] = field(default_factory=list)
    hectare: HectareSettings = field(default_factory=HectareSettings)


@dataclass
class ParcelFilter:
    """Фильтр для уже сформированных участков (слой «свободные от прав» и др.)."""
    vri_keywords: list[str] = field(
        default_factory=lambda: ["сельскохозяйств", "садоводств", "индивидуальн", "личного подсобного", "крестьянск"]
    )
    min_area_m2: float = 0
    max_area_m2: float = 10_000
    exclude_statuses: list[str] = field(default_factory=lambda: ["Архивный", "Аннулированный", "Снят"])


@dataclass
class ExtraSource:
    """Доп. источник: WFS (ФГИС ТП, региональные ГИСОГД) или локальный файл (GeoJSON/KML/GPKG)."""
    name: str
    type: str                        # "wfs" | "file"
    role: str = "flag"               # "exclude" | "flag" | "zones"
    url: Optional[str] = None
    type_name: Optional[str] = None
    path: Optional[str] = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Settings:
    nspd: NspdSettings = field(default_factory=NspdSettings)
    gemini: GeminiSettings = field(default_factory=GeminiSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    scan: ScanSettings = field(default_factory=ScanSettings)
    parcel_filter: ParcelFilter = field(default_factory=ParcelFilter)
    extra_sources: list[ExtraSource] = field(default_factory=list)
    output_dir: str = "out"


def _merge(dc, data: dict):
    for k, v in (data or {}).items():
        if not hasattr(dc, k):
            raise ValueError(f"Неизвестный параметр конфигурации: {k}")
        cur = getattr(dc, k)
        if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
            _merge(cur, v)
        else:
            setattr(dc, k, v)
    return dc


def load_settings(path: Optional[str | Path] = None) -> Settings:
    s = Settings()
    p = Path(path) if path else Path("config.yaml")
    if not p.exists():
        return s
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    extras = data.pop("extra_sources", []) or []
    _merge(s, data)
    s.extra_sources = [ExtraSource(**e) for e in extras]
    # относительные пути к сертификату считаем от папки с конфигом, а не от текущей папки
    if s.nspd.ca_bundle and not Path(s.nspd.ca_bundle).is_absolute():
        s.nspd.ca_bundle = str((p.parent / s.nspd.ca_bundle).resolve())
    if s.nspd.ca_bundle and not Path(s.nspd.ca_bundle).exists():
        raise FileNotFoundError(f"Не найден сертификат nspd.ca_bundle: {s.nspd.ca_bundle}")
    return s
