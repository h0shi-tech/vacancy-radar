"""Output renderers: aligned table, GFM markdown, and stable JSON.

Column widths are calculated with ``len()``, which counts Unicode code points.
For Cyrillic in a monospaced terminal one code point is one cell, so the table
lines up; this is intentional.  It would *not* be correct for East Asian wide
characters or combining marks -- those require a wcwidth implementation, which
this dependency-free tool does not include.
"""

from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import quote

from .models import Query, Score, Vacancy, ensure_aware
from .scoring import Ranked, age_days, assess_salary, vacancy_flags

TABLE_HEADERS: tuple[str, ...] = (
    "#",
    "SCORE",
    "TITLE",
    "EMPLOYER",
    "SALARY (NET)",
    "AREA",
    "AGE",
    "FLAGS",
)
RIGHT_ALIGNED = frozenset({"#", "SCORE"})
MAX_COLUMN_WIDTHS = {"TITLE": 48, "EMPLOYER": 30}

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def _format_salary(vacancy: Vacancy, query: Query) -> str:
    assessment = assess_salary(vacancy.salary, query.min_salary, query.currency)
    if assessment.net_from is None and assessment.net_to is None:
        return "—"
    currency = assessment.currency or "?"
    if assessment.net_from is not None and assessment.net_to is not None:
        amount = f"{_money(assessment.net_from)}–{_money(assessment.net_to)}"
    elif assessment.net_from is not None:
        amount = f"from {_money(assessment.net_from)}"
    else:
        amount = f"up to {_money(assessment.net_to or 0)}"
    # The suffix is "net" even when gross is unknown; the flag column then
    # carries the gross_unknown warning.
    return f"{amount} {currency} net"


def _format_age(vacancy: Vacancy, now: datetime) -> str:
    days = age_days(vacancy.published_at, now)
    if days is None:
        return "—"
    hours = days * 24
    if hours < 1:
        return "<1h"
    if hours < 24:
        return f"{int(hours)}h"
    if days < 7:
        return f"{days:.0f}d"
    if days < 30:
        return f"{days / 7:.0f}w"
    return f"{days / 30:.0f}mo"


def _format_flags(vacancy: Vacancy, query: Query) -> str:
    flags = vacancy_flags(vacancy, query.currency)
    return ", ".join(flags) if flags else "—"


def _clip(text: str, limit: int | None) -> str:
    if limit is None or len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _pad(text: str, width: int, right: bool) -> str:
    return text.rjust(width) if right else text.ljust(width)


def _paint(text: str, colour: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{colour}{text}{_RESET}"


def _score_colour(score: float) -> str:
    if score >= 70:
        return _GREEN
    if score >= 45:
        return _YELLOW
    return _RED


def _row_cells(
    rank: int,
    vacancy: Vacancy,
    score: Score,
    query: Query,
    now: datetime,
) -> list[str]:
    return [
        str(rank),
        f"{score.total:.1f}",
        _clip(vacancy.title, MAX_COLUMN_WIDTHS["TITLE"]),
        _clip(vacancy.employer or "—", MAX_COLUMN_WIDTHS["EMPLOYER"]),
        _format_salary(vacancy, query),
        vacancy.area_name,
        _format_age(vacancy, now),
        _format_flags(vacancy, query),
    ]


def _explain_lines(rows: list[Ranked]) -> list[str]:
    lines = ["", "Why these ranks:"]
    for index, (vacancy, score) in enumerate(rows, start=1):
        lines.append(f"{index}. {vacancy.title} — {score.total:.1f}/100")
        for name, weight in score.weights.items():
            lines.append(
                f"   {name:<9} score {score.components[name]:.2f} "
                f"× weight {weight:.2f} = {score.contributions[name]:5.1f} pts "
                f"— {score.notes.get(name, '')}"
            )
    return lines


def render_table(
    rows: list[Ranked],
    query: Query,
    *,
    now: datetime | None = None,
    color: bool = False,
    explain: bool = False,
) -> str:
    """Render a fixed-width table that aligns Cyrillic and ASCII alike."""

    current = ensure_aware(now)
    cells_by_row = [
        _row_cells(index, vacancy, score, query, current)
        for index, (vacancy, score) in enumerate(rows, start=1)
    ]

    widths: list[int] = []
    for column_index, header in enumerate(TABLE_HEADERS):
        candidates = [len(header)]
        candidates.extend(len(row[column_index]) for row in cells_by_row)
        widths.append(max(candidates))

    def format_line(cells: list[str]) -> str:
        return "  ".join(
            _pad(cell, width, header in RIGHT_ALIGNED)
            for cell, width, header in zip(cells, widths, TABLE_HEADERS)
        ).rstrip()

    header_line = format_line(list(TABLE_HEADERS))
    separator = "  ".join("-" * width for width in widths).rstrip()

    lines: list[str] = []
    if color:
        lines.append(_paint(header_line, _BOLD, True))
        lines.append(_paint(separator, _DIM, True))
    else:
        lines.append(header_line)
        lines.append(separator)

    for cells, (_, score) in zip(cells_by_row, rows):
        parts: list[str] = []
        for index, (cell, width, header) in enumerate(
            zip(cells, widths, TABLE_HEADERS)
        ):
            padded = _pad(cell, width, header in RIGHT_ALIGNED)
            if color and header == "SCORE":
                padded = _paint(padded, _score_colour(score.total), True)
            parts.append(padded)
        lines.append("  ".join(parts).rstrip())

    if explain:
        lines.extend(_explain_lines(rows))

    return "\n".join(lines) + "\n"


def _md_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _md_url(url: str) -> str:
    # Parentheses and spaces would otherwise terminate or break the link
    # destination in GFM; percent-encoding them is safe.
    return quote(url, safe=":/?#=&%+,;@!$'*[]")


def _vacancy_markdown(vacancy: Vacancy) -> str:
    label = _md_escape(vacancy.title)
    if vacancy.url:
        return f"[{label}]({_md_url(vacancy.url)})"
    return label


def _md_explain(rows: list[Ranked]) -> list[str]:
    lines = ["", "### Why these ranks", ""]
    for index, (vacancy, score) in enumerate(rows, start=1):
        lines.append(f"**{index}. {_md_escape(vacancy.title)}** — {score.total:.1f}/100")
        lines.append("")
        for name, weight in score.weights.items():
            note = _md_escape(score.notes.get(name, ""))
            lines.append(
                f"- `{name}` — {score.components[name]:.2f} × "
                f"{weight:.2f} = {score.contributions[name]:.1f} pts. {note}"
            )
        lines.append("")
    return lines


def render_markdown(
    rows: list[Ranked],
    query: Query,
    *,
    now: datetime | None = None,
    explain: bool = False,
) -> str:
    """Render valid GFM with a linked Vacancy column."""

    current = ensure_aware(now)
    lines = [
        "| # | Score | Vacancy | Employer | Salary (net) | Area | Age | Flags |",
        "| ---: | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for index, (vacancy, score) in enumerate(rows, start=1):
        cells = [
            str(index),
            f"{score.total:.1f}",
            _vacancy_markdown(vacancy),
            _md_escape(vacancy.employer or "—"),
            _md_escape(_format_salary(vacancy, query)),
            _md_escape(vacancy.area_name),
            _md_escape(_format_age(vacancy, current)),
            _md_escape(_format_flags(vacancy, query)),
        ]
        lines.append("| " + " | ".join(cells) + " |")

    if explain:
        lines.extend(_md_explain(rows))

    return "\n".join(lines).rstrip() + "\n"


def _salary_json(vacancy: Vacancy, query: Query) -> dict[str, object] | None:
    salary = vacancy.salary
    if salary is None:
        return None
    assessment = assess_salary(salary, query.min_salary, query.currency)
    return {
        "from": salary.from_,
        "to": salary.to,
        "currency": salary.currency,
        "gross": salary.gross,
        "net_from": (
            round(assessment.net_from, 2)
            if assessment.net_from is not None
            else None
        ),
        "net_to": (
            round(assessment.net_to, 2)
            if assessment.net_to is not None
            else None
        ),
        "status": assessment.status,
    }


def _explain_json(score: Score) -> dict[str, object]:
    return {
        "total": round(score.total, 2),
        "components": {name: round(value, 4) for name, value in score.components.items()},
        "weights": {name: round(value, 4) for name, value in score.weights.items()},
        "contributions": {
            name: round(value, 2) for name, value in score.contributions.items()
        },
        "notes": dict(score.notes),
    }


def vacancy_json(
    vacancy: Vacancy,
    score: Score,
    query: Query,
    *,
    explain: bool = False,
) -> dict[str, object]:
    """The stable per-vacancy JSON shape (also handy for library users)."""

    result: dict[str, object] = {
        "id": vacancy.id,
        "title": vacancy.title,
        "employer": vacancy.employer,
        "area": {"id": vacancy.area_id, "name": vacancy.area_name},
        "salary": _salary_json(vacancy, query),
        "remote": vacancy.remote,
        "published_at": (
            vacancy.published_at.isoformat() if vacancy.published_at else None
        ),
        "url": vacancy.url,
        "snippet": vacancy.snippet,
        "flags": list(vacancy_flags(vacancy, query.currency)),
        "score": round(score.total, 2),
    }
    if explain:
        result["explain"] = _explain_json(score)
    return result


def query_json(query: Query) -> dict[str, object]:
    return {
        "text": query.text,
        "area_id": query.area_id,
        "min_salary": query.min_salary,
        "currency": query.currency,
        "keywords": list(query.keywords),
        "remote": query.remote,
        "remote_only": query.remote_only,
        "top": query.top,
        "per_page": query.per_page,
    }


def render_json(
    rows: list[Ranked],
    query: Query,
    *,
    source: str,
    now: datetime | None = None,
    fetched: int,
    explain: bool = False,
) -> str:
    """Render the stable top-level JSON document."""

    current = ensure_aware(now)
    document = {
        "query": query_json(query),
        "source": source,
        "generated_at": current.isoformat(timespec="seconds"),
        "fetched": fetched,
        "count": len(rows),
        "results": [
            vacancy_json(vacancy, score, query, explain=explain)
            for vacancy, score in rows
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
