# src/key_rotator.py
"""
Key rotation for Gemini API keys.

Handles round-robin key selection, rate-limit cooldowns, and dead-key
tracking so a single exhausted or invalid key never blocks generation.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from threading import Lock
from typing import Optional

logger = logging.getLogger(__name__)

RATE_LIMIT_COOLDOWN_SECONDS = 90
ALL_COOLING_SLEEP_SECONDS = 60


@dataclass
class KeyState:
    key: str
    cooldown_until: float = 0.0
    dead: bool = False
    requests_made: int = 0


class KeyRotator:
    """Round-robin rotator over GEMINI_KEY_1..GEMINI_KEY_8."""

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
        keys = []
        for i in range(1, 17):
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

    def wait_for_available_key(self, max_wait_seconds: int = 300) -> Optional[str]:
        """Block until a key frees up, or return None after all keys are dead."""
        waited = 0
        while waited < max_wait_seconds:
            if self.all_dead():
                return None
            key = self.get_next_key()
            if key:
                return key
            logger.warning("All keys cooling down, sleeping %ds", ALL_COOLING_SLEEP_SECONDS)
            time.sleep(ALL_COOLING_SLEEP_SECONDS)
            waited += ALL_COOLING_SLEEP_SECONDS
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