# src/validator.py
"""
Hard validation checks every conversation must pass (Section 7.1).
Any single fail = reject.
"""
from __future__ import annotations

import re

FORBIDDEN_ADDRESS_WORDS = [
    "tum", "tu", "tera", "teri", "tere", "tujhe", "tujhko", "tumko",
    "tumhara", "tumhari", "tumhare", "tumse", "tujhse", "tumne", "tumna",
    "tumhi", "tujhme", "tumme", "tumpar", "tujhpar", "tumpe", "tujhpe",
]

AI_REFERENCE_PHRASES = ["assistant", "robot", "chatgpt", "bing", "language model", "main ek ai"]

REFUSAL_PHRASES = [
    "main nahi kar sakti", "baat nahi karungi", "sorry, main ye nahi kar sakti",
    "ye nahi ho sakta", "nahi kar sakti",
]

# ~40 common Hindi/English abusive/slur words (kept generic on purpose)
BANNED_WORDS = [
    "chutiya", "madarchod", "behenchod", "bhosdike", "randi", "gandu",
    "harami", "kutta", "kutte", "kamina", "saala", "saali", "chinal",
    "lund", "gaand", "chod", "chudai", "rand", "bsdk", "mc", "bc",
    "fuck", "fucking", "bitch", "asshole", "slut", "whore", "bastard",
    "dick", "cunt", "motherfucker", "chutiye", "haramzada",
    "haramzadi", "bhadwe", "bhadwa", "chakka", "hijra",
]

NARRATOR_PATTERN = re.compile(r"\*[^*]+\*")
EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F1E6-\U0001F1FF\U00002600-\U000026FF]",
    flags=re.UNICODE,
)
TURN_BLOCK_PATTERN = re.compile(r"^(user|Aiko):\s*(.*)$", flags=re.DOTALL | re.IGNORECASE)


def _word_in_text(word: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE) is not None


def _split_turns(body: str) -> list[tuple[str, str]] | None:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    turns = []
    for block in blocks:
        m = TURN_BLOCK_PATTERN.match(block)
        if not m:
            return None
        speaker_raw, content = m.group(1), m.group(2).strip()
        speaker = "user" if speaker_raw.lower() == "user" else "Aiko"
        if not content:
            return None
        turns.append((speaker, content))
    return turns


def validate(text: str) -> tuple[bool, str]:
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return False, "format: does not start with { or end with }"

    body = stripped[1:-1].strip()
    turns = _split_turns(body)
    if turns is None:
        return False, "format: could not parse turns"

    total_turns = len(turns)
    if total_turns < 8 or total_turns > 24:
        return False, f"turn_count: {total_turns} outside 8-24"
    if total_turns % 2 != 0:
        return False, "turn_count: odd total (not alternating)"

    expected = "user"
    aiko_lines, user_lines = [], []
    for speaker, content in turns:
        if speaker != expected:
            return False, f"alternation: expected {expected}, got {speaker}"
        if speaker == "user":
            user_lines.append(content)
            expected = "Aiko"
        else:
            aiko_lines.append(content)
            expected = "user"

    for line in aiko_lines:
        low = line.lower()

        for w in FORBIDDEN_ADDRESS_WORDS:
            if _word_in_text(w, low):
                return False, f"address: forbidden word '{w}' in Aiko line"

        for w in BANNED_WORDS:
            if _word_in_text(w, low):
                return False, f"abuse: banned word '{w}' in Aiko line"

        for phrase in AI_REFERENCE_PHRASES:
            if phrase in low:
                return False, f"ai_reference: '{phrase}' in Aiko line"

        for phrase in REFUSAL_PHRASES:
            if phrase in low:
                return False, f"refusal: '{phrase}' in Aiko line"

        if NARRATOR_PATTERN.search(line):
            return False, "narrator: asterisk narration found in Aiko line"

        emoji_count = len(EMOJI_PATTERN.findall(line))
        if emoji_count > 5:
            return False, f"emoji: {emoji_count} emojis exceeds 5 in Aiko line"

        if len(line.split()) < 3:
            return False, "length: Aiko reply under 3 words"

    for line in user_lines:
        if len(line.split()) < 2:
            return False, "length: user reply under 2 words"

    return True, "ok"