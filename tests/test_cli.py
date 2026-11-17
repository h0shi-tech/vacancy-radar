"""End-to-end CLI tests, including the offline fixture path."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.support import (
    ExplodingTransport,
    FIXED_NOW,
    FIXTURE_PATH,
    NetworkFailureTransport,
    StaticTransport,
    fixture_body,
)
from vacancy_radar.cache import JsonCache
from vacancy_radar.cli import main


class OfflineCliTests(unittest.TestCase):
    def run_offline(self, *arguments: str) -> tuple[int, str, str, ExplodingTransport]:
        out = io.StringIO()
        err = io.StringIO()
        transport = ExplodingTransport()
        code = main(
            list(arguments),
            stdout=out,
            stderr=err,
            now=FIXED_NOW,
            transport=transport,
        )
        return code, out.getvalue(), err.getvalue(), transport

    def test_offline_demo_table(self) -> None:
        code, output, error, transport = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--top",
            "5",
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(transport.calls, 0)
        self.assertNotIn("\x1b[", output)
        self.assertTrue(output.startswith("#  SCORE"))
        rows = [
            line
            for line in output.splitlines()
            if line and not line.startswith("-") and not line.startswith("#")
        ]
        self.assertEqual(len(rows), 5)
        self.assertIn("Senior Python-разработчик", output)

    def test_offline_json_ranked_order_and_exit_code(self) -> None:
        code, output, error, transport = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--format",
            "json",
            "--top",
            "5",
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertEqual(transport.calls, 0)
        document = json.loads(output)
        self.assertEqual(document["fetched"], 15)
        self.assertEqual(document["count"], 5)
        ids = [result["id"] for result in document["results"]]
        self.assertEqual(
            ids,
            [
                "990000001",
                "990000002",
                "990000004",
                "990000003",
                "990000006",
            ],
        )
        scores = [result["score"] for result in document["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_capture_stdout_with_redirect(self) -> None:
        captured_out = io.StringIO()
        captured_err = io.StringIO()
        with redirect_stdout(captured_out), redirect_stderr(captured_err):
            code = main(
                ["--offline", str(FIXTURE_PATH), "--top", "3"],
                now=FIXED_NOW,
                transport=ExplodingTransport(),
            )
        self.assertEqual(code, 0)
        self.assertIn("SCORE", captured_out.getvalue())
        self.assertEqual(captured_err.getvalue(), "")

    def test_offline_without_area_replays_all_areas(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline", str(FIXTURE_PATH), "--format", "json", "--top", "50"
        )
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(document["count"], 15)
        self.assertGreater(
            len({result["area"]["name"] for result in document["results"]}), 1
        )

    def test_area_name_filter_offline(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--area",
            "samara",
            "--format",
            "json",
        )
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(document["count"], 2)
        self.assertTrue(
            all(result["area"]["id"] == "78" for result in document["results"])
        )

    def test_area_id_filter_offline(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--area-id",
            "4",
            "--format",
            "json",
        )
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(document["count"], 1)
        self.assertEqual(document["results"][0]["area"]["name"], "Новосибирск")

    def test_russia_area_alias_replays_the_whole_fixture(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--area",
            "russia",
            "--format",
            "json",
            "--top",
            "50",
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["count"], 15)

    def test_query_text_filter_offline(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "django",
            "--format",
            "json",
        )
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(document["count"], 3)
        for result in document["results"]:
            searchable = (
                result["title"] + result["snippet"] + (result["employer"] or "")
            ).casefold()
            self.assertIn("django", searchable)

    def test_remote_only_filters_non_remote(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--remote-only",
            "--format",
            "json",
            "--top",
            "50",
        )
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(document["count"], 8)
        self.assertTrue(all(result["remote"] for result in document["results"]))

    def test_remote_bonus_does_not_hard_filter(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline", str(FIXTURE_PATH), "--remote", "--format", "json"
        )
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertTrue(
            any(not result["remote"] for result in document["results"])
        )

    def test_min_salary_keeps_unknown_and_mismatched_currencies(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--min-salary",
            "999999999",
            "--format",
            "json",
            "--top",
            "50",
        )
        self.assertEqual(code, 0)
        document = json.loads(output)
        self.assertEqual(document["count"], 5)
        self.assertEqual(document["fetched"], 15)
        for result in document["results"]:
            salary = result["salary"]
            if salary is not None:
                self.assertNotEqual(salary["currency"], "RUB")
                self.assertIn(
                    salary["status"],
                    {"currency_mismatch", "currency_unknown"},
                )
        flags = [flag for result in document["results"] for flag in result["flags"]]
        self.assertIn("currency_mismatch", flags)
        self.assertIn("salary_unknown", flags)

    def test_no_results_after_filter_exits_3(self) -> None:
        code, output, error, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--area-id",
            "4",
            "--remote-only",
            "--format",
            "json",
        )
        self.assertEqual(code, 3)
        document = json.loads(output)
        self.assertEqual(document["count"], 0)
        self.assertEqual(document["results"], [])
        self.assertIn("No vacancies matched", error)
        self.assertIn("fetched 1", error)

    def test_no_results_table_prints_nothing_to_stdout(self) -> None:
        code, output, error, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "zzz-not-in-the-fixture",
        )
        self.assertEqual(code, 3)
        self.assertEqual(output, "")
        self.assertIn("No vacancies matched", error)

    def test_invalid_fixture_path_exits_1(self) -> None:
        code, output, error, _ = self.run_offline(
            "--offline", str(FIXTURE_PATH.parent / "missing.json")
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("cannot read fixture", error)
        self.assertNotIn("Traceback", error)

    def test_invalid_fixture_json_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            code, output, error, _ = self.run_offline("--offline", str(path))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("not valid JSON", error)
        self.assertNotIn("Traceback", error)

    def test_unknown_area_exits_1(self) -> None:
        code, output, error, transport = self.run_offline(
            "--offline", str(FIXTURE_PATH), "--area", "atlantis"
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("unknown area", error)
        self.assertNotIn("Traceback", error)
        self.assertEqual(transport.calls, 0)

    def test_usage_error_exits_1(self) -> None:
        code, output, error, _ = self.run_offline(
            "--offline", str(FIXTURE_PATH), "--top", "0"
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("error:", error)
        self.assertNotIn("Traceback", error)

    def test_non_finite_numeric_option_is_a_usage_error(self) -> None:
        code, output, error, _ = self.run_offline(
            "--offline", str(FIXTURE_PATH), "--timeout", "nan"
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("finite", error)
        self.assertNotIn("Traceback", error)

    def test_conflicting_text_options_exit_1(self) -> None:
        code, output, error, _ = self.run_offline(
            "--offline", str(FIXTURE_PATH), "python", "--query", "java"
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("different search texts", error)

    def test_area_and_area_id_are_mutually_exclusive(self) -> None:
        code, output, error, _ = self.run_offline(
            "--offline", str(FIXTURE_PATH), "--area", "samara", "--area-id", "1"
        )
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("either --area or --area-id", error)

    def test_markdown_output_has_links(self) -> None:
        code, output, error, _ = self.run_offline(
            "--offline", str(FIXTURE_PATH), "--format", "md", "--top", "2"
        )
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("| # | Score | Vacancy |", output)
        self.assertIn("](https://hh.ru/vacancy/", output)

    def test_json_explain_contains_breakdown(self) -> None:
        code, output, _, _ = self.run_offline(
            "--offline",
            str(FIXTURE_PATH),
            "--format",
            "json",
            "--top",
            "3",
            "--explain",
            "--remote",
            "--keyword",
            "python",
        )
        self.assertEqual(code, 0)
        document = json.loads(output)
        for result in document["results"]:
            explain = result["explain"]
            self.assertIn("salary", explain["components"])
            self.assertIn("remote", explain["components"])
            self.assertAlmostEqual(sum(explain["weights"].values()), 1.0)
            self.assertIn("notes", explain)


class LiveCliTests(unittest.TestCase):
    def test_live_requires_text_before_touching_the_network(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        transport = ExplodingTransport()
        code = main(
            [],
            stdout=out,
            stderr=err,
            transport=transport,
            now=FIXED_NOW,
        )
        self.assertEqual(code, 1)
        self.assertEqual(transport.calls, 0)
        self.assertIn("search text is required", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_network_failure_is_clean_and_exits_1(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        transport = NetworkFailureTransport()
        code = main(
            ["python", "--no-cache"],
            stdout=out,
            stderr=err,
            transport=transport,
            now=FIXED_NOW,
        )
        self.assertEqual(code, 1)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("could not reach hh.ru", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_live_transport_is_used_and_parameters_are_sent(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        transport = StaticTransport(fixture_body())
        code = main(
            [
                "python",
                "--area",
                "samara",
                "--per-page",
                "15",
                "--no-cache",
                "--format",
                "json",
            ],
            stdout=out,
            stderr=err,
            transport=transport,
            now=FIXED_NOW,
        )
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertIn("text=python", request.url)
        self.assertIn("area=78", request.url)
        self.assertIn("per_page=15", request.url)
        self.assertIn("vacancy-radar/", request.headers["User-Agent"])
        document = json.loads(out.getvalue())
        self.assertEqual(document["fetched"], 15)
        self.assertEqual(document["count"], 10)

    def test_live_search_defaults_to_moscow_area(self) -> None:
        transport = StaticTransport(fixture_body())
        code = main(
            ["python", "--no-cache", "--format", "json"],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            transport=transport,
            now=FIXED_NOW,
        )
        self.assertEqual(code, 0)
        self.assertIn("area=1", transport.requests[0].url)

    def test_second_live_run_uses_the_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = JsonCache(Path(tmp), ttl=300)
            first_transport = StaticTransport(fixture_body())
            first_out = io.StringIO()
            first_code = main(
                ["python", "--format", "json"],
                stdout=first_out,
                stderr=io.StringIO(),
                transport=first_transport,
                now=FIXED_NOW,
                cache=cache,
            )
            second_transport = ExplodingTransport()
            second_out = io.StringIO()
            second_code = main(
                ["python", "--format", "json"],
                stdout=second_out,
                stderr=io.StringIO(),
                transport=second_transport,
                now=FIXED_NOW,
                cache=cache,
            )

        self.assertEqual(first_code, 0)
        self.assertEqual(second_code, 0)
        self.assertEqual(len(first_transport.requests), 1)
        self.assertEqual(second_transport.calls, 0)
        self.assertEqual(json.loads(first_out.getvalue()), json.loads(second_out.getvalue()))

    def test_malformed_live_payload_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = JsonCache(Path(tmp), ttl=300)
            transport = StaticTransport(
                json.dumps({"items": [{"id": "no-name"}]}).encode("utf-8")
            )
            out = io.StringIO()
            err = io.StringIO()
            code = main(
                ["python", "--format", "json"],
                stdout=out,
                stderr=err,
                transport=transport,
                now=FIXED_NOW,
                cache=cache,
            )
            cache_files = list(Path(tmp).glob("*.json"))

        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("could not parse", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(cache_files, [])


if __name__ == "__main__":
    unittest.main()
