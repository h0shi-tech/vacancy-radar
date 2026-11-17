# vacancy-radar

[![CI](https://img.shields.io/github/actions/workflow/status/h0shi-tech/vacancy-radar/ci.yml?branch=main)](https://github.com/h0shi-tech/vacancy-radar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Zero dependencies](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

`vacancy-radar` is a command-line tool that pulls job vacancies from the public
hh.ru API, scores them against your salary floor, keywords, remote preference
and recency, and prints a ranked shortlist.

## Why

hh.ru returns a lot of results, and the ordering that is best for the site is
not necessarily the ordering that is best for you. Checking salary, location,
stack and freshness by hand for every search is repetitive. `vacancy-radar`
makes those preferences explicit, applies them consistently, and can explain
every rank it produces.

## Quick start

Requires Python 3.11+ and nothing else.

```bash
git clone https://github.com/h0shi-tech/vacancy-radar.git
cd vacancy-radar
python3 -m vacancy_radar --offline fixtures/vacancies.json --top 5
```

```text
#  SCORE  TITLE                         EMPLOYER            SALARY (NET)             AREA             AGE  FLAGS
-  -----  ----------------------------  ------------------  -----------------------  ---------------  ---  ----------------------
1   82.9  Senior Python-разработчик     ООО «Кодовая база»  208 800–278 400 RUB net  Москва           4h   —
2   82.3  Python-разработчик (FastAPI)  DataCraft           156 600–217 500 RUB net  Санкт-Петербург  12h  remote
3   78.3  Data Engineer (Python)        Fintech Lab         220 000–280 000 RUB net  Москва           3d   —
4   75.5  Backend-разработчик Python    АО «Волга-Телеком»  from 130 500 RUB net     Самара           1d   remote, partial_salary
5   74.0  ML Engineer (Python)          Нейро-Софт          from 217 500 RUB net     Москва           5d   partial_salary
```

This output was captured on 2026-11-17. Ages and recency scores depend on the
current clock, so a later run will show different `AGE` values and may reorder
close scores; the table format is stable. Offline replay performs no network
requests. The bundled fixture is synthetic — its companies and vacancies are
invented — but it uses the documented hh.ru search-response shape.

A live search looks like this. It needs network access, requires no API key,
and defaults to Moscow (area id 1):

```bash
python3 -m vacancy_radar "python разработчик" --area moscow \
  --min-salary 200000 --keyword python --keyword fastapi --remote --top 10
```

One more example, with a machine-readable score breakdown:

```bash
python3 -m vacancy_radar "data engineer" --format json --explain --top 3
```

## Installation

Running from the checked-out repository is enough. To install a `vacancy-radar`
console script instead:

```bash
python3 -m pip install -e .
vacancy-radar --offline fixtures/vacancies.json --top 5
```

There are no runtime dependencies. `pip` only needs its normal build tooling.

## Usage

```text
vacancy-radar [TEXT] [options]
```

`TEXT` and `--query TEXT` are interchangeable; passing both is allowed only
when they are identical. A search text is required for live searches. Offline
replay can run without one.

| Option | Meaning |
| --- | --- |
| `--area NAME` | City alias: `moscow`, `spb`, `yekaterinburg`, `novosibirsk`, `samara`, `kazan`, `russia`. Live searches default to Moscow. |
| `--area-id ID` | Any numeric hh.ru area id, for regions not in the built-in table. |
| `--min-salary N` | Net salary floor in `--currency` (default: no floor). |
| `--currency CODE` | Currency for the floor and comparisons. Supported: RUB, USD, EUR, KZT, BYN, GEL. Default: RUB. |
| `--keyword WORD` | Preference keyword. Repeat the flag or separate words with commas. Matching is case-insensitive substring matching. |
| `--remote` | Adds the remote-work bonus to scoring. This is a preference, not a filter. |
| `--remote-only` | Hard filter that discards non-remote vacancies. |
| `--top N` | Number of ranked rows to print (default 10). |
| `--format {table,md,json}` | Output format (default `table`). |
| `--explain` | Show per-component score breakdowns in table/markdown output, or an `explain` object in JSON. |
| `--color {auto,always,never}` | ANSI colour for tables. `auto` enables it only on a TTY (and respects `NO_COLOR`). |
| `--offline PATH` | Replay a recorded-shape hh.ru JSON response instead of using the network. |
| `--per-page N` | Live API page size, 1–100 (default 50). |
| `--timeout SECONDS` | HTTP timeout per attempt (default 10). |
| `--no-cache` | Do not read or write the live-response cache. |
| `--cache-ttl SECONDS` | Live-response cache TTL (default 300). |

`--offline` semantics are deliberately simple: the fixture is treated as the
server's response. It is not filtered by area unless you pass `--area` or
`--area-id`, and `--per-page` does not truncate it. A supplied text query is
approximated locally with a case-insensitive substring search over title,
snippet and employer — this is not hh.ru's search grammar.

### Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | At least one ranked result was printed. |
| `3` | The search ran, but no vacancies survived the filters. JSON mode still prints a valid `results: []` document. |
| `1` | Usage error, network/API failure, unreadable fixture, or invalid fixture data. |

Expected errors go to stderr with a short message; the CLI does not print
tracebacks for them.

## How scoring works

Every component is on a 0–1 scale. The final score is a weighted sum rescaled
to 0–100:

```text
total = 100 × Σ(component_i × weight_i) / Σ(weight_i)
```

| Component | Weight | What drives it |
| --- | ---: | --- |
| Salary fit | 0.35 | Net salary relative to the floor or a reference level. |
| Keyword hits | 0.30 | Coverage of `--keyword` terms; title hits count twice. |
| Recency | 0.20 | Exponential decay from `published_at`, 7-day half-life. |
| Remote | 0.10 | 1 for remote, 0 otherwise. Included **only** when `--remote` is passed. |
| Employer | 0.05 | 1 for a named employer, 0 for anonymous/missing. |

Without `--remote`, the active weights sum to 0.90 and are renormalised by that
sum, so the total remains comparable across runs. `--format json --explain`
shows `components`, the active `weights`, the resulting `contributions` in
percentage points (they sum to `total`), and a note explaining each component.

### Salary fit

hh.ru reports `salary: {from, to, currency, gross}`. Values missing on the wire
stay unknown — neither bound is ever treated as zero.

- If `gross` is `true`, both bounds are multiplied by **0.87** to estimate net.
- If `gross` is `false`, the amounts are used as net.
- If `gross` is null, amounts are treated as net and the vacancy is flagged
  `gross_unknown`.
- The comparison amount is the net `from` when known, otherwise the net `to`.
  A `to`-only salary is therefore an upper-bound estimate and is flagged
  `partial_salary`.
- With `--min-salary`, the target is `1.5 × min_salary`, so salary fit grows
  until the salary reaches 1.5× the floor and then saturates at 1.0.
- Without a floor, the target is an illustrative per-currency reference level:
  RUB 150,000; USD 2,000; EUR 1,800; KZT 750,000; BYN 5,000; GEL 4,000.

Salaries with both bounds missing get a salary component of 0.35, not 0. A
salary in a different currency, or with no currency, gets 0.15 because it
cannot be verified against the requested floor. The missing/mismatch cases are
kept in the shortlist and flagged; only a known same-currency amount below the
floor is filtered out.

### Keyword hits

```text
(2 × title_hits + snippet_hits) / (2 × keyword_count)
```

Each configured keyword can contribute once, at most. A keyword found in the
title is not counted again in the snippet. With no keywords configured the
component is neutral (0.50). Matching is plain case-insensitive substring
matching: there is no stemming or Russian morphology. So `python` matches
`Python-разработчик`, but `команда` does not match `команды`; conversely, a
short keyword such as `go` can match unrelated words such as `google`.

### Recency

```text
recency = 0.5 ^ (age_in_days / 7)
```

Future publication dates are clamped to age zero; a missing date scores 0.10
rather than pretending the vacancy is new.

### Remote and employer

The remote component exists only when you ask for it with `--remote`; remote
vacancies then receive the full 0.10. `--remote-only` is the separate hard
filter. The employer component gives a small advantage to named employers and
penalises anonymous postings, but anonymity is not a hard filter.

## Design decisions

**Gross → net uses a flat 0.87 factor.** hh.ru's `gross` flag says whether the
advertised amount is before or after income tax. The tool estimates net pay by
applying the common 13% personal income tax rate. Real Russian taxation is
progressive and depends on annual income, so 0.87 is a documented
simplification, not tax advice. It is a named constant in `scoring.py`.

**No currency conversion.** FX rates move, and silently converting a USD or KZT
salary into RUB would manufacture precision the data does not have. The tool
compares only salaries in the requested `--currency`; other currencies are
marked `currency_mismatch`, salaries with no currency are marked
`currency_unknown`, and the original amounts remain visible. Use `--currency`
to change the comparison base.

**Standard library only.** `urllib.request` handles HTTP, `argparse` handles the
CLI, `unittest` handles the suite. A zero-dependency tool is easy to audit, run
from a checkout, and install in a restricted environment. The trade-off is that
table rendering, retries and links are implemented in this repository rather
than delegated to convenience packages.

**Conservative salary filtering.** A known same-currency amount below the
floor is dropped. Unknown salaries and currencies that cannot be compared are
kept and flagged, because dropping them would silently hide potentially
relevant vacancies. The table and JSON always expose those flags.

**HTTP retry policy.** Two retries (three attempts total) on HTTP 429 and 5xx,
with 0.5s and 1.0s exponential backoff. A valid `Retry-After` header is honoured
and capped at 30 seconds so a broken server cannot stall the CLI indefinitely.
Requests use a real `User-Agent`, a 10-second default timeout, and never include
the raw response body in error messages. Connection-level failures are not
retried; they surface immediately as an error.

**Single-page fetch, optional cache.** Each live run asks for one page of up to
100 results. Successful payloads are cached in JSON under
`$XDG_CACHE_HOME/vacancy-radar` (falling back to `~/.cache/vacancy-radar`) for
five minutes by default. Caching is best-effort and can be disabled with
`--no-cache`. Fetching further pages is on the roadmap.

**Offline-first demo.** The bundled fixture is synthetic but shaped like an
hh.ru search response, including nested `salary`, `employer`, `snippet`,
`schedule`, `work_format` and area objects. Tests and the demo use it with zero
network access. A plain `--offline` replay shows every fixture row regardless
of the live default area; pass an area explicitly to filter the replay.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests are standard-library `unittest` and never touch the network. Retry
logic, parsing and the CLI are exercised through injected fake transports; the
offline CLI test injects a transport that raises if it is called. The suite
covers gross/net salary normalisation, unknown bounds, currency mismatch,
scoring monotonicity, ranking, Cyrillic table alignment, markdown escaping,
stable JSON shape, CLI exit codes and cache TTL.

## Roadmap

- [ ] Fetch additional pages when `--top` exceeds `--per-page`.
- [ ] Save repeated preferences in an optional TOML profile.
- [ ] Make salary anchors and scoring weights configurable per profile.

## License

MIT — see [LICENSE](LICENSE). Created by Vlad Nikishin.
