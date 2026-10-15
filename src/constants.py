# src/constants.py
"""
Single source of truth for slot topology and dataset size.

Change SLOT_KEY_COUNTS here ONLY -- everything else derives from it.
Every module that touches keys, slots, or the 600k target imports
from here.

SAFETY: MAX_KEY_INDEX is computed, never hardcoded, so adding or
removing a slot can never leave a stale constant behind.
"""
from __future__ import annotations

# How many keys each slot gets, in slot order (slot 1 first).
# Current: 27 keys, 5 slots.
#   Slot 1 -> GEMINI_KEY_1..6
#   Slot 2 -> GEMINI_KEY_7..12
#   Slot 3 -> GEMINI_KEY_13..17
#   Slot 4 -> GEMINI_KEY_18..22
#   Slot 5 -> GEMINI_KEY_23..27
SLOT_KEY_COUNTS = [6, 6, 5, 5, 5]

# Derived -- never set these by hand.
MAX_KEY_INDEX = sum(SLOT_KEY_COUNTS)                     # = 27
SLOT_COUNT = len(SLOT_KEY_COUNTS)                        # = 5
RUN_LOCK_SLOTS = [str(i) for i in range(1, SLOT_COUNT + 1)]  # ["1","2","3","4","5"]

# Dataset size goal. Used by leaf quotas, progress %, and report bodies.
TOTAL_TARGET = 600_000