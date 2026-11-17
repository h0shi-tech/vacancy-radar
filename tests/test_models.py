"""Model parsing and query validation tests."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tests.support import FIXED_NOW, fixture_payload
from vacancy_radar.errors import DataError
from vacancy_radar.hh_client import parse_vacancies
from vacancy_radar.models import Query, Salary, Vacancy, ensure_aware


class VacancyParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vacancies = parse_vacancies(fixture_payload())
        self.by_id = {vacancy.id: vacancy for vacancy in self.vacancies}

    def test_fixture_parses_into_fifteen_vacancies(self) -> None:
        self.assertEqual(len(self.vacancies), 15)
        self.assertEqual(len(self.by_id), 15)

    def test_full_salary_and_employer_fields(self) -> None:
        vacancy = self.by_id["990000001"]
        self.assertEqual(vacancy.title, "Senior Python-разработчик")
        self.assertEqual(vacancy.employer, "ООО «Кодовая база»")
        self.assertEqual(vacancy.area_id, "1")
        self.assertEqual(vacancy.area_name, "Москва")
        self.assertFalse(vacancy.remote)
        self.assertIsNotNone(vacancy.salary)
        assert vacancy.salary is not None
        self.assertEqual(vacancy.salary.from_, 240_000)
        self.assertEqual(vacancy.salary.to, 320_000)
        self.assertEqual(vacancy.salary.currency, "RUB")
        self.assertTrue(vacancy.salary.gross)
        self.assertEqual(
            vacancy.published_at,
            datetime(2026, 11, 17, 18, 0, tzinfo=timezone(timedelta(hours=3))),
        )
        self.assertIn("FastAPI", vacancy.snippet)
        self.assertIn("Проектировать", vacancy.snippet)

    def test_remote_schedule_is_detected(self) -> None:
        vacancy = self.by_id["990000002"]
        self.assertTrue(vacancy.remote)

    def test_remote_work_format_only_is_detected(self) -> None:
        raw = {
            "id": "42",
            "name": "Remote role",
            "schedule": {"id": "fullDay", "name": "Полный день"},
            "work_format": [{"id": "REMOTE", "name": "Удалённо"}],
        }
        vacancy = Vacancy.from_hh(raw)
        self.assertTrue(vacancy.remote)

    def test_anonymous_employers(self) -> None:
        self.assertTrue(self.by_id["990000013"].is_anonymous)
        self.assertTrue(self.by_id["990000015"].is_anonymous)
        self.assertFalse(self.by_id["990000001"].is_anonymous)
        self.assertTrue(Vacancy.from_hh({"id": "1", "name": "A", "employer": {"name": "  "}}).is_anonymous)

    def test_missing_optional_fields_are_tolerated(self) -> None:
        vacancy = Vacancy.from_hh({"id": 7, "name": "Only the basics"})
        self.assertEqual(vacancy.id, "7")
        self.assertIsNone(vacancy.salary)
        self.assertIsNone(vacancy.employer)
        self.assertIsNone(vacancy.published_at)
        self.assertIsNone(vacancy.area_id)
        self.assertEqual(vacancy.area_name, "Unknown")
        self.assertFalse(vacancy.remote)
        self.assertTrue(vacancy.is_anonymous)

    def test_invalid_published_at_becomes_unknown(self) -> None:
        vacancy = Vacancy.from_hh(
            {"id": "8", "name": "Bad date", "published_at": "yesterday"}
        )
        self.assertIsNone(vacancy.published_at)

    def test_control_characters_are_stripped_from_display_text(self) -> None:
        vacancy = Vacancy.from_hh(
            {
                "id": "9",
                "name": "Python\x1b[31m developer",
                "employer": {"name": "ACME\x07"},
                "area": {"id": "1", "name": "Москва\x1b[0m"},
            }
        )
        for text in (vacancy.title, vacancy.employer or "", vacancy.area_name):
            self.assertTrue(all(character.isprintable() for character in text))
        self.assertEqual(vacancy.employer, "ACME")

    def test_malformed_items_raise_data_error(self) -> None:
        with self.assertRaises(DataError):
            Vacancy.from_hh([])
        with self.assertRaises(DataError):
            Vacancy.from_hh({"name": "No id"})
        with self.assertRaises(DataError):
            Vacancy.from_hh({"id": "1"})
        with self.assertRaises(DataError):
            Vacancy.from_hh({"id": "1", "name": "Bad salary", "salary": []})
        with self.assertRaises(DataError):
            Vacancy.from_hh({"id": "1", "name": "Bad employer", "employer": "Acme"})
        with self.assertRaises(DataError):
            Vacancy.from_hh({"id": "1", "name": "Bad area", "area": "Moscow"})

    def test_parse_vacancies_requires_items_array(self) -> None:
        with self.assertRaises(DataError):
            parse_vacancies({})
        with self.assertRaises(DataError):
            parse_vacancies({"items": "not-a-list"})

    def test_alternate_url_fallback(self) -> None:
        alternate = Vacancy.from_hh(
            {"id": "1", "name": "A", "alternate_url": "https://hh.ru/vacancy/1"}
        )
        plain = Vacancy.from_hh(
            {"id": "2", "name": "B", "url": "https://api.hh.ru/vacancies/2"}
        )
        self.assertEqual(alternate.url, "https://hh.ru/vacancy/1")
        self.assertEqual(plain.url, "https://api.hh.ru/vacancies/2")


class QueryTests(unittest.TestCase):
    def test_normalisation(self) -> None:
        query = Query(
            text="  python  ",
            currency="rub",
            keywords=("Python", " fastapi ", "python", ""),
        )
        self.assertEqual(query.text, "python")
        self.assertEqual(query.currency, "RUB")
        self.assertEqual(query.keywords, ("Python", "fastapi"))

    def test_validation(self) -> None:
        bad_arguments = [
            {"top": 0},
            {"per_page": 0},
            {"per_page": 101},
            {"currency": "RUBLE"},
            {"area_id": 0},
            {"min_salary": -1},
        ]
        for arguments in bad_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    Query(**arguments)

    def test_salary_from_hh_keeps_unknown_gross(self) -> None:
        salary = Salary.from_hh(
            {"from": 100, "to": None, "currency": "rub", "gross": "true"}
        )
        self.assertEqual(salary, Salary(100, None, "RUB", None))

    def test_ensure_aware_treats_naive_as_utc(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0)
        self.assertEqual(ensure_aware(naive).tzinfo, timezone.utc)
        self.assertEqual(ensure_aware(FIXED_NOW), FIXED_NOW)


if __name__ == "__main__":
    unittest.main()
