"""Tests for the small, verified area-alias table."""

from __future__ import annotations

import unittest

from vacancy_radar.areas import (
    AREA_BY_ID,
    UnknownAreaError,
    normalize_area_name,
    resolve_area,
)


class AreaTests(unittest.TestCase):
    def test_common_aliases_resolve_to_verified_ids(self) -> None:
        cases = {
            "moscow": 1,
            "Москва": 1,
            "spb": 2,
            "Санкт-Петербург": 2,
            "samara": 78,
            "Самара": 78,
            "kazan": 88,
            "Казань": 88,
            "yekaterinburg": 3,
            "novosibirsk": 4,
            "russia": 113,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(resolve_area(name).id, expected)

    def test_case_and_yo_normalisation(self) -> None:
        self.assertEqual(normalize_area_name("  САНКТ-ПЕТЕРБУРГ  "), "санкт-петербург")
        self.assertEqual(normalize_area_name("Королёв"), "королев")
        self.assertEqual(resolve_area("  MOSCOW ").id, 1)
        self.assertEqual(resolve_area("екб").id, 3)

    def test_unknown_area_raises_with_guidance(self) -> None:
        with self.assertRaises(UnknownAreaError) as caught:
            resolve_area("atlantis")
        message = str(caught.exception)
        self.assertIn("unknown area", message)
        self.assertIn("--area-id", message)

    def test_numeric_area_names_are_not_guessed(self) -> None:
        # Numeric ids belong to --area-id; --area must not silently accept any
        # number that is not in the verified table.
        with self.assertRaises(UnknownAreaError):
            resolve_area("999999")

    def test_area_table_has_no_duplicate_ids(self) -> None:
        self.assertEqual(len(AREA_BY_ID), len(set(AREA_BY_ID)))
        self.assertEqual(AREA_BY_ID[1].name, "Москва")
        self.assertEqual(AREA_BY_ID[78].name, "Самара")


if __name__ == "__main__":
    unittest.main()
