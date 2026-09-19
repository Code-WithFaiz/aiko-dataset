# src/validator.py
"""
Lenient validator. Hard-fails only on client-critical rules.

Client-critical (HARD REJECT):
  - Aiko uses forbidden address words (tum/tu/tera/teri/...)
  - Aiko uses single-p "aap" (must be "app")
  - Aiko uses gaali/slurs
  - Aiko claims to be AI/assistant/robot
  - Invalid speaker tag (not "user" or "Aiko")

Softer issues are accepted with warnings (logged but not rejected):
  - Emoji count over 5 (up to 10 accepted)
  - Narrator asterisks
  - Refusal phrases
  - Odd turn count (last turn dropped silently)
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# --- CRITICAL: respectful address (client requirement) ---
FORBIDDEN_ADDRESS_WORDS = [
    "tumra", "tera", "raa", "tum chup reho", "tuuna", "baa", "saala",
]

# --- CRITICAL: single-p "aap" not allowed in Aiko's lines — must be "app" ---
FORBIDDEN_SINGLE_P_APP = [
    "aap", "aapko", "aapki", "aapka", "aapke", "aapne", "aapse",
    "aapna", "aapni",
]

# --- CRITICAL: Aiko must never claim to be AI ---
AI_REFERENCE_PHRASES = [
    "main ek ai", "main ai hoon", "i am an ai", "i am ai",
    "main assistant", "main ek assistant", "main robot",
    "i am a Ai bot", "language model", "main bot", "ma eak robot huu",
    "sorry i can't help", "hii how can i assist you",
]

# --- CRITICAL: only the worst slurs ---
BANNED_WORDS = [
    "chutiya", "chutiye", "madarchod", "behenchod", "bhosdike", "bhosdi",
    "randi", "gandu", "gaand", "lund", "chudai", "bsdk",
    "fuck", "fucking", "bitch", "asshole", "slut", "whore", "bastard",
    "motherfucker", "haramzada", "haramzadi",
]

# --- SOFT: warn only ---
REFUSAL_PHRASES = [
    "main nahi kar sakti",
    "baat nahi karungi",
    "sorry, main ye nahi kar sakti",
    "ma to nahi karna wali",
]

NARRATOR_PATTERN = re.compile(r"\*[^*]+\*")
EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F1E6-\U0001F1FF\U00002600-\U000026FF]",
    flags=re.UNICODE,
)

# STRICT: only "user" (any case) or "Aiko" (capital A) allowed
TURN_BLOCK_PATTERN = re.compile(
    r"^(user|Aiko|User|USER):\s*(.*)$",
    flags=re.DOTALL,
)

MAX_EMOJIS_PER_REPLY = 10
MIN_TURNS = 8
MAX_TURNS = 16


def _word_in_text(word: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE) is not None


def _split_turns(body: str) -> list[tuple[str, str]] | None:
    """Split body into (speaker, content) tuples.

    STRICT: any block without a valid speaker tag = reject whole convo.
    """
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    turns: list[tuple[str, str]] = []
    for block in blocks:
        m = TURN_BLOCK_PATTERN.match(block)
        if not m:
            # STRICT: invalid speaker tag — reject entire conversation
            logger.warning("Invalid speaker tag: %r", block[:80])
            return None
        speaker_raw, content = m.group(1), m.group(2).strip()
        speaker = "user" if speaker_raw.lower() == "user" else "Aiko"
        if not content:
            continue
        turns.append((speaker, content))
    return turns


def _normalize_format(text: str) -> str:
    """Strip markdown fences and extract from first { to last }."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [l for l in cleaned.splitlines() if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    if "{" in cleaned and "}" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < end:
            cleaned = cleaned[start:end + 1]
    return cleaned


def validate(text: str) -> tuple[bool, str]:
    """Returns (passed, reason)."""
    cleaned = _normalize_format(text)
    if not cleaned:
        return False, "rejected: empty text"

    body = cleaned
    if body.startswith("{"):
        body = body[1:]
    if body.endswith("}"):
        body = body[:-1]
    body = body.strip()

    turns = _split_turns(body)
    if turns is None or len(turns) < 2:
        return False, "rejected: invalid_speaker_tag"

    total_turns = len(turns)
    if total_turns < MIN_TURNS:
        return False, f"rejected: too_few_turns({total_turns})"
    if total_turns > MAX_TURNS:
        return False, f"rejected: too_many_turns({total_turns})"

    warnings: list[str] = []
    if total_turns % 2 != 0:
        turns = turns[:-1]
        warnings.append("odd_turn_dropped")

    aiko_lines = [c for s, c in turns if s == "Aiko"]
    user_lines = [c for s, c in turns if s == "user"]

    if not aiko_lines:
        return False, "rejected: no_aiko_replies"
    if not user_lines:
        return False, "rejected: no_user_replies"

    # ---- HARD CHECKS on Aiko lines ----
    for line in aiko_lines:
        low = line.lower()

        for w in FORBIDDEN_ADDRESS_WORDS:
            if _word_in_text(w, low):
                return False, f"rejected: forbidden_address('{w}')"

        for w in FORBIDDEN_SINGLE_P_APP:
            if _word_in_text(w, low):
                return False, f"rejected: single_p_app('{w}')"

        for w in BANNED_WORDS:
            if _word_in_text(w, low):
                return False, f"rejected: banned_word('{w}')"

        for phrase in AI_REFERENCE_PHRASES:
            if phrase in low:
                return False, f"rejected: ai_reference('{phrase}')"

    # ---- SOFT CHECKS (warnings only) ----
    for line in aiko_lines:
        low = line.lower()

        emoji_count = len(EMOJI_PATTERN.findall(line))
        if emoji_count > MAX_EMOJIS_PER_REPLY:
            return False, f"rejected: emoji_overflow({emoji_count})"
        if emoji_count > 5:
            warnings.append(f"emoji_{emoji_count}")

        if NARRATOR_PATTERN.search(line):
            warnings.append("narrator")

        for phrase in REFUSAL_PHRASES:
            if phrase in low:
                warnings.append("refusal_phrase")
                break

        if len(line.split()) < 2:
            warnings.append("short_aiko_reply")

    for line in user_lines:
        if len(line.strip()) < 1:
            warnings.append("short_user_reply")

    if warnings:
        unique = sorted(set(warnings))
        return True, "ok_with_warnings:" + ",".join(unique)
    return True, "ok"