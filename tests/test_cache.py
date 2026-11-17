"""Tests for the optional JSON response cache."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from vacancy_radar.cache import JsonCache


class JsonCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_roundtrip_preserves_unicode_payload(self) -> None:
        cache = JsonCache(self.directory)
        payload = {"items": [{"name": "Разработчик", "active": True}], "found": 1}
        self.assertTrue(cache.set("key", payload))
        self.assertEqual(cache.get("key"), payload)

    def test_missing_key_returns_none(self) -> None:
        cache = JsonCache(self.directory)
        self.assertIsNone(cache.get("never-stored"))

    def test_ttl_expiry_and_boundary(self) -> None:
        now = [1_000.0]
        cache = JsonCache(
            self.directory,
            ttl=10,
            clock=lambda: now[0],
        )
        cache.set("key", {"ok": True})

        now[0] = 1_009.9
        self.assertEqual(cache.get("key"), {"ok": True})
        now[0] = 1_010.0
        self.assertEqual(cache.get("key"), {"ok": True})
        now[0] = 1_010.01
        self.assertIsNone(cache.get("key"))

    def test_zero_ttl_expires_once_clock_moves(self) -> None:
        now = [50.0]
        cache = JsonCache(self.directory, ttl=0, clock=lambda: now[0])
        cache.set("key", 1)
        self.assertEqual(cache.get("key"), 1)
        now[0] = 50.001
        self.assertIsNone(cache.get("key"))

    def test_corrupt_cache_file_is_ignored(self) -> None:
        cache = JsonCache(self.directory)
        cache.set("key", {"ok": True})
        path = next(self.directory.glob("*.json"))
        path.write_text("{corrupt", encoding="utf-8")
        self.assertIsNone(cache.get("key"))

    def test_future_timestamp_is_treated_as_fresh(self) -> None:
        times = [2_000.0, 1_000.0]
        cache = JsonCache(
            self.directory,
            ttl=10,
            clock=lambda: times.pop(0),
        )
        cache.set("key", "value")
        self.assertEqual(cache.get("key"), "value")

    def test_set_is_best_effort_when_directory_cannot_be_created(self) -> None:
        blocker = self.directory / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        cache = JsonCache(blocker)
        self.assertFalse(cache.set("key", {"ok": True}))
        self.assertIsNone(cache.get("key"))

    def test_clear_removes_entries(self) -> None:
        cache = JsonCache(self.directory)
        cache.set("one", 1)
        cache.set("two", 2)
        self.assertEqual(cache.clear(), 2)
        self.assertIsNone(cache.get("one"))
        self.assertIsNone(cache.get("two"))
        self.assertEqual(cache.clear(), 0)

    def test_keys_are_isolated(self) -> None:
        cache = JsonCache(self.directory)
        cache.set("a", {"value": "a"})
        cache.set("b", {"value": "b"})
        self.assertEqual(cache.get("a"), {"value": "a"})
        self.assertEqual(cache.get("b"), {"value": "b"})

    def test_no_temporary_files_are_left_after_successful_write(self) -> None:
        cache = JsonCache(self.directory)
        cache.set("key", {"ok": True})
        self.assertEqual(len(list(self.directory.glob("*.json"))), 1)
        self.assertEqual(list(self.directory.glob(".tmp-*")), [])

    def test_negative_ttl_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            JsonCache(self.directory, ttl=-1)

    def test_non_finite_ttl_is_rejected(self) -> None:
        for ttl in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(ttl=ttl):
                with self.assertRaises(ValueError):
                    JsonCache(self.directory, ttl=ttl)

    def test_properties_expose_configuration(self) -> None:
        cache = JsonCache(self.directory, ttl=42)
        self.assertEqual(cache.directory, self.directory)
        self.assertEqual(cache.ttl, 42.0)


if __name__ == "__main__":
    unittest.main()
