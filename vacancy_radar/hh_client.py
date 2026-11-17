"""hh.ru API access with an injectable transport.

``urllib.request`` is the only HTTP implementation shipped, but nothing in the
client depends on it: tests inject a fake :class:`Transport`, so the suite can
assert that no socket is ever opened.

Retry policy: any 429 or 5xx response is retried up to two times (three
attempts total) with exponential backoff of 0.5s and 1.0s.  A valid
``Retry-After`` header overrides the backoff and is capped at 30 seconds, so a
hostile or confused server cannot block the CLI indefinitely.  Other status
codes fail immediately.  Response bodies are never included in error messages.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import __version__
from .errors import DataError, FixtureError, HHApiError, HHNetworkError
from .models import Query, Vacancy

API_BASE_URL = "https://api.hh.ru"
USER_AGENT = f"vacancy-radar/{__version__} (+https://github.com/h0shi-tech/vacancy-radar)"
DEFAULT_TIMEOUT = 10.0
MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 0.5
MAX_RETRY_AFTER_SECONDS = 30.0
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
RETRYABLE_STATUSES = frozenset({429})


@dataclass(frozen=True)
class HttpRequest:
    url: str
    headers: Mapping[str, str]
    timeout: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    """Minimal HTTP abstraction implemented by the real transport and fakes."""

    def send(self, request: HttpRequest) -> HttpResponse:  # pragma: no cover - protocol
        ...


class UrllibTransport:
    """Standard-library HTTP transport.

    HTTP error statuses are returned as normal :class:`HttpResponse` objects so
    the client can decide whether to retry.  Connection-level failures raise
    :class:`HHNetworkError`.
    """

    def send(self, request: HttpRequest) -> HttpResponse:
        outgoing = Request(
            request.url,
            headers=dict(request.headers),
            method="GET",
        )
        try:
            with urlopen(outgoing, timeout=request.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise HHNetworkError(
                        "hh.ru response exceeded the 10 MiB safety limit"
                    )
                headers = {
                    str(key).lower(): str(value)
                    for key, value in response.headers.items()
                }
                return HttpResponse(
                    status=response.status,
                    headers=headers,
                    body=body,
                )
        except HTTPError as exc:
            headers = {
                str(key).lower(): str(value)
                for key, value in (exc.headers.items() if exc.headers else ())
            }
            # The body is intentionally not read or kept: error text from a
            # remote server is untrusted and can be huge.
            exc.close()
            return HttpResponse(status=exc.code, headers=headers, body=b"")
        except (URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise HHNetworkError(str(reason)) from None


def parse_vacancies(payload: Mapping[str, Any]) -> list[Vacancy]:
    """Parse an hh.ru search response into vacancy objects."""

    items = payload.get("items")
    if not isinstance(items, list):
        raise DataError("payload does not contain an 'items' array")
    vacancies: list[Vacancy] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise DataError(f"item {index} is not a JSON object")
        try:
            vacancies.append(Vacancy.from_hh(item))
        except DataError as exc:
            raise DataError(f"item {index}: {exc}") from None
    return vacancies


def load_fixture(path: str | Path) -> list[Vacancy]:
    """Read an offline fixture.

    The preferred shape is a full hh.ru search response
    (``{"items": [...], "found": ...}``).  A bare list of vacancy objects is
    also accepted for hand-written fixtures.
    """

    fixture = Path(path)
    try:
        raw = fixture.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        detail = getattr(exc, "strerror", None) or str(exc)
        raise FixtureError(f"cannot read fixture {fixture}: {detail}") from None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise FixtureError(f"fixture {fixture} is not valid JSON") from None

    if isinstance(payload, list):
        payload = {"items": payload}
    if not isinstance(payload, Mapping) or not isinstance(payload.get("items"), list):
        raise FixtureError(
            f"fixture {fixture} must be an hh.ru-style object with an 'items' array"
        )

    try:
        return parse_vacancies(payload)
    except DataError as exc:
        raise FixtureError(f"fixture {fixture} has an unexpected shape: {exc}") from None


def _retry_delay(
    headers: Mapping[str, str],
    attempt: int,
    *,
    now: datetime | None = None,
) -> float:
    """Return the delay before the next attempt.

    ``attempt`` is zero-based.  A numeric or HTTP-date ``Retry-After`` wins over
    the exponential backoff; the result is always capped.
    """

    raw_retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if raw_retry_after:
        value = str(raw_retry_after).strip()
        seconds: float | None = None
        try:
            seconds = float(value)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                current = now or datetime.now(timezone.utc)
                seconds = (parsed - current).total_seconds()
        if seconds is not None and seconds >= 0:
            return min(seconds, MAX_RETRY_AFTER_SECONDS)

    return min(BACKOFF_BASE_SECONDS * (2 ** attempt), MAX_RETRY_AFTER_SECONDS)


class HHClient:
    """Client for the public hh.ru vacancies endpoint."""

    def __init__(
        self,
        transport: Transport | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        retries: int = MAX_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        base_url: str = API_BASE_URL,
    ) -> None:
        if retries < 0:
            raise ValueError("retries must not be negative")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self._transport = transport if transport is not None else UrllibTransport()
        self._timeout = timeout
        self._retries = retries
        self._sleep = sleep
        self._base_url = base_url.rstrip("/")

    @property
    def transport(self) -> Transport:
        return self._transport

    def build_url(self, query: Query, per_page: int) -> str:
        params: dict[str, str] = {
            "page": "0",
            "per_page": str(per_page),
        }
        if query.text:
            params["text"] = query.text
        if query.area_id is not None:
            params["area"] = str(query.area_id)
        return f"{self._base_url}/vacancies?{urlencode(params)}"

    @staticmethod
    def _decode(payload: bytes) -> dict[str, Any]:
        try:
            decoded = payload.decode("utf-8")
            parsed = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HHApiError(
                "hh.ru returned a response that was not valid JSON"
            ) from None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
            raise HHApiError(
                "hh.ru returned a response without an 'items' array"
            )
        return parsed

    def fetch_page(self, query: Query, per_page: int) -> dict[str, Any]:
        """Fetch and decode one page of search results.

        The raw payload is returned so the CLI can cache it without a second
        serialisation/deserialisation step.
        """

        request = HttpRequest(
            url=self.build_url(query, per_page),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
            timeout=self._timeout,
        )

        for attempt in range(self._retries + 1):
            try:
                response = self._transport.send(request)
            except HHNetworkError as exc:
                raise HHNetworkError(f"could not reach hh.ru: {exc}") from None

            if response.status == 200:
                return self._decode(response.body)

            retryable = (
                response.status in RETRYABLE_STATUSES
                or 500 <= response.status <= 599
            )
            if retryable and attempt < self._retries:
                self._sleep(_retry_delay(response.headers, attempt))
                continue

            raise HHApiError(f"hh.ru API request failed with HTTP {response.status}")

        # The loop either returns or raises; this keeps static analysers happy.
        raise HHApiError("hh.ru API request failed after retries")

    def search(self, query: Query, per_page: int | None = None) -> list[Vacancy]:
        """Fetch one page and parse it into vacancies."""

        payload = self.fetch_page(query, per_page or query.per_page)
        try:
            return parse_vacancies(payload)
        except DataError as exc:
            raise HHApiError(f"hh.ru returned an unexpected payload: {exc}") from None
