# src/dedup.py
"""
Non-repeat / duplicate detection.
"""
from __future__ import annotations

import hashlib
import re

AIKO_LINE_PATTERN = re.compile(r"Aiko:\s*(.+?)(?=\n\n|\Z)", flags=re.DOTALL | re.IGNORECASE)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def hash_text(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def _extract_aiko_lines(text: str) -> list[str]:
    return [l.strip() for l in AIKO_LINE_PATTERN.findall(text) if l.strip()]


def signature(text: str) -> str:
    """First 30 words of first Aiko reply + last 20 words of last Aiko reply."""
    aiko_lines = _extract_aiko_lines(text)
    if not aiko_lines:
        return normalize(text)[:200]
    first_words = normalize(aiko_lines[0]).split()[:30]
    last_words = normalize(aiko_lines[-1]).split()[-20:]
    return " ".join(first_words) + " ||| " + " ".join(last_words)


def opening_text(text: str) -> str:
    aiko_lines = _extract_aiko_lines(text)
    return normalize(aiko_lines[0]) if aiko_lines else ""


def jaccard_similarity(a: str, b: str) -> float:
    set_a, set_b = set(a.split()), set(b.split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def is_duplicate(
    text: str,
    hash_exists_fn,
    recent_signatures: list[str],
    recent_openings: list[str],
) -> tuple[bool, str]:
    if hash_exists_fn(hash_text(text)):
        return True, "exact_hash_duplicate"

    sig = signature(text)
    for existing in recent_signatures:
        if jaccard_similarity(sig, existing) > 0.90:
            return True, "signature_similarity>0.90"

    opening = opening_text(text)
    for existing in recent_openings:
        if opening == existing:
            return True, "exact_opening_duplicate"
        if jaccard_similarity(opening, existing) > 0.95:
            return True, "opening_similarity>0.95"

    return False, "unique"


# ---- Within-run repeated-phrase guard ----
# In-memory only: resets every GitHub Actions run (process restarts each
# time). It catches Aiko reusing the same 4-word phrase too often inside
# one ~50-minute run. Catching repeats ACROSS days needs a small MongoDB
# counter in db.py -- worth adding once the new prompt has been tested and
# you can see whether repeats are still a problem.
_PHRASE_COUNTS: dict[str, int] = {}
PHRASE_N = 4
PHRASE_OVERUSE_LIMIT = 3


def _phrase_ngrams(text: str, n: int = PHRASE_N) -> list[str]:
    words = normalize("\n".join(_extract_aiko_lines(text))).split()
    return [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]


def check_and_record_phrases(text: str, limit: int = PHRASE_OVERUSE_LIMIT) -> tuple[bool, str]:
    """Returns (is_overused, phrase). Call only on a conversation you are
    about to accept -- it checks AND records in the same step."""
    grams = set(_phrase_ngrams(text))
    for g in grams:
        if _PHRASE_COUNTS.get(g, 0) >= limit:
            return True, g
    for g in grams:
        _PHRASE_COUNTS[g] = _PHRASE_COUNTS.get(g, 0) + 1
    return False, ""
