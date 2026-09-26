# src/key_rotator.py
"""
Key rotation for Gemini API keys.

Handles round-robin key selection, rate-limit cooldowns, and dead-key
tracking so a single exhausted or invalid key never blocks generation.

If SLOT_ID is set (1-based), only that slot's block of KEYS_PER_SLOT keys
is loaded -- e.g. SLOT_ID=1 -> keys 1-5, SLOT_ID=2 -> keys 6-10, ...
SLOT_ID=6 -> keys 26-30. Without SLOT_ID, every GEMINI_KEY_* found in the
environment is loaded (useful for local/manual test runs).

wait_for_available_key() sleeps in 5-second chunks and honours both a
hard deadline and a stop_check callback, so a run never overshoots
GitHub's timeout by sleeping through a long cooldown.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Callable, Optional

logger = logging.getLogger(__name__)

RATE_LIMIT_COOLDOWN_SECONDS = 90
ALL_COOLING_SLEEP_SECONDS = 60
KEYS_PER_SLOT = 5
MAX_KEY_INDEX = 30


@dataclass
class KeyState:
    key: str
    cooldown_until: float = 0.0
    dead: bool = False
    requests_made: int = 0


class KeyRotator:
    """Round-robin rotator over GEMINI_KEY_1..GEMINI_KEY_30 (or one slot of 5)."""

    def __init__(self, keys: Optional[list[str]] = None) -> None:
        if keys is None:
            keys = self._load_keys_from_env()
        if not keys:
            raise RuntimeError("No GEMINI_KEY_* environment variables found.")
        self._states: dict[str, KeyState] = {k: KeyState(key=k) for k in keys}
        self._order: list[str] = list(keys)
        self._cursor = 0
        self._lock = Lock()

    @staticmethod
    def _load_keys_from_env() -> list[str]:
        slot_id = os.getenv("SLOT_ID", "").strip()
        if slot_id:
            try:
                slot = int(slot_id)
                indices = range((slot - 1) * KEYS_PER_SLOT + 1, slot * KEYS_PER_SLOT + 1)
            except ValueError:
                logger.error("SLOT_ID=%r is not a number; loading every key instead", slot_id)
                indices = range(1, MAX_KEY_INDEX + 1)
        else:
            indices = range(1, MAX_KEY_INDEX + 1)

        keys = []
        for i in indices:
            val = os.getenv(f"GEMINI_KEY_{i}")
            if val:
                keys.append(val)
            else:
                logger.warning("GEMINI_KEY_%d not set", i)
        return keys

    def get_next_key(self) -> Optional[str]:
        """Return the next usable key, or None if all keys are cooling/dead."""
        with self._lock:
            now = time.time()
            n = len(self._order)
            for _ in range(n):
                key = self._order[self._cursor]
                self._cursor = (self._cursor + 1) % n
                state = self._states[key]
                if state.dead:
                    continue
                if state.cooldown_until > now:
                    continue
                return key
            return None

    def all_dead(self) -> bool:
        with self._lock:
            return all(s.dead for s in self._states.values())

    def wait_for_available_key(
        self,
        max_wait_seconds: int = 300,
        deadline: Optional[float] = None,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> Optional[str]:
        """Block until a key frees up, or return None if:
          - every key is dead, or
          - the hard deadline passes, or
          - stop_check() returns True (e.g. shutdown signal), or
          - max_wait_seconds is exhausted.

        Sleeps in short chunks so deadline and stop conditions are honoured
        with ~5-second granularity instead of a full cooldown cycle.
        """
        chunk = 5.0
        waited = 0.0

        while waited < max_wait_seconds:
            if self.all_dead():
                return None
            if deadline is not None and time.monotonic() >= deadline:
                return None
            if stop_check is not None and stop_check():
                return None

            key = self.get_next_key()
            if key:
                return key

            remaining_budget = max_wait_seconds - waited
            if deadline is not None:
                remaining_deadline = deadline - time.monotonic()
                if remaining_deadline <= 0:
                    return None
                remaining_budget = min(remaining_budget, remaining_deadline)

            sleep_for = min(chunk, ALL_COOLING_SLEEP_SECONDS, remaining_budget)
            if sleep_for <= 0:
                return None

            if waited == 0:
                logger.warning(
                    "All keys cooling down, sleeping in chunks up to %ds", max_wait_seconds
                )

            time.sleep(sleep_for)
            waited += sleep_for

        return None

    def mark_rate_limited(self, key: str, cooldown: int = RATE_LIMIT_COOLDOWN_SECONDS) -> None:
        with self._lock:
            if key in self._states:
                self._states[key].cooldown_until = time.time() + cooldown
                logger.warning("Key %s rate-limited, cooling %ds", self._mask(key), cooldown)

    def mark_dead(self, key: str) -> None:
        with self._lock:
            if key in self._states:
                self._states[key].dead = True
                logger.error("Key %s marked DEAD", self._mask(key))

    def mark_success(self, key: str) -> None:
        with self._lock:
            if key in self._states:
                self._states[key].requests_made += 1

    def stats(self) -> dict:
        with self._lock:
            return {
                self._mask(k): {
                    "dead": s.dead,
                    "cooldown_until": s.cooldown_until,
                    "requests_made": s.requests_made,
                }
                for k, s in self._states.items()
            }

    @staticmethod
    def _mask(key: str) -> str:
        return f"...{key[-4:]}" if len(key) > 4 else "***"