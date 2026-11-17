"""Command-line interface for vacancy-radar."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .areas import (
    AREA_BY_ID,
    DEFAULT_AREA_ID,
    RUSSIA_AREA_ID,
    UnknownAreaError,
    normalize_area_name,
    resolve_area,
)
from .cache import JsonCache
from .errors import DataError, HHApiError, RadarError
from .hh_client import HHClient, Transport, load_fixture, parse_vacancies
from .models import Query, Vacancy, ensure_aware
from .render import render_json, render_markdown, render_table
from .scoring import SUPPORTED_CURRENCIES, rank_vacancies

CACHE_TTL_SECONDS = 300.0


class UsageError(Exception):
    """A command-line combination that argparse cannot express by itself."""


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a number") from None
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite number zero or greater")
    return parsed


def _currency_code(value: str) -> str:
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise argparse.ArgumentTypeError("must be a three-letter currency code")
    if code not in SUPPORTED_CURRENCIES:
        supported = ", ".join(SUPPORTED_CURRENCIES)
        raise argparse.ArgumentTypeError(
            f"unsupported currency {code}; supported: {supported}"
        )
    return code


def _page_size(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 100:
        raise argparse.ArgumentTypeError("must be between 1 and 100")
    return parsed


class _Parser(argparse.ArgumentParser):
    """ArgumentParser that reports usage errors as exceptions.

    argparse's default ``error()`` calls ``sys.exit(2)``.  The required exit
    code for a usage error here is 1, and tests call ``main()`` in-process, so
    conversion to an exception is both clearer and testable.  ``--help`` still
    exits normally through SystemExit(0).
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="vacancy-radar",
        description=(
            "Search hh.ru vacancies and rank them against a salary floor, "
            "keywords, remote preference and recency."
        ),
        epilog=(
            "examples:\n"
            "  vacancy-radar --offline fixtures/vacancies.json --top 5\n"
            "  vacancy-radar \"python developer\" --area moscow "
            "--min-salary 200000 --keyword python --keyword fastapi --remote\n"
            "  vacancy-radar \"data engineer\" --format json --explain"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "text",
        nargs="?",
        metavar="TEXT",
        help="search text (same as --query; required for live searches)",
    )
    parser.add_argument(
        "--query",
        metavar="TEXT",
        help="search text, alternative to the positional TEXT",
    )
    parser.add_argument(
        "--area",
        metavar="NAME",
        help=(
            "city name such as moscow, spb or samara; live searches default "
            f"to Moscow (area {DEFAULT_AREA_ID})"
        ),
    )
    parser.add_argument(
        "--area-id",
        type=_positive_int,
        metavar="ID",
        help="hh.ru area id for any region not covered by --area",
    )
    parser.add_argument(
        "--min-salary",
        type=_non_negative_int,
        metavar="N",
        help="net salary floor in --currency (default: no floor)",
    )
    parser.add_argument(
        "--currency",
        type=_currency_code,
        default="RUB",
        metavar="CODE",
        help="salary currency for the floor and comparisons (default: RUB)",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        metavar="WORD",
        help="preference keyword; repeat the flag or use commas",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="add the remote-work bonus to scoring (not a hard filter)",
    )
    parser.add_argument(
        "--remote-only",
        action="store_true",
        help="discard non-remote vacancies",
    )
    parser.add_argument(
        "--top",
        type=_positive_int,
        default=10,
        metavar="N",
        help="number of ranked results to print (default: 10)",
    )
    parser.add_argument(
        "--format",
        choices=("table", "md", "json"),
        default="table",
        help="output format (default: table)",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="include per-component score breakdowns",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="ANSI colour for the table (default: auto, tty only)",
    )
    parser.add_argument(
        "--offline",
        metavar="PATH",
        help="replay a bundled/recorded hh.ru JSON fixture instead of the network",
    )
    parser.add_argument(
        "--per-page",
        type=_page_size,
        default=50,
        metavar="N",
        help="live API page size, 1..100 (default: 50)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=10.0,
        metavar="SECONDS",
        help="HTTP timeout per attempt (default: 10)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="do not read or write the live-response cache",
    )
    parser.add_argument(
        "--cache-ttl",
        type=_non_negative_float,
        default=CACHE_TTL_SECONDS,
        metavar="SECONDS",
        help=f"live-response cache TTL (default: {CACHE_TTL_SECONDS:g})",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def _parse_keywords(raw: list[str]) -> tuple[str, ...]:
    terms: list[str] = []
    for chunk in raw:
        for term in chunk.split(","):
            stripped = term.strip()
            if stripped:
                terms.append(stripped)
    return tuple(terms)


def _build_query(args: argparse.Namespace, *, offline: bool) -> Query:
    if args.text is not None and args.query is not None:
        if args.text.strip() != args.query.strip():
            raise UsageError("TEXT and --query contain different search texts")
    text = args.text if args.text is not None else args.query
    text = text.strip() if text is not None else ""
    if not text and not offline:
        raise UsageError(
            "a search text is required for live searches; pass TEXT or --query, "
            "or use --offline PATH"
        )

    if args.area is not None and args.area_id is not None:
        raise UsageError("use either --area or --area-id, not both")

    area_id: int | None
    if args.area is not None:
        try:
            area_id = resolve_area(args.area).id
        except UnknownAreaError as exc:
            raise UsageError(str(exc)) from None
    elif args.area_id is not None:
        area_id = args.area_id
    elif offline:
        # A fixture is a recorded search response.  Replaying it should not
        # silently discard non-Moscow rows unless the user asked for an area.
        area_id = None
    else:
        area_id = DEFAULT_AREA_ID

    try:
        return Query(
            text=text or None,
            area_id=area_id,
            min_salary=args.min_salary,
            currency=args.currency,
            keywords=_parse_keywords(args.keyword),
            remote=args.remote,
            remote_only=args.remote_only,
            top=args.top,
            per_page=args.per_page,
        )
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def _matches_area(vacancy: Vacancy, area_id: int) -> bool:
    if area_id == RUSSIA_AREA_ID:
        # A country-wide search returns vacancies whose own area is a child
        # city, not id 113.  The bundled fixture is a Russia-wide sample, so
        # the country alias intentionally disables the local area filter.
        return True
    if vacancy.area_id is not None:
        return vacancy.area_id == str(area_id)
    expected = AREA_BY_ID.get(area_id)
    if expected is None:
        return False
    return normalize_area_name(vacancy.area_name) == normalize_area_name(expected.name)


def _matches_text(vacancy: Vacancy, needle: str) -> bool:
    haystack = "\n".join(
        (vacancy.title, vacancy.snippet, vacancy.employer or "")
    ).casefold()
    return needle in haystack


def _filter_offline(vacancies: list[Vacancy], query: Query) -> list[Vacancy]:
    """Approximate hh.ru's server-side search on a replayed fixture.

    This is a case-insensitive substring search over title, snippet and
    employer -- honest locals, not hh.ru's search grammar.  Salary and
    remote-only filtering happen later, in :func:`scoring.rank_vacancies`.
    """

    filtered = vacancies
    if query.text:
        needle = query.text.casefold()
        filtered = [vacancy for vacancy in filtered if _matches_text(vacancy, needle)]
    if query.area_id is not None:
        filtered = [
            vacancy
            for vacancy in filtered
            if _matches_area(vacancy, query.area_id)
        ]
    return filtered


def _default_cache_dir() -> Path:
    override = os.environ.get("XDG_CACHE_HOME")
    base = Path(override).expanduser() if override else Path.home() / ".cache"
    return base / "vacancy-radar"


def _cache_key(query: Query, per_page: int) -> str:
    return json.dumps(
        {
            "version": __version__,
            "text": query.text,
            "area_id": query.area_id,
            "per_page": per_page,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _looks_like_payload(value: Any) -> bool:
    return isinstance(value, Mapping) and isinstance(value.get("items"), list)


def _search_live(
    args: argparse.Namespace,
    query: Query,
    transport: Transport | None,
    cache: JsonCache | None,
) -> list[Vacancy]:
    client = HHClient(transport=transport, timeout=args.timeout)
    store = None if args.no_cache else cache
    if store is None and not args.no_cache:
        store = JsonCache(_default_cache_dir(), ttl=args.cache_ttl)

    payload: Any = None
    if store is not None:
        key = _cache_key(query, args.per_page)
        payload = store.get(key)
        if not _looks_like_payload(payload):
            payload = None

    from_cache = payload is not None
    if payload is None:
        payload = client.fetch_page(query, args.per_page)

    try:
        vacancies = parse_vacancies(payload)
    except DataError as exc:
        raise HHApiError(f"could not parse the received vacancies: {exc}") from None

    # Only persist a payload that parsed successfully; otherwise a bad server
    # response would be replayed from the cache until its TTL expired.
    if store is not None and not from_cache:
        store.set(_cache_key(query, args.per_page), payload)
    return vacancies


def _color_enabled(mode: str, stream: TextIO) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    is_tty = getattr(stream, "isatty", lambda: False)()
    return bool(is_tty) and "NO_COLOR" not in os.environ


def _write(stream: TextIO, text: str) -> None:
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")


def _report_usage(parser: argparse.ArgumentParser, stream: TextIO, message: str) -> None:
    usage = parser.format_usage().rstrip()
    if usage:
        print(usage, file=stream)
    print(f"{parser.prog}: error: {message}", file=stream)
    print(f"Try '{parser.prog} --help' for usage.", file=stream)


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: Transport | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now: datetime | None = None,
    cache: JsonCache | None = None,
) -> int:
    """Run the CLI.  Returns a process exit code rather than calling exit().

    The keyword-only injection points exist so tests can avoid the network,
    filesystem cache and wall clock; ``python -m`` only uses ``argv``.
    """

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    parser = build_parser()

    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except UsageError as exc:
        _report_usage(parser, err, str(exc))
        return 1

    try:
        query = _build_query(args, offline=args.offline is not None)
    except UsageError as exc:
        _report_usage(parser, err, str(exc))
        return 1

    try:
        current = ensure_aware(now)
        if args.offline is not None:
            vacancies = _filter_offline(load_fixture(args.offline), query)
            source = f"offline:{args.offline}"
        else:
            vacancies = _search_live(args, query, transport, cache)
            source = "hh.ru"

        fetched = len(vacancies)
        rows = rank_vacancies(vacancies, query, now=current)

        if args.format == "json":
            _write(
                out,
                render_json(
                    rows,
                    query,
                    source=source,
                    now=current,
                    fetched=fetched,
                    explain=args.explain,
                ),
            )
        elif args.format == "md":
            if rows:
                _write(
                    out,
                    render_markdown(
                        rows, query, now=current, explain=args.explain
                    ),
                )
        elif rows:
            _write(
                out,
                render_table(
                    rows,
                    query,
                    now=current,
                    color=_color_enabled(args.color, out),
                    explain=args.explain,
                ),
            )

        if not rows:
            print(
                f"No vacancies matched your filters (fetched {fetched}).",
                file=err,
            )
            return 3
        return 0
    except RadarError as exc:
        print(f"{parser.prog}: error: {exc}", file=err)
        return 1


def entrypoint() -> int:
    """Console-script entry point declared in ``pyproject.toml``."""

    return main()
