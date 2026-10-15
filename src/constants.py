# src/constants.py
"""Single source of truth for slot topology and dataset size."""
from __future__ import annotations

# Slot -> key distribution (slot 1 first). Sum must equal MAX_KEY_INDEX.
#   Slot 1 -> GEMINI_KEY_1..6
#   Slot 2 -> GEMINI_KEY_7..12
#   Slot 3 -> GEMINI_KEY_13..17
#   Slot 4 -> GEMINI_KEY_18..22
#   Slot 5 -> GEMINI_KEY_23..27
SLOT_KEY_COUNTS = [6, 6, 5, 5, 5]

MAX_KEY_INDEX = sum(SLOT_KEY_COUNTS)
SLOT_COUNT = len(SLOT_KEY_COUNTS)
RUN_LOCK_SLOTS = [str(i) for i in range(1, SLOT_COUNT + 1)]

TOTAL_TARGET = 600_000