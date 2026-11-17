"""Shared, dependency-free test helpers.

All HTTP is fake by construction.  ``ExplodingTransport`` is used in offline
tests so an accidental network call fails loudly rather than opening a socket.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vacancy_radar.errors import HHNetworkError
from vacancy_radar.hh_client import HttpRequest, HttpResponse
from vacancy_radar.models import Salary, Vacancy

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "fixtures" / "vacancies.json"
FIXED_NOW = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)


def fixture_payload() -> dict[str, Any]:
    with FIXTURE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def fixture_body() -> bytes:
    return json.dumps(fixture_payload(), ensure_ascii=False).encode("utf-8")


def make_salary(
    from_: int | None = 150_000,
    to: int | None = 200_000,
    currency: str | None = "RUB",
    gross: bool | None = False,
) -> Salary:
    return Salary(from_=from_, to=to, currency=currency, gross=gross)


def make_vacancy(
    *,
    vacancy_id: str = "1",
    title: str = "Python-разработчик",
    employer: str | None = "Acme",
    area_id: str | None = "1",
    area_name: str = "Москва",
    salary: Salary | None = None,
    published_at: datetime | None = None,
    url: str | None = "https://hh.ru/vacancy/1",
    snippet: str = "Python, FastAPI, PostgreSQL",
    remote: bool = False,
) -> Vacancy:
    return Vacancy(
        id=vacancy_id,
        title=title,
        employer=employer,
        area_id=area_id,
        area_name=area_name,
        salary=make_salary() if salary is None else salary,
        published_at=(
            FIXED_NOW - timedelta(days=1)
            if published_at is None
            else published_at
        ),
        url=url,
        snippet=snippet,
        remote=remote,
    )


def http_response(
    status: int = 200,
    payload: dict[str, Any] | None = None,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> HttpResponse:
    if body is None and payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return HttpResponse(
        status=status,
        headers=headers or {},
        body=body if body is not None else b"",
    )


class FakeTransport:
    """Return queued responses; record every request."""

    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self._responses = list(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("fake transport ran out of responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StaticTransport:
    """Always return the same 200 response and count calls."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return HttpResponse(status=200, headers={}, body=self._body)


class NetworkFailureTransport:
    """Raise a network error on every call; records the attempt."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, request: HttpRequest) -> HttpResponse:
        self.calls += 1
        raise HHNetworkError("simulated connection failure")


class ExplodingTransport:
    """Fail the test immediately if a network call is attempted."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, request: HttpRequest) -> HttpResponse:
        self.calls += 1
        raise AssertionError("network access was attempted in a test")
