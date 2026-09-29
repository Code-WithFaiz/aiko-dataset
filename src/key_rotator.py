# src/key_rotator.py
"""
Key rotation for Gemini API keys.

Handles round-robin key selection, rate-limit cooldowns, dead-key
tracking, and a per-slot circuit breaker for server-side outages.

If SLOT_ID is set (1-based), only that slot's block of KEYS_PER_SLOT keys
is loaded -- e.g. SLOT_ID=1 -> keys 1-6, SLOT_ID=2 -> keys 7-12, ...
SLOT_ID=5 -> keys 26-30. Without SLOT_ID, every GEMINI_KEY_* found in the
environment is loaded (useful for local/manual test runs).

CIRCUIT BREAKER
----------------
When every key in this slot has returned a 5xx (500/503) at least once
SINCE the last successful call, the circuit opens. While open:
  - get_next_key() and wait_for_available_key() refuse to return any key
  - the caller (main._call_with_key_rotation) bails out with reason
    "server_side_down" so the slot shuts down gracefully without
    hammering the API further.

A single success resets the whole wave: the circuit closes and every key
is available again. Short server blips are tolerated; only a sustained
outage trips the breaker.
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
    server_error_seen: bool = False


class KeyRotator:
    """Round-robin rotator over GEMINI_KEY_1..GEMINI_KEY_30 (or one slot of 6)."""

    def __init__(self, keys: Optional[list[str]] = None) -> None:
        if keys is None:
            keys = self._load_keys_from_env()
        if not keys:
            raise RuntimeError("No GEMINI_KEY_* environment variables found.")
        self._states: dict[str, KeyState] = {k: KeyState(key=k) for k in keys}
        self._order: list[str] = list(keys)
        self._cursor = 0
        self._lock = Lock()
        # Circuit breaker state
        self._server_error_keys: set[str] = set()
        self._circuit_open = False

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

    def total_keys(self) -> int:
        return len(self._order)

    def server_error_count(self) -> int:
        with self._lock:
            return len(self._server_error_keys)

    def is_circuit_open(self) -> bool:
        with self._lock:
            return self._circuit_open

    def get_next_key(self) -> Optional[str]:
        """Return the next usable key, or None if none is currently usable.

        A key is skipped if it is dead, cooling down, or has already
        returned a 5xx since the last success (server-error wave).
        """
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
                if key in self._server_error_keys:
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
          - the circuit breaker has opened, or
          - the hard deadline passes, or
          - stop_check() returns True (e.g. shutdown signal), or
          - max_wait_seconds is exhausted.

        Sleeps in short chunks so deadline, circuit state, and stop
        conditions are honoured with ~5-second granularity.
        """
        chunk = 5.0
        waited = 0.0

        while waited < max_wait_seconds:
            if self.all_dead():
                return None
            if self.is_circuit_open():
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

    def mark_server_error(self, key: str) -> None:
        """Record a 5xx response from this key. When every key in the slot
        has reported a 5xx since the last success, open the circuit."""
        with self._lock:
            if key not in self._states:
                return
            self._states[key].server_error_seen = True
            self._server_error_keys.add(key)
            if len(self._server_error_keys) >= len(self._order):
                if not self._circuit_open:
                    self._circuit_open = True
                    logger.critical(
                        "Circuit breaker OPEN — all %d keys returned 5xx",
                        len(self._order),
                    )

    def mark_success(self, key: str) -> None:
        with self._lock:
            if key in self._states:
                self._states[key].requests_made += 1
            # A success closes the circuit and clears the wave.
            if self._server_error_keys or self._circuit_open:
                logger.info(
                    "Success on %s — resetting server-error wave (was %d keys)",
                    self._mask(key), len(self._server_error_keys),
                )
                self._server_error_keys.clear()
                self._circuit_open = False
            for s in self._states.values():
                s.server_error_seen = False

    def stats(self) -> dict:
        with self._lock:
            return {
                self._mask(k): {
                    "dead": s.dead,
                    "cooldown_until": s.cooldown_until,
                    "requests_made": s.requests_made,
                    "server_error_seen": s.server_error_seen,
                }
                for k, s in self._states.items()
            }

    @staticmethod
    def _mask(key: str) -> str:
        return f"...{key[-4:]}" if len(key) > 4 else "***"