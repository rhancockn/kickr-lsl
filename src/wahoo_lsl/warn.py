"""Thread-safe warn-once helper."""
from __future__ import annotations

import logging
import threading
from collections.abc import Hashable


class WarnOnce:
    """Emit a WARNING the first time a key is seen; count every occurrence."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self._counts: dict[Hashable, int] = {}

    def check(self, key: Hashable) -> bool:
        """Record one occurrence of ``key``. Return True only for the first occurrence."""
        with self._lock:
            n = self._counts.get(key, 0)
            self._counts[key] = n + 1
        return n == 0

    def warn(self, key: Hashable, msg: str, *args: object) -> bool:
        """Log ``msg % args`` at WARNING the first time ``key`` is seen. Return True if logged."""
        first = self.check(key)
        if first:
            self._logger.warning(msg, *args)
        return first

    def counts(self) -> dict[Hashable, int]:
        with self._lock:
            return dict(self._counts)
