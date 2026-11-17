"""Table, markdown and JSON rendering tests."""

from __future__ import annotations

import json
import re
import unittest
from datetime import timedelta

from tests.support import FIXED_NOW, make_salary, make_vacancy
from vacancy_radar.models import Query
from vacancy_radar.render import (
    render_json,
    render_markdown,
    render_table,
    vacancy_json,
)
from vacancy_radar.scoring import score_vacancy

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def ranked_rows() -> list:
    first = make_vacancy(
        vacancy_id="a",
        title="Python | Разработчик",
        employer="ООО «Ромашка»",
        salary=make_salary(180_000, 240_000, "RUB", gross=True),
        published_at=FIXED_NOW - timedelta(hours=2),
        url="https://hh.ru/vacancy/a",
        remote=True,
    )
    second = make_vacancy(
        vacancy_id="b",
        title="Data Engineer",
        employer="ACME | Lab",
        area_id="2",
        area_name="Санкт-Петербург",
        salary=make_salary(120_000, None, "RUB", gross=False),
        published_at=FIXED_NOW - timedelta(days=3),
        url="https://hh.ru/vacancy/b",
        remote=False,
    )
    query = Query(remote=True, keywords=("python", "data"))
    return [
        (first, score_vacancy(first, query, FIXED_NOW)),
        (second, score_vacancy(second, query, FIXED_NOW)),
    ]


class TableTests(unittest.TestCase):
    def test_cyrillic_columns_align(self) -> None:
        rows = ranked_rows()
        query = Query(remote=True)
        output = render_table(rows, query, now=FIXED_NOW)
        lines = output.splitlines()

        self.assertGreaterEqual(len(lines), 4)
        header = lines[0]
        header_positions = {
            name: header.index(name)
            for name in (
                "SCORE",
                "TITLE",
                "EMPLOYER",
                "SALARY (NET)",
                "AREA",
                "AGE",
                "FLAGS",
            )
        }
        for line, (vacancy, _) in zip(lines[2:], rows):
            self.assertEqual(line.index(vacancy.title), header_positions["TITLE"])
            assert vacancy.employer is not None
            self.assertEqual(line.index(vacancy.employer), header_positions["EMPLOYER"])
            self.assertEqual(line.index(vacancy.area_name), header_positions["AREA"])

    def test_no_ansi_when_colour_is_disabled(self) -> None:
        output = render_table(ranked_rows(), Query(), now=FIXED_NOW, color=False)
        self.assertNotIn("\x1b[", output)
        self.assertIn("SCORE", output)

    def test_ansi_colour_does_not_break_alignment(self) -> None:
        rows = ranked_rows()
        plain = render_table(rows, Query(remote=True), now=FIXED_NOW, color=False)
        coloured = render_table(rows, Query(remote=True), now=FIXED_NOW, color=True)
        self.assertIn("\x1b[", coloured)
        self.assertEqual(ANSI_PATTERN.sub("", coloured), plain)

    def test_age_and_flags_columns(self) -> None:
        output = render_table(ranked_rows(), Query(remote=True), now=FIXED_NOW)
        lines = output.splitlines()
        self.assertIn("2h", lines[2])
        self.assertIn("remote", lines[2])
        self.assertIn("3d", lines[3])

    def test_explain_adds_a_component_breakdown(self) -> None:
        output = render_table(
            ranked_rows(),
            Query(remote=True),
            now=FIXED_NOW,
            explain=True,
        )
        self.assertIn("Why these ranks:", output)
        self.assertIn("salary", output)
        self.assertIn("keywords", output)
        self.assertIn("recency", output)
        self.assertIn("remote", output)
        self.assertIn("employer", output)

    def test_long_titles_are_clipped_only_in_the_table(self) -> None:
        long_title = "Очень длинное название вакансии " * 4
        vacancy = make_vacancy(vacancy_id="long", title=long_title)
        query = Query()
        table = render_table(
            [(vacancy, score_vacancy(vacancy, query, FIXED_NOW))],
            query,
            now=FIXED_NOW,
        )
        markdown = render_markdown(
            [(vacancy, score_vacancy(vacancy, query, FIXED_NOW))],
            query,
            now=FIXED_NOW,
        )
        self.assertIn("…", table)
        self.assertNotIn(long_title.strip(), table)
        self.assertIn(long_title.strip(), markdown)


class MarkdownTests(unittest.TestCase):
    def test_pipes_are_escaped_and_links_are_present(self) -> None:
        output = render_markdown(ranked_rows(), Query(remote=True), now=FIXED_NOW)
        lines = output.splitlines()

        self.assertTrue(lines[0].startswith("| # | Score | Vacancy |"))
        self.assertIn("| ---: | ---: | --- |", lines[1])
        self.assertIn("Python \\| Разработчик", output)
        self.assertIn("ACME \\| Lab", output)
        self.assertIn("](https://hh.ru/vacancy/a)", output)
        self.assertIn("](https://hh.ru/vacancy/b)", output)

    def test_table_has_the_expected_number_of_cells(self) -> None:
        output = render_markdown(ranked_rows(), Query(remote=True), now=FIXED_NOW)
        for line in output.splitlines()[2:]:
            # Every literal pipe is preceded by a backslash if it belongs to
            # cell content; count only unescaped separators.
            separators = re.findall(r"(?<!\\)\|", line)
            self.assertEqual(len(separators), 9, line)

    def test_explain_section_is_valid_gfm(self) -> None:
        output = render_markdown(
            ranked_rows(),
            Query(remote=True),
            now=FIXED_NOW,
            explain=True,
        )
        self.assertIn("### Why these ranks", output)
        self.assertIn("**1. Python \\| Разработчик**", output)
        self.assertIn("- `salary`", output)
        self.assertIn("pts.", output)

    def test_empty_rows_still_render_a_header(self) -> None:
        output = render_markdown([], Query(), now=FIXED_NOW)
        self.assertIn("| # | Score |", output)
        self.assertEqual(len(output.splitlines()), 2)


class JsonTests(unittest.TestCase):
    def test_stable_shape_without_explain(self) -> None:
        rows = ranked_rows()
        query = Query(remote=True, keywords=("python",))
        document = json.loads(
            render_json(
                rows,
                query,
                source="offline:test",
                now=FIXED_NOW,
                fetched=7,
            )
        )

        self.assertEqual(
            set(document),
            {"query", "source", "generated_at", "fetched", "count", "results"},
        )
        self.assertEqual(document["source"], "offline:test")
        self.assertEqual(document["fetched"], 7)
        self.assertEqual(document["count"], 2)
        self.assertEqual(document["generated_at"], "2026-11-17T20:00:00+00:00")

        result = document["results"][0]
        self.assertEqual(
            set(result),
            {
                "id",
                "title",
                "employer",
                "area",
                "salary",
                "remote",
                "published_at",
                "url",
                "snippet",
                "flags",
                "score",
            },
        )
        self.assertEqual(set(result["area"]), {"id", "name"})
        self.assertEqual(
            set(result["salary"]),
            {"from", "to", "currency", "gross", "net_from", "net_to", "status"},
        )
        self.assertIsInstance(result["score"], float)
        self.assertNotIn("explain", result)
        self.assertIn("Python | Разработчик", result["title"])

    def test_explain_shape(self) -> None:
        rows = ranked_rows()
        query = Query(remote=True)
        document = json.loads(
            render_json(
                rows,
                query,
                source="test",
                now=FIXED_NOW,
                fetched=2,
                explain=True,
            )
        )
        explain = document["results"][0]["explain"]
        self.assertEqual(
            set(explain),
            {"total", "components", "weights", "contributions", "notes"},
        )
        self.assertIn("remote", explain["components"])
        self.assertAlmostEqual(sum(explain["weights"].values()), 1.0)
        self.assertAlmostEqual(
            sum(explain["contributions"].values()),
            explain["total"],
            places=1,
        )
        self.assertIn("salary", explain["notes"])

    def test_empty_results_shape(self) -> None:
        document = json.loads(
            render_json(
                [],
                Query(),
                source="test",
                now=FIXED_NOW,
                fetched=0,
            )
        )
        self.assertEqual(document["count"], 0)
        self.assertEqual(document["results"], [])

    def test_missing_salary_serialises_as_null(self) -> None:
        from dataclasses import replace

        vacancy = replace(make_vacancy(), salary=None)
        query = Query()
        score = score_vacancy(vacancy, query, FIXED_NOW)
        payload = vacancy_json(vacancy, score, query)
        self.assertIsNone(payload["salary"])
        self.assertIn("salary_unknown", payload["flags"])

    def test_unicode_is_not_ascii_escaped(self) -> None:
        text = render_json(
            ranked_rows(),
            Query(),
            source="test",
            now=FIXED_NOW,
            fetched=2,
        )
        self.assertIn("ООО «Ромашка»", text)
        self.assertIn("Москва", text)


if __name__ == "__main__":
    unittest.main()
