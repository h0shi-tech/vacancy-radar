"""Dataclasses and hh.ru payload parsing.

The public hh.ru Vacancies API returns JSON objects with nullable ``salary``,
``employer``, ``area`` and ``snippet`` fields.  This module is the only place
that translates that wire format into the rest of the application.  Parsing is
deliberately defensive: a malformed payload raises :class:`DataError` with a
short explanation, while individually optional fields become ``None``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .errors import DataError

# Employer names hh.ru uses when a company is not disclosed.  Values are
# case-folded for comparison.
ANONYMOUS_EMPLOYER_NAMES = frozenset(
    {
        "анонимный работодатель",
        "anonymous employer",
        "аноним",
        "unknown",
    }
)

_REMOTE_SCHEDULE_IDS = frozenset({"remote"})
_REMOTE_WORK_FORMAT_IDS = frozenset({"REMOTE", "remote"})
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def ensure_aware(value: datetime | None = None) -> datetime:
    """Return an aware datetime.

    hh.ru always sends an offset, but fixture authors and callers of the pure
    scoring functions may pass naive datetimes.  Naive values are interpreted
    as UTC; an omitted value means "now" in UTC.
    """

    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # ``Z`` is valid ISO 8601 but Python 3.11's fromisoformat does not accept
    # it; normalise it to an explicit offset first.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ensure_aware(parsed)


def _clean_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # Collapse whitespace and drop terminal control characters.  Vacancy data
    # comes from a remote service, so it must not be able to emit ANSI escapes
    # into a user's terminal.
    collapsed = " ".join(value.split())
    cleaned = "".join(character for character in collapsed if character.isprintable())
    return cleaned or None


@dataclass(frozen=True)
class Salary:
    """A salary exactly as advertised by hh.ru.

    ``from_`` is used because ``from`` is a Python keyword.  ``gross`` is
    three-valued on purpose: ``None`` means hh.ru did not say, which is not the
    same as "net" or "gross".
    """

    from_: int | None
    to: int | None
    currency: str | None
    gross: bool | None

    @classmethod
    def from_hh(cls, raw: Any) -> Salary | None:
        """Parse a ``salary`` object, or ``None`` when hh.ru sent null."""

        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise DataError("salary is neither an object nor null")

        def amount(key: str) -> int | None:
            value = raw.get(key)
            if value is None:
                return None
            if isinstance(value, bool):
                raise DataError(f"salary.{key} is a boolean, expected an integer")
            try:
                return int(value)
            except (TypeError, ValueError):
                raise DataError(f"salary.{key} is not an integer") from None

        currency_raw = raw.get("currency")
        currency = str(currency_raw).strip().upper() if currency_raw else None
        gross_raw = raw.get("gross")
        gross = gross_raw if isinstance(gross_raw, bool) else None

        return cls(
            from_=amount("from"),
            to=amount("to"),
            currency=currency,
            gross=gross,
        )


def _detect_remote(raw: Mapping[str, Any]) -> bool:
    """Detect remote work across the hh.ru fields that have carried it.

    Older responses expose ``schedule: {"id": "remote"}``; newer ones may also
    carry ``work_format`` entries.  Both are checked so the fixture and live
    responses do not depend on one API generation.
    """

    schedule = raw.get("schedule")
    if isinstance(schedule, Mapping):
        schedule_id = str(schedule.get("id") or "").strip().casefold()
        schedule_name = str(schedule.get("name") or "").casefold()
        if schedule_id in _REMOTE_SCHEDULE_IDS or "удал" in schedule_name:
            return True

    work_formats = raw.get("work_format")
    if isinstance(work_formats, list):
        for item in work_formats:
            if not isinstance(item, Mapping):
                continue
            format_id = str(item.get("id") or "").strip()
            format_name = str(item.get("name") or "").casefold()
            if format_id in _REMOTE_WORK_FORMAT_IDS or "удал" in format_name:
                return True
    return False


def _join_snippet(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if not isinstance(raw, Mapping):
        return ""
    parts: list[str] = []
    for key in ("requirement", "responsibility"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return "\n\n".join(parts)


@dataclass(frozen=True)
class Vacancy:
    """A single vacancy, normalised from the hh.ru search response."""

    id: str
    title: str
    employer: str | None
    area_id: str | None
    area_name: str
    salary: Salary | None
    published_at: datetime | None
    url: str | None
    snippet: str
    remote: bool

    @classmethod
    def from_hh(cls, raw: Any) -> Vacancy:
        if not isinstance(raw, Mapping):
            raise DataError("vacancy item is not a JSON object")

        vacancy_id = raw.get("id")
        if vacancy_id is None:
            raise DataError("vacancy item is missing its id")
        title = _clean_text(raw.get("name"))
        if title is None:
            raise DataError(f"vacancy {vacancy_id} is missing its name")

        employer_raw = raw.get("employer")
        employer: str | None = None
        if isinstance(employer_raw, Mapping):
            employer = _clean_text(employer_raw.get("name"))
        elif employer_raw is not None:
            raise DataError(f"vacancy {vacancy_id} has a malformed employer")

        area_raw = raw.get("area")
        area_id: str | None = None
        area_name = "Unknown"
        if isinstance(area_raw, Mapping):
            if area_raw.get("id") is not None:
                area_id = str(area_raw["id"])
            area_name = _clean_text(area_raw.get("name")) or "Unknown"
        elif area_raw is not None:
            raise DataError(f"vacancy {vacancy_id} has a malformed area")

        salary_raw = raw.get("salary")
        if salary_raw is None:
            salary = None
        elif isinstance(salary_raw, Mapping):
            salary = Salary.from_hh(salary_raw)
        else:
            raise DataError(f"vacancy {vacancy_id} has a malformed salary")

        url_raw = raw.get("alternate_url") or raw.get("url")
        url = url_raw.strip() if isinstance(url_raw, str) and url_raw.strip() else None

        return cls(
            id=str(vacancy_id),
            title=title,
            employer=employer,
            area_id=area_id,
            area_name=area_name,
            salary=salary,
            published_at=_parse_datetime(raw.get("published_at")),
            url=url,
            snippet=_join_snippet(raw.get("snippet")),
            remote=_detect_remote(raw),
        )

    @property
    def is_anonymous(self) -> bool:
        """True when no usable employer name is attached to the vacancy."""

        if self.employer is None:
            return True
        name = self.employer.strip()
        if not name:
            return True
        return name.casefold() in ANONYMOUS_EMPLOYER_NAMES


@dataclass(frozen=True)
class Query:
    """Search parameters plus the preferences used for scoring.

    ``text`` and ``area_id`` are server-side search parameters when talking to
    hh.ru.  The remaining fields only affect local filtering and scoring.
    """

    text: str | None = None
    area_id: int | None = None
    min_salary: int | None = None
    currency: str = "RUB"
    keywords: tuple[str, ...] = ()
    remote: bool = False
    remote_only: bool = False
    top: int = 10
    per_page: int = 50

    def __post_init__(self) -> None:
        if self.text is not None:
            text = self.text.strip()
            object.__setattr__(self, "text", text or None)
        if self.area_id is not None and self.area_id < 1:
            raise ValueError("area id must be a positive integer")
        if self.min_salary is not None and self.min_salary < 0:
            raise ValueError("minimum salary must be zero or greater")
        if self.top < 1:
            raise ValueError("--top must be at least 1")
        if not 1 <= self.per_page <= 100:
            raise ValueError("--per-page must be between 1 and 100")

        currency = self.currency.strip().upper()
        if not _CURRENCY_RE.match(currency):
            raise ValueError("currency must be a three-letter code such as RUB")
        object.__setattr__(self, "currency", currency)

        cleaned: list[str] = []
        seen: set[str] = set()
        for keyword in self.keywords:
            term = keyword.strip()
            if not term:
                continue
            folded = term.casefold()
            if folded not in seen:
                seen.add(folded)
                cleaned.append(term)
        object.__setattr__(self, "keywords", tuple(cleaned))


@dataclass(frozen=True)
class Score:
    """Weighted score with the full per-component breakdown.

    ``total`` is on a 0..100 scale.  ``components`` are each on 0..1;
    ``weights`` are the raw configured weights (not renormalised for inactive
    components); ``contributions`` are percentage points and sum to ``total``.
    """

    total: float
    components: Mapping[str, float]
    weights: Mapping[str, float]
    contributions: Mapping[str, float]
    notes: Mapping[str, str]
