"""A small, explicit map of hh.ru area ids.

hh.ru's area dictionary is huge (thousands of entries) and its ids are stable
but not human-friendly.  Rather than guess an id from a name at runtime, this
module maps a deliberately small set of common city aliases to verified ids.
Anything else can be passed directly with ``--area-id``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Area ids verified against https://api.hh.ru/areas and
# https://api.hh.ru/suggests/areas during development.
DEFAULT_AREA_ID = 1
RUSSIA_AREA_ID = 113


@dataclass(frozen=True)
class Area:
    id: int
    name: str
    aliases: tuple[str, ...]


AREAS: tuple[Area, ...] = (
    Area(1, "Москва", ("moscow", "moskva", "москва")),
    Area(
        2,
        "Санкт-Петербург",
        (
            "saint petersburg",
            "saint-petersburg",
            "st petersburg",
            "st-petersburg",
            "spb",
            "piter",
            "санкт-петербург",
            "питер",
        ),
    ),
    Area(3, "Екатеринбург", ("yekaterinburg", "ekaterinburg", "ekb", "екатеринбург", "екб")),
    Area(4, "Новосибирск", ("novosibirsk", "новосибирск")),
    Area(78, "Самара", ("samara", "самара")),
    Area(88, "Казань", ("kazan", "казань")),
    Area(113, "Россия", ("russia", "all", "россия", "вся россия")),
)

AREA_BY_ID: dict[int, Area] = {area.id: area for area in AREAS}


class UnknownAreaError(ValueError):
    """Raised when ``--area`` does not match the built-in alias table."""


def normalize_area_name(value: str) -> str:
    """Case-fold, collapse whitespace and unify ``ё``/``е`` for comparisons."""

    text = value.strip().casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", text)


def _build_alias_table() -> dict[str, Area]:
    table: dict[str, Area] = {}
    for area in AREAS:
        for alias in area.aliases:
            table[normalize_area_name(alias)] = area
    return table


_ALIASES = _build_alias_table()


def supported_area_names() -> str:
    """Return a comma-separated list for CLI error messages."""

    return ", ".join(area.name for area in AREAS)


def resolve_area(value: str) -> Area:
    """Resolve a city name/alias to a verified hh.ru area id.

    ``--area-id`` exists for ids outside this small table; numeric input to
    ``--area`` is intentionally not accepted, so a typo cannot silently become
    an unrelated region.
    """

    normalized = normalize_area_name(value)
    if normalized in _ALIASES:
        return _ALIASES[normalized]
    raise UnknownAreaError(
        f"unknown area {value!r}. Known names: {supported_area_names()}. "
        "For any other hh.ru region pass --area-id with its numeric id."
    )
