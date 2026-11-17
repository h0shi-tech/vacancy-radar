"""Salary normalisation and floor-comparison tests."""

from __future__ import annotations

import unittest

from tests.support import make_salary, make_vacancy
from vacancy_radar.errors import DataError
from vacancy_radar.models import Query, Salary
from vacancy_radar.scoring import (
    GROSS_TO_NET,
    SALARY_CURRENCY_MISMATCH_FIT,
    SALARY_MISSING_FIT,
    SALARY_STATUS_CURRENCY_MISMATCH,
    SALARY_STATUS_CURRENCY_UNKNOWN,
    SALARY_STATUS_KNOWN,
    SALARY_STATUS_UNKNOWN,
    assess_salary,
    normalize_salary,
    passes_salary_floor,
    vacancy_flags,
)


class NormalisationTests(unittest.TestCase):
    def test_gross_both_bounds_is_converted(self) -> None:
        salary = make_salary(100_000, 150_000, "RUB", gross=True)
        assessment = assess_salary(salary, None, "RUB")

        self.assertEqual(assessment.status, SALARY_STATUS_KNOWN)
        self.assertAlmostEqual(assessment.net_from, 87_000.0)
        self.assertAlmostEqual(assessment.net_to, 130_500.0)
        self.assertAlmostEqual(assessment.fit, 87_000.0 / 150_000.0)
        self.assertIn("×0.87", assessment.note)

    def test_gross_constant_is_the_documented_value(self) -> None:
        self.assertEqual(GROSS_TO_NET, 0.87)
        net_from, _ = normalize_salary(make_salary(100_001, None, "RUB", True))
        self.assertAlmostEqual(net_from, 100_001 * 0.87)

    def test_net_salary_is_not_converted_again(self) -> None:
        salary = make_salary(100_000, 150_000, "RUB", gross=False)
        assessment = assess_salary(salary, None, "RUB")

        self.assertAlmostEqual(assessment.net_from, 100_000.0)
        self.assertAlmostEqual(assessment.net_to, 150_000.0)
        self.assertIn("reported as net", assessment.note)

    def test_from_only(self) -> None:
        salary = make_salary(100_000, None, "RUB", gross=True)
        assessment = assess_salary(salary, None, "RUB")

        self.assertAlmostEqual(assessment.net_from, 87_000.0)
        self.assertIsNone(assessment.net_to)
        self.assertEqual(assessment.used_amount, 87_000.0)
        self.assertEqual(assessment.used_bound, "from")
        self.assertIn("from bound", assessment.note)

    def test_to_only_uses_the_upper_bound_as_best_estimate(self) -> None:
        salary = make_salary(None, 100_000, "RUB", gross=True)
        assessment = assess_salary(salary, None, "RUB")

        self.assertIsNone(assessment.net_from)
        self.assertAlmostEqual(assessment.net_to, 87_000.0)
        self.assertEqual(assessment.used_amount, 87_000.0)
        self.assertEqual(assessment.used_bound, "to")
        self.assertIn("only the upper bound is known", assessment.note)

    def test_missing_bounds_are_unknown_not_zero(self) -> None:
        for salary in (None, make_salary(None, None, "RUB", True)):
            with self.subTest(salary=salary):
                assessment = assess_salary(salary, None, "RUB")
                self.assertEqual(assessment.status, SALARY_STATUS_UNKNOWN)
                self.assertIsNone(assessment.used_amount)
                self.assertIsNone(assessment.net_from)
                self.assertIsNone(assessment.net_to)
                self.assertEqual(assessment.fit, SALARY_MISSING_FIT)
                self.assertNotEqual(assessment.fit, 0.0)
                self.assertIn("not treated as zero", assessment.note)

    def test_missing_salary_object_and_empty_salary_object(self) -> None:
        self.assertIsNone(Salary.from_hh(None))
        parsed = Salary.from_hh({"from": None, "to": None, "currency": None, "gross": None})
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed, Salary(None, None, None, None))
        self.assertEqual(
            assess_salary(parsed, 100_000, "RUB").status,
            SALARY_STATUS_UNKNOWN,
        )

    def test_currency_mismatch_does_not_convert_fx(self) -> None:
        salary = make_salary(1_000, 2_000, "USD", gross=True)
        assessment = assess_salary(salary, 100_000, "RUB")

        self.assertEqual(assessment.status, SALARY_STATUS_CURRENCY_MISMATCH)
        self.assertEqual(assessment.currency, "USD")
        self.assertAlmostEqual(assessment.net_from, 870.0)
        self.assertAlmostEqual(assessment.net_to, 1_740.0)
        self.assertIsNone(assessment.used_amount)
        self.assertEqual(assessment.fit, SALARY_CURRENCY_MISMATCH_FIT)
        self.assertIn("no FX conversion", assessment.note)

    def test_currency_unknown(self) -> None:
        salary = make_salary(100_000, None, None, gross=False)
        assessment = assess_salary(salary, None, "RUB")

        self.assertEqual(assessment.status, SALARY_STATUS_CURRENCY_UNKNOWN)
        self.assertIsNone(assessment.currency)
        self.assertIsNone(assessment.used_amount)
        self.assertIn("currency is missing", assessment.note)

    def test_currency_comparison_is_case_insensitive(self) -> None:
        salary = make_salary(1_000, None, "usd", gross=False)
        assessment = assess_salary(salary, None, "USD")
        self.assertEqual(assessment.status, SALARY_STATUS_KNOWN)

    def test_missing_gross_flag_is_treated_as_net_and_flagged(self) -> None:
        salary = make_salary(100_000, 120_000, "RUB", gross=None)
        assessment = assess_salary(salary, None, "RUB")
        vacancy = make_vacancy(salary=salary)

        self.assertAlmostEqual(assessment.net_from, 100_000.0)
        self.assertIn("gross/net flag missing", assessment.note)
        self.assertIn("gross_unknown", vacancy_flags(vacancy, "RUB"))

    def test_bad_salary_fields_raise_data_error(self) -> None:
        with self.assertRaises(DataError):
            Salary.from_hh({"from": "not-a-number", "to": 1, "currency": "RUB"})
        with self.assertRaises(DataError):
            Salary.from_hh({"from": True, "to": None, "currency": "RUB"})
        with self.assertRaises(DataError):
            Salary.from_hh([])


class FloorComparisonTests(unittest.TestCase):
    def test_floor_fit_and_filter(self) -> None:
        vacancy = make_vacancy(
            salary=make_salary(100_000, 150_000, "RUB", gross=False)
        )

        self.assertTrue(passes_salary_floor(vacancy, Query(min_salary=100_000)))
        self.assertFalse(passes_salary_floor(vacancy, Query(min_salary=150_001)))

    def test_missing_salary_passes_the_filter(self) -> None:
        from dataclasses import replace

        vacancy = replace(make_vacancy(), salary=None)
        self.assertTrue(passes_salary_floor(vacancy, Query(min_salary=10_000_000)))

    def test_mismatched_currency_passes_the_filter(self) -> None:
        vacancy = make_vacancy(
            salary=make_salary(1_000, 2_000, "USD", gross=False)
        )
        self.assertTrue(passes_salary_floor(vacancy, Query(min_salary=10_000_000)))

    def test_fit_saturates_at_one_point_five_times_the_floor(self) -> None:
        at_floor = assess_salary(
            make_salary(100_000, None, "RUB", False), 100_000, "RUB"
        )
        one_and_a_half = assess_salary(
            make_salary(150_000, None, "RUB", False), 100_000, "RUB"
        )
        much_higher = assess_salary(
            make_salary(500_000, None, "RUB", False), 100_000, "RUB"
        )

        self.assertAlmostEqual(at_floor.fit, 2 / 3)
        self.assertAlmostEqual(one_and_a_half.fit, 1.0)
        self.assertAlmostEqual(much_higher.fit, 1.0)
        self.assertLess(at_floor.fit, one_and_a_half.fit)

    def test_no_floor_uses_anchor_and_is_monotonic(self) -> None:
        low = assess_salary(make_salary(75_000, None, "RUB", False), None, "RUB")
        high = assess_salary(make_salary(150_000, None, "RUB", False), None, "RUB")
        self.assertLess(low.fit, high.fit)
        self.assertAlmostEqual(high.fit, 1.0)

    def test_below_floor_fit_is_lower_than_at_floor(self) -> None:
        below = assess_salary(
            make_salary(50_000, None, "RUB", False), 100_000, "RUB"
        )
        at = assess_salary(
            make_salary(100_000, None, "RUB", False), 100_000, "RUB"
        )
        self.assertLess(below.fit, at.fit)
        self.assertGreaterEqual(below.fit, 0.0)


if __name__ == "__main__":
    unittest.main()
