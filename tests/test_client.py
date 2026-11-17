"""HTTP client, retry, parsing and fixture-loader tests.

Every transport here is a fake.  No test constructs UrllibTransport, and the
suite would fail if it tried to reach hh.ru.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tests.support import (
    FakeTransport,
    FIXTURE_PATH,
    fixture_payload,
    http_response,
)
from vacancy_radar.errors import DataError, FixtureError, HHApiError, HHNetworkError
from vacancy_radar.hh_client import (
    HHClient,
    _retry_delay,
    load_fixture,
    parse_vacancies,
)
from vacancy_radar.models import Query


class ClientParsingTests(unittest.TestCase):
    def test_fixture_payload_is_parsed_through_client(self) -> None:
        transport = FakeTransport([http_response(payload=fixture_payload())])
        client = HHClient(transport=transport)
        vacancies = client.search(Query(text="python", area_id=1), per_page=15)

        self.assertEqual(len(vacancies), 15)
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertIn("vacancy-radar/0.1.0", request.headers["User-Agent"])
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertEqual(request.timeout, 10.0)

    def test_build_url_encodes_search_parameters(self) -> None:
        client = HHClient(transport=FakeTransport([]))
        url = client.build_url(Query(text="python разработчик", area_id=78), 25)
        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "api.hh.ru")
        self.assertEqual(parsed.path, "/vacancies")
        self.assertEqual(params["text"], ["python разработчик"])
        self.assertEqual(params["area"], ["78"])
        self.assertEqual(params["per_page"], ["25"])
        self.assertEqual(params["page"], ["0"])

    def test_build_url_omits_optional_parameters(self) -> None:
        client = HHClient(transport=FakeTransport([]))
        params = parse_qs(urlparse(client.build_url(Query(), 5)).query)
        self.assertNotIn("text", params)
        self.assertNotIn("area", params)

    def test_malformed_json_does_not_leak_body(self) -> None:
        transport = FakeTransport(
            [http_response(body=b"<html>secret backend details</html>")]
        )
        client = HHClient(transport=transport)
        with self.assertRaises(HHApiError) as caught:
            client.fetch_page(Query(text="python"), 5)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("not valid JSON", str(caught.exception))

    def test_missing_items_array_is_an_api_error(self) -> None:
        transport = FakeTransport([http_response(payload={"found": 0})])
        client = HHClient(transport=transport)
        with self.assertRaises(HHApiError):
            client.fetch_page(Query(text="python"), 5)

    def test_malformed_items_raise_data_error(self) -> None:
        with self.assertRaises(DataError):
            parse_vacancies({"items": [{"id": "1"}]})
        with self.assertRaises(DataError):
            parse_vacancies({"items": ["not-an-object"]})


class RetryTests(unittest.TestCase):
    def make_client(self, transport: FakeTransport, sleeps: list[float], **kwargs) -> HHClient:
        return HHClient(
            transport=transport,
            sleep=sleeps.append,
            **kwargs,
        )

    def test_two_retries_then_success_with_exponential_backoff(self) -> None:
        transport = FakeTransport(
            [
                http_response(500),
                http_response(503),
                http_response(payload={"items": []}),
            ]
        )
        sleeps: list[float] = []
        client = self.make_client(transport, sleeps)

        payload = client.fetch_page(Query(text="python"), 5)

        self.assertEqual(payload, {"items": []})
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_retry_after_header_is_honoured(self) -> None:
        transport = FakeTransport(
            [
                http_response(429, headers={"retry-after": "2"}),
                http_response(payload={"items": []}),
            ]
        )
        sleeps: list[float] = []
        client = self.make_client(transport, sleeps)

        client.fetch_page(Query(text="python"), 5)

        self.assertEqual(sleeps, [2.0])

    def test_retry_after_is_capped(self) -> None:
        transport = FakeTransport(
            [
                http_response(429, headers={"Retry-After": "9999"}),
                http_response(payload={"items": []}),
            ]
        )
        sleeps: list[float] = []
        client = self.make_client(transport, sleeps)

        client.fetch_page(Query(text="python"), 5)

        self.assertEqual(sleeps, [30.0])

    def test_invalid_retry_after_falls_back_to_backoff(self) -> None:
        transport = FakeTransport(
            [
                http_response(500, headers={"retry-after": "later"}),
                http_response(payload={"items": []}),
            ]
        )
        sleeps: list[float] = []
        client = self.make_client(transport, sleeps)

        client.fetch_page(Query(text="python"), 5)

        self.assertEqual(sleeps, [0.5])

    def test_exhausted_retries_raise_without_leaking_body(self) -> None:
        transport = FakeTransport(
            [
                http_response(500, body=b"internal secret"),
                http_response(502, body=b"internal secret"),
                http_response(503, body=b"internal secret"),
            ]
        )
        sleeps: list[float] = []
        client = self.make_client(transport, sleeps)

        with self.assertRaises(HHApiError) as caught:
            client.fetch_page(Query(text="python"), 5)

        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(sleeps, [0.5, 1.0])
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_non_retryable_status_fails_immediately(self) -> None:
        transport = FakeTransport(
            [http_response(403, body=b"forbidden details")]
        )
        sleeps: list[float] = []
        client = self.make_client(transport, sleeps)

        with self.assertRaises(HHApiError) as caught:
            client.fetch_page(Query(text="python"), 5)

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(sleeps, [])
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertNotIn("forbidden details", str(caught.exception))

    def test_network_error_is_not_retried(self) -> None:
        transport = FakeTransport([HHNetworkError("simulated network down")])
        sleeps: list[float] = []
        client = self.make_client(transport, sleeps)

        with self.assertRaises(HHNetworkError) as caught:
            client.fetch_page(Query(text="python"), 5)

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(sleeps, [])
        self.assertIn("could not reach hh.ru", str(caught.exception))

    def test_retry_after_http_date_is_supported(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        retry_at = format_datetime(now + timedelta(seconds=10))
        delay = _retry_delay({"retry-after": retry_at}, 0, now=now)
        self.assertAlmostEqual(delay, 10.0)

    def test_zero_retries_configuration(self) -> None:
        transport = FakeTransport([http_response(500)])
        sleeps: list[float] = []
        client = self.make_client(transport, sleeps, retries=0)

        with self.assertRaises(HHApiError):
            client.fetch_page(Query(text="python"), 5)

        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(sleeps, [])

    def test_invalid_client_configuration(self) -> None:
        with self.assertRaises(ValueError):
            HHClient(transport=FakeTransport([]), retries=-1)
        with self.assertRaises(ValueError):
            HHClient(transport=FakeTransport([]), timeout=0)
        with self.assertRaises(ValueError):
            HHClient(transport=FakeTransport([]), timeout=float("nan"))
        with self.assertRaises(ValueError):
            HHClient(transport=FakeTransport([]), timeout=float("inf"))


class FixtureLoaderTests(unittest.TestCase):
    def test_bundled_fixture_loads_offline(self) -> None:
        vacancies = load_fixture(FIXTURE_PATH)
        self.assertEqual(len(vacancies), 15)
        self.assertEqual(len({vacancy.id for vacancy in vacancies}), 15)

    def test_bare_list_fixture_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.json"
            path.write_text(
                json.dumps([{"id": "1", "name": "Python developer"}]),
                encoding="utf-8",
            )
            vacancies = load_fixture(path)
        self.assertEqual(len(vacancies), 1)
        self.assertEqual(vacancies[0].title, "Python developer")

    def test_missing_fixture_raises_fixture_error(self) -> None:
        with self.assertRaises(FixtureError):
            load_fixture(FIXTURE_PATH.parent / "does-not-exist.json")

    def test_invalid_json_fixture_raises_fixture_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{oops", encoding="utf-8")
            with self.assertRaises(FixtureError):
                load_fixture(path)

    def test_wrong_shape_fixture_raises_fixture_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wrong.json"
            path.write_text('{"items": "nope"}', encoding="utf-8")
            with self.assertRaises(FixtureError):
                load_fixture(path)


if __name__ == "__main__":
    unittest.main()
