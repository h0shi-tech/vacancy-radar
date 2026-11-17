"""Pure scoring and filtering logic.

The score is a weighted sum of five components, each on 0..1:

======== ====== ==========================================================
salary   0.35   net salary vs. the floor (or a reference level)
keywords 0.30   keyword coverage; title matches count twice
recency  0.20   exponential decay with a 7-day half-life
remote   0.10   bonus, included only when the user asked with ``--remote``
employer 0.05   1.0 for a named employer, 0.0 for anonymous postings
======== ====== ==========================================================

The active weights are renormalised, so a query without ``--remote`` divides
by 0.90 rather than 1.0 and the total stays on a 0..100 scale.

Salary handling is intentionally conservative:

* gross amounts are converted to net with the flat 13% assumption ``× 0.87``;
* a missing ``from`` or ``to`` stays unknown (``None``), never zero;
* a different currency is *not* converted.  There are no FX rates in this
  tool, and silently inventing one would make the ranking dishonest;
* when a floor is configured, only vacancies whose known amount is in the
  requested currency are filtered.  Unknown and mismatched salaries are kept,
  flagged, and scored lower.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Query, Salary, Score, Vacancy, ensure_aware

#: Flat gross -> net assumption for Russian personal income tax.  This is a
#: simplification: the actual rate is progressive and depends on annual income.
GROSS_TO_NET = 0.87

RECENCY_HALF_LIFE_DAYS = 7.0
SALARY_HEADROOM_FACTOR = 1.5

SALARY_MISSING_FIT = 0.35
SALARY_CURRENCY_UNKNOWN_FIT = 0.15
SALARY_CURRENCY_MISMATCH_FIT = 0.15
SALARY_UNREFERENCED_FIT = 0.50
KEYWORD_NEUTRAL_FIT = 0.50
RECENCY_UNKNOWN_FIT = 0.10

#: Illustrative reference levels for salary when ``--min-salary`` is absent.
#: These are not exchange rates and are never used to compare currencies.
DEFAULT_SALARY_ANCHORS: dict[str, float] = {
    "RUB": 150_000.0,
    "USD": 2_000.0,
    "EUR": 1_800.0,
    "KZT": 750_000.0,
    "BYN": 5_000.0,
    "GEL": 4_000.0,
}
SUPPORTED_CURRENCIES: tuple[str, ...] = tuple(DEFAULT_SALARY_ANCHORS)

COMPONENT_WEIGHTS: dict[str, float] = {
    "salary": 0.35,
    "keywords": 0.30,
    "recency": 0.20,
    "remote": 0.10,
    "employer": 0.05,
}

SALARY_STATUS_UNKNOWN = "unknown"
SALARY_STATUS_CURRENCY_UNKNOWN = "currency_unknown"
SALARY_STATUS_CURRENCY_MISMATCH = "currency_mismatch"
SALARY_STATUS_KNOWN = "known"

FLAG_ORDER: tuple[str, ...] = (
    "remote",
    "anonymous",
    "salary_unknown",
    "currency_mismatch",
    "currency_unknown",
    "partial_salary",
    "gross_unknown",
)

Ranked = tuple[Vacancy, Score]


@dataclass(frozen=True)
class SalaryAssessment:
    """The result of normalising and comparing one salary range."""

    status: str
    net_from: float | None
    net_to: float | None
    currency: str | None
    used_amount: float | None
    used_bound: str | None
    fit: float
    note: str


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def normalize_salary(salary: Salary | None) -> tuple[float | None, float | None]:
    """Return ``(net_from, net_to)`` after applying the gross->net assumption.

    Only ``gross is True`` triggers conversion.  A missing gross flag is treated
    as net, because inventing a deduction would be worse than showing the
    advertised number; the vacancy is flagged ``gross_unknown`` in that case.
    """

    if salary is None:
        return None, None
    factor = GROSS_TO_NET if salary.gross is True else 1.0
    net_from = salary.from_ * factor if salary.from_ is not None else None
    net_to = salary.to * factor if salary.to is not None else None
    return net_from, net_to


def assess_salary(
    salary: Salary | None,
    min_salary: int | None,
    currency: str,
) -> SalaryAssessment:
    """Normalise a salary and score how well it fits the requested floor.

    A salary with both bounds missing is ``unknown``.  If a currency is present
    but differs from the requested one, the assessment is
    ``currency_mismatch``: net bounds are still shown in the original currency,
    but no comparison against the floor is made.
    """

    requested = currency.strip().upper()
    net_from, net_to = normalize_salary(salary)

    if salary is None or (salary.from_ is None and salary.to is None):
        return SalaryAssessment(
            status=SALARY_STATUS_UNKNOWN,
            net_from=net_from,
            net_to=net_to,
            currency=salary.currency if salary else None,
            used_amount=None,
            used_bound=None,
            fit=SALARY_MISSING_FIT,
            note="no salary listed; an unknown salary is not treated as zero",
        )

    if salary.gross is True:
        conversion = (
            f"gross converted to net at ×{GROSS_TO_NET:g} "
            "(flat 13% tax assumption)"
        )
    elif salary.gross is False:
        conversion = "salary reported as net"
    else:
        conversion = "gross/net flag missing; amounts treated as net"

    salary_currency = salary.currency.upper() if salary.currency else None
    if salary_currency is None:
        return SalaryAssessment(
            status=SALARY_STATUS_CURRENCY_UNKNOWN,
            net_from=net_from,
            net_to=net_to,
            currency=None,
            used_amount=None,
            used_bound=None,
            fit=SALARY_CURRENCY_UNKNOWN_FIT,
            note=(
                f"{conversion}; the salary currency is missing, so it cannot "
                f"be compared with {requested}"
            ),
        )

    if salary_currency != requested:
        return SalaryAssessment(
            status=SALARY_STATUS_CURRENCY_MISMATCH,
            net_from=net_from,
            net_to=net_to,
            currency=salary_currency,
            used_amount=None,
            used_bound=None,
            fit=SALARY_CURRENCY_MISMATCH_FIT,
            note=(
                f"{conversion}; the salary is in {salary_currency}, not "
                f"{requested}; no FX conversion is attempted"
            ),
        )

    used_amount = net_from if net_from is not None else net_to
    used_bound = "from" if net_from is not None else "to"
    assert used_amount is not None  # both bounds were checked above

    if min_salary:
        target = max(float(min_salary) * SALARY_HEADROOM_FACTOR, 1.0)
        fit = min(1.0, max(0.0, used_amount / target))
        relation = "at or above" if used_amount >= min_salary else "below"
        basis = (
            f"{_money(used_amount)} {requested} net ({used_bound} bound) is "
            f"{relation} the {_money(min_salary)} {requested} floor"
        )
    else:
        anchor = DEFAULT_SALARY_ANCHORS.get(requested)
        if anchor:
            fit = min(1.0, max(0.0, used_amount / anchor))
            basis = (
                f"{_money(used_amount)} {requested} net ({used_bound} bound) "
                f"against the {_money(anchor)} {requested} reference level"
            )
        else:
            fit = SALARY_UNREFERENCED_FIT
            basis = (
                f"no reference salary is configured for {requested}; "
                "the salary component is neutral"
            )

    note = f"{conversion}; {basis}"
    if used_bound == "to":
        note += (
            "; only the upper bound is known, so it is used as the best "
            "available estimate"
        )
    if salary.gross is None:
        note += "; the gross/net flag is missing"

    return SalaryAssessment(
        status=SALARY_STATUS_KNOWN,
        net_from=net_from,
        net_to=net_to,
        currency=salary_currency,
        used_amount=used_amount,
        used_bound=used_bound,
        fit=fit,
        note=note,
    )


def keyword_fit(vacancy: Vacancy, keywords: tuple[str, ...]) -> tuple[float, str]:
    """Score keyword coverage, weighting title hits twice as much as snippets."""

    terms: list[str] = []
    seen: set[str] = set()
    for raw in keywords:
        term = raw.strip().casefold()
        if term and term not in seen:
            seen.add(term)
            terms.append(term)

    if not terms:
        return (
            KEYWORD_NEUTRAL_FIT,
            "no keywords configured; the keyword component is neutral",
        )

    title = vacancy.title.casefold()
    snippet = vacancy.snippet.casefold()
    title_hits = [term for term in terms if term in title]
    snippet_hits = [
        term for term in terms if term not in title and term in snippet
    ]
    matched = len(title_hits) + len(snippet_hits)
    fit = min(
        1.0,
        (2 * len(title_hits) + len(snippet_hits)) / (2 * len(terms)),
    )
    note = (
        f"{matched}/{len(terms)} keyword(s) matched "
        f"({len(title_hits)} in the title, {len(snippet_hits)} in the snippet); "
        "title hits count twice"
    )
    return fit, note


def age_days(published_at: datetime | None, now: datetime | None = None) -> float | None:
    """Return the non-negative age in days, or ``None`` if the date is unknown."""

    if published_at is None:
        return None
    current = ensure_aware(now)
    published = ensure_aware(published_at)
    seconds = (current - published).total_seconds()
    return max(0.0, seconds / 86_400.0)


def recency_fit(
    published_at: datetime | None,
    now: datetime | None = None,
) -> tuple[float, str]:
    """Exponential freshness decay with a 7-day half-life."""

    age = age_days(published_at, now)
    if age is None:
        return (
            RECENCY_UNKNOWN_FIT,
            "publication date missing; recency is scored low, not as brand new",
        )
    fit = 0.5 ** (age / RECENCY_HALF_LIFE_DAYS)
    note = (
        f"published {age:.1f} day(s) ago; a "
        f"{RECENCY_HALF_LIFE_DAYS:g}-day half-life gives {fit:.2f}"
    )
    return fit, note


def employer_fit(vacancy: Vacancy) -> tuple[float, str]:
    if vacancy.is_anonymous:
        return 0.0, "anonymous or missing employer; the small employer weight is lost"
    return 1.0, f"named employer ({vacancy.employer})"


def vacancy_flags(vacancy: Vacancy, currency: str) -> tuple[str, ...]:
    """Return the stable, human-readable flags for one vacancy."""

    requested = currency.strip().upper()
    flags: set[str] = set()
    if vacancy.remote:
        flags.add("remote")
    if vacancy.is_anonymous:
        flags.add("anonymous")

    salary = vacancy.salary
    if salary is None or (salary.from_ is None and salary.to is None):
        flags.add("salary_unknown")
    else:
        salary_currency = salary.currency.upper() if salary.currency else None
        if salary_currency is None:
            flags.add("currency_unknown")
        elif salary_currency != requested:
            flags.add("currency_mismatch")
        if (salary.from_ is None) != (salary.to is None):
            flags.add("partial_salary")
        if salary.gross is None:
            flags.add("gross_unknown")

    return tuple(flag for flag in FLAG_ORDER if flag in flags)


def passes_salary_floor(vacancy: Vacancy, query: Query) -> bool:
    """Whether a vacancy is allowed through the salary-floor filter.

    Unknown salaries and salary currencies that differ from the requested one
    pass the filter on purpose: the floor cannot be verified, so the vacancy is
    surfaced with a flag instead of being silently discarded.
    """

    if not query.min_salary:
        return True
    assessment = assess_salary(vacancy.salary, query.min_salary, query.currency)
    if assessment.status != SALARY_STATUS_KNOWN or assessment.used_amount is None:
        return True
    return assessment.used_amount >= query.min_salary


def filter_vacancies(vacancies: list[Vacancy], query: Query) -> list[Vacancy]:
    """Apply the local hard filters (remote-only and verifiable salary floor)."""

    return [
        vacancy
        for vacancy in vacancies
        if (not query.remote_only or vacancy.remote)
        and passes_salary_floor(vacancy, query)
    ]


def score_vacancy(vacancy: Vacancy, query: Query, now: datetime | None = None) -> Score:
    """Compute the weighted score and its per-component explanation."""

    current = ensure_aware(now)
    assessment = assess_salary(vacancy.salary, query.min_salary, query.currency)
    keyword_score, keyword_note = keyword_fit(vacancy, query.keywords)
    recency_score, recency_note = recency_fit(vacancy.published_at, current)
    employer_score, employer_note = employer_fit(vacancy)

    components: dict[str, float] = {
        "salary": assessment.fit,
        "keywords": keyword_score,
        "recency": recency_score,
    }
    notes: dict[str, str] = {
        "salary": assessment.note,
        "keywords": keyword_note,
        "recency": recency_note,
    }

    if query.remote:
        components["remote"] = 1.0 if vacancy.remote else 0.0
        notes["remote"] = (
            "remote position; the remote bonus is applied"
            if vacancy.remote
            else "not remote; no remote bonus"
        )

    components["employer"] = employer_score
    notes["employer"] = employer_note

    weights = {name: COMPONENT_WEIGHTS[name] for name in components}
    total_weight = sum(weights.values())
    contributions = {
        name: components[name] * weights[name] / total_weight * 100.0
        for name in components
    }
    total = sum(contributions.values())

    return Score(
        total=total,
        components=components,
        weights=weights,
        contributions=contributions,
        notes=notes,
    )


def rank_vacancies(
    vacancies: list[Vacancy],
    query: Query,
    now: datetime | None = None,
) -> list[Ranked]:
    """Filter, score, sort (score desc, then newer, then id) and cut to top N."""

    current = ensure_aware(now)
    kept = filter_vacancies(vacancies, query)
    scored: list[Ranked] = [
        (vacancy, score_vacancy(vacancy, query, current)) for vacancy in kept
    ]

    def sort_key(item: Ranked) -> tuple[float, float, str]:
        vacancy, score = item
        timestamp = (
            ensure_aware(vacancy.published_at).timestamp()
            if vacancy.published_at is not None
            else float("-inf")
        )
        return (-score.total, -timestamp, vacancy.id)

    scored.sort(key=sort_key)
    return scored[: query.top]
