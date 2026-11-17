"""A deliberately small on-disk JSON cache for live hh.ru responses.

Only one page is fetched per run, so the cache stores the decoded API payload
keyed by a hash of the request parameters.  The cache is best-effort: any
filesystem or serialisation failure makes :meth:`JsonCache.set` return False
and never interrupts a search.  Offline replay never uses the cache.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


class JsonCache:
    """TTL cache with one JSON file per hashed key."""

    def __init__(
        self,
        directory: str | Path,
        ttl: float = 300.0,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not math.isfinite(ttl) or ttl < 0:
            raise ValueError("cache TTL must be a finite, non-negative number")
        self._directory = Path(directory)
        self._ttl = float(ttl)
        self._clock = clock

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def ttl(self) -> float:
        return self._ttl

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self._directory / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        """Return the cached payload, or ``None`` if absent/expired/corrupt."""

        path = self._path_for(key)
        try:
            raw = path.read_text(encoding="utf-8")
            entry = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None

        if not isinstance(entry, dict):
            return None
        created_at = entry.get("created_at")
        if not isinstance(created_at, (int, float)):
            return None
        age = self._clock() - float(created_at)
        if age < 0:  # clock skew: treat as fresh rather than expiring forever
            age = 0.0
        if age > self._ttl:
            return None
        if "payload" not in entry:
            return None
        return entry["payload"]

    def set(self, key: str, payload: Any) -> bool:
        """Atomically write a payload.  Returns False when caching failed."""

        entry = {
            "created_at": self._clock(),
            "payload": payload,
        }
        path = self._path_for(key)
        temp_path: str | None = None
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            descriptor, temp_path = tempfile.mkstemp(
                prefix=".tmp-",
                suffix=".json",
                dir=self._directory,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False)
                handle.write("\n")
            os.replace(temp_path, path)
            return True
        except (OSError, TypeError, ValueError):
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return False

    def clear(self) -> int:
        """Delete cache entries and return how many files were removed."""

        removed = 0
        try:
            candidates = list(self._directory.glob("*.json"))
        except OSError:
            return 0
        for path in candidates:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed
