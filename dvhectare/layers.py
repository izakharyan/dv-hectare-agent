"""Реестр слоёв НСПД (nspd.gov.ru — бывшая ПКК).

layer_id    — используется в WMS (`/api/aeggis/v3/{layer_id}/wms`), в т.ч. GetFeatureInfo по точке.
category_id — используется в поиске по контуру (`/api/geoportal/v1/intersects`).

ID взяты из актуальной схемы слоёв НСПД (см. open-source библиотеку pynspd).
НСПД время от времени меняет ID — если слой перестал отвечать, откройте
nspd.gov.ru/map, DevTools → Network и посмотрите актуальный ID в запросах к aeggis.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NspdLayer:
    key: str
    title: str
    layer_id: int
    category_id: int


_L = NspdLayer

LAYERS: dict[str, NspdLayer] = {
    # --- кадастр ---
    "parcels": _L("parcels", "Земельные участки из ЕГРН", 36048, 36368),
    "quarters": _L("quarters", "Кадастровые кварталы", 36071, 36381),
    "buildings": _L("buildings", "Здания", 36049, 36369),
    "free_parcels": _L("free_parcels", "Земельные участки, свободные от прав третьих лиц", 37298, 38979),
    "auction_parcels": _L("auction_parcels", "Земельные участки, выставленные на аукцион", 37299, 38981),
    "planned_by_scheme": _L("planned_by_scheme", "ЗУ, образуемые по схеме расположения", 37294, 38943),
    "planned_by_survey": _L("planned_by_survey", "ЗУ, образуемые по проекту межевания", 36473, 37158),
    # --- зонирование ---
    "terr_zones": _L("terr_zones", "Территориальные зоны (ПЗЗ)", 875838, 472819),
    "settlements": _L("settlements", "Населённые пункты (полигоны)", 875831, 472812),
    "municipalities": _L("municipalities", "Муниципальные образования", 875819, 472800),
    # --- ограничения / особые территории ---
    # Все ЗОУИТ в НСПД имеют общую категорию 36940 → intersects вернёт их вместе.
    "zouit": _L("zouit", "ЗОУИТ (все виды)", 37581, 36940),
    "oopt": _L("oopt", "Особо охраняемые природные территории", 875845, 472825),
    "tor": _L("tor", "Территории опережающего развития", 875848, 472828),
    "oez": _L("oez", "Особые экономические зоны", 875846, 472826),
    "okn": _L("okn", "Территории объектов культурного наследия", 875840, 472820),
    "forestry": _L("forestry", "Лесничества", 875866, 472847),
    "hunting": _L("hunting", "Охотничьи угодья", 875847, 472827),
    "water": _L("water", "Береговые линии (водные объекты)", 875832, 472813),
}


def get_layer(key: str) -> NspdLayer:
    try:
        return LAYERS[key]
    except KeyError as e:
        raise KeyError(f"Неизвестный слой '{key}'. Доступные: {', '.join(LAYERS)}") from e
