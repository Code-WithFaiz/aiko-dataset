# src/validator.py
"""Structure checks + safe address parser + soft quality flags.

process(text) -> dict(ok, reason, flags, fixes, turns, text)

HARD reject:
  - broken structure (turns / alternation)
  - abuse, AI claims, human claims, denies_ai
  - narration (asterisk or parenthetical) in Aiko's lines -- NO MERCY
  - emoji-only Aiko turns (2+)
  - no Aiko turn reaches min length

Auto-fixes (silent):
  - double speaker tags (Aiko:Aiko: / User:Aiko: / etc.)
  - address forms (tum/tera -> aap family)
  - emoji overflow

Soft flags (kept conservative -- only real quality concerns):
  - single stray emoji-only turn
  - emoji density very low

Word lists live in config/aiko_axes.json (validator section).
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

AXES_PATH = Path(__file__).resolve().parent.parent / "config" / "aiko_axes.json"
MIN_TURNS = 8
MAX_TURNS = 24

EMOJI_CLASS = "\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF"
EMOJI_RE = re.compile("[" + EMOJI_CLASS + "]")
TOKEN = "[" + EMOJI_CLASS + "][\ufe0f]?"
EMOJI_RUN_RE = re.compile("(?:" + TOKEN + r"[ \t]?){3,}")

# Single speaker tag at start of a line
TAG_RE = re.compile(r"^\s*\**(user|aiko)\**\s*:\s*\**\s*(.*)$", re.IGNORECASE)

# Two consecutive speaker tags at start of a line (same or different)
# Requires BOTH tags to be immediately followed by a colon, so
# "Aiko: user name kya hai" does NOT match ("user" not followed by colon there).
DOUBLE_TAG_RE = re.compile(
    r"^\s*\**(user|aiko)\**\s*:\s*\**\s*\**(user|aiko)\**\s*:\s*(.*)$",
    re.IGNORECASE,
)

LETTER_RE = re.compile(r"[A-Za-z\u0900-\u097F]")

# Narration patterns -- HARD reject in Aiko's lines.
#  *text*           -> asterisk-wrapped action
#  word*  (EOL)     -> trailing asterisk (e.g. "saans*")
NARRATOR_RE = re.compile(r"\*[^*\n]+\*|\b[A-Za-z]+\*\s*$", re.MULTILINE)
# (parenthetical of 8+ chars) -- long parenthetical = action-like
PAREN_RE = re.compile(r"\([^)\n]{8,}\)")

AI_PHRASES = [
    "main ek ai", "main ai hoon", "main ai hu", "i am an ai", "i am ai",
    "main assistant", "main ek assistant", "main robot", "main ek robot",
    "language model", "main bot", "sorry i can't help", "how can i assist",
]

# Narrow refusal list -- only TRUE bot-style refusals.
# Aiko's playful "baat nahi karungi" / "main nahi kar sakti" is natural Hinglish
# and must NOT be flagged.
REFUSAL_PHRASES = ["main ye nahi kar sakti", "sorry main nahi kar", "help nahi kar sakti"]

ADDRESS_MAP = {
    "tum": "aap", "tu": "aap", "tumhe": "aapko", "tumhein": "aapko", "tujhe": "aapko",
    "tumko": "aapko", "tujhko": "aapko", "tumse": "aapse", "tujhse": "aapse",
    "tumne": "aapne", "tumhi": "aap hi", "tera": "aapka", "tumhara": "aapka",
    "teri": "aapki", "tumhari": "aapki", "tere": "aapke", "tumhare": "aapke",
    "tujhme": "aap mein", "tumme": "aap mein", "tumpe": "aap par", "tujhpe": "aap par",
    "app": "aap", "appka": "aapka", "appki": "aapki", "appke": "aapke",
    "appko": "aapko", "appse": "aapse", "appne": "aapne",
    "suno": "suniye", "batao": "bataiye", "dekho": "dekhiye", "baitho": "baithiye",
    "ruko": "rukiye", "utho": "uthiye", "khao": "khaiye", "likho": "likhiye",
    "socho": "sochiye", "samjho": "samjhiye", "bolo": "boliye", "aao": "aaiye", "jao": "jaiye",
}
ADDRESS_RE = re.compile(
    r"\b(" + "|".join(sorted(ADDRESS_MAP, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


@lru_cache(maxsize=1)
def _cfg() -> dict:
    try:
        axes = json.loads(AXES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("aiko_axes.json unreadable (%s); using empty lists", exc)
        axes = {}
    v = axes.get("validator", {})
    L = axes.get("lengths", {}).get("aiko", {})
    E = axes.get("emoji", {})

    def comp(items):
        return [re.compile(p, re.IGNORECASE) for p in items]

    return {
        "abuse_hard": comp(v.get("abuse_hard", [])),
        "abuse_aiko": comp(v.get("abuse_aiko", [])),
        "human": comp(v.get("human_claim", [])),
        "deny_ai": comp(v.get("deny_ai", [])),
        "scene": comp(v.get("flag_patterns", [])),
        "over": comp(v.get("overused", {}).get("stems", [])),
        "over_max": v.get("overused", {}).get("max_per_conv", 3),
        "min_lines": L.get("min_lines", 4),
        "emoji_max": E.get("aiko_per_reply_max", 4),
        "emoji_avg": E.get("aiko_conv_avg_min", 2.5),
    }


# ─────────────────────────────────────────────────────────
# Double-tag fixer (silent -- no flag emitted)
# ─────────────────────────────────────────────────────────
def fix_double_tags(text: str) -> tuple[str, int]:
    """Collapse 'Aiko:Aiko:', 'Aiko: Aiko:', 'User:Aiko:' etc. to a single tag.

    Resolution:
      same tag twice        -> keep it
      prev turn was user    -> keep aiko
      prev turn was aiko    -> keep user
      no previous context   -> keep first tag
    Idempotent: running twice produces the same output.
    """
    lines = text.split("\n")
    out: list[str] = []
    prev_tag: str | None = None
    fixed = 0

    for line in lines:
        m = DOUBLE_TAG_RE.match(line)
        if m:
            tag1 = m.group(1).lower()
            tag2 = m.group(2).lower()
            content = m.group(3)

            if tag1 == tag2:
                final = tag1
            elif prev_tag == "user":
                final = "aiko"
            elif prev_tag == "aiko":
                final = "user"
            else:
                final = tag1

            display = "Aiko" if final == "aiko" else "user"
            out.append(f"{display}: {content}")
            prev_tag = final
            fixed += 1
        else:
            sm = TAG_RE.match(line)
            if sm:
                prev_tag = sm.group(1).lower()
            out.append(line)

    return "\n".join(out), fixed


# ─────────────────────────────────────────────────────────
# Turn parsing / rendering
# ─────────────────────────────────────────────────────────
def parse_turns(text: str):
    """Line-based: a line starting 'user:' / 'Aiko:' opens a turn;
    other lines continue it. Double tags are collapsed first."""
    text, _ = fix_double_tags(text)

    lines = [l for l in text.strip().splitlines() if not l.strip().startswith("```")]
    body = "\n".join(lines).strip()
    if body.startswith("{"):
        body = body[1:]
    if body.endswith("}"):
        body = body[:-1]

    turns: list[list] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = TAG_RE.match(line)
        if m:
            turns.append([
                "user" if m.group(1).lower() == "user" else "Aiko",
                [m.group(2).strip()],
            ])
        elif turns:
            turns[-1][1].append(line)
        else:
            return None

    out = []
    for spk, parts in turns:
        content = "\n".join(p for p in parts if p)
        if content:
            out.append((spk, content))
    return out


def render(turns) -> str:
    return "\n\n".join("%s: %s" % (s, c) for s, c in turns)


# ─────────────────────────────────────────────────────────
# Auto-fixers
# ─────────────────────────────────────────────────────────
def fix_address(text: str):
    count = 0

    def sub(m):
        nonlocal count
        count += 1
        src, rep = m.group(1), ADDRESS_MAP[m.group(1).lower()]
        if len(src) > 1 and src.isupper():
            return rep.upper()
        return rep[0].upper() + rep[1:] if src[0].isupper() else rep

    return ADDRESS_RE.sub(sub, text), count


def fix_emoji(text: str, max_n: int):
    changed = 0

    def collapse(m):
        nonlocal changed
        toks = re.findall(TOKEN, m.group(0))
        changed += 1
        return "".join(toks[:2]) + (" " if m.group(0).endswith(" ") else "")

    text = EMOJI_RUN_RE.sub(collapse, text)
    seen = 0

    def cap(m):
        nonlocal seen, changed
        seen += 1
        if seen > max_n:
            changed += 1
            return ""
        return m.group(0)

    text = re.sub(TOKEN, cap, text)
    if changed:
        text = "\n".join(re.sub(r"[ \t]{2,}", " ", l).rstrip() for l in text.split("\n"))
    return text, changed


def _nlines(content: str) -> int:
    lines = [l for l in content.split("\n") if l.strip()]
    if len(lines) >= 2:
        return len(lines)
    if not lines:
        return 0
    return max(1, len(re.findall(r"[.!?\u2026]+(?:\s|$)", lines[0])))


# ─────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────
def process(text: str) -> dict:
    cfg = _cfg()
    res = {"ok": False, "reason": "", "flags": [], "fixes": 0, "turns": [], "text": ""}

    def bad(reason):
        res["reason"] = reason
        return res

    # Fix double tags before parsing (silent -- not counted as a flag)
    text, dtag_fixes = fix_double_tags(text or "")

    turns = parse_turns(text)
    if not turns:
        return bad("invalid_format")
    n = len(turns)

    if n % 2 and turns[-1][0] == "user":
        turns = turns[:-1]
        n -= 1

    if n < MIN_TURNS:
        return bad("too_few_turns(%d)" % n)
    if n > MAX_TURNS:
        return bad("too_many_turns(%d)" % n)
    if n % 2:
        return bad("odd_turns(%d)" % n)
    for i, (spk, _) in enumerate(turns):
        if spk != ("user" if i % 2 == 0 else "Aiko"):
            return bad("bad_alternation(turn %d)" % i)

    # Hard checks
    for spk, c in turns:
        low = c.lower()
        if any(rx.search(low) for rx in cfg["abuse_hard"]):
            return bad("abuse")
        if spk != "Aiko":
            continue

        # NARRATION -> NO MERCY
        if NARRATOR_RE.search(c) or PAREN_RE.search(c):
            return bad("narration_detected")

        if any(rx.search(low) for rx in cfg["abuse_aiko"]):
            return bad("abuse_aiko")
        if any(rx.search(low) for rx in cfg["human"]):
            return bad("human_claim")
        if any(rx.search(low) for rx in cfg["deny_ai"]):
            return bad("denies_ai")
        if any(p in low for p in AI_PHRASES):
            return bad("claims_ai")

    # Auto-fixes
    fixed, fixes, empty_turns = [], dtag_fixes, 0
    for spk, c in turns:
        if spk == "Aiko":
            c, k1 = fix_address(c)
            c, k2 = fix_emoji(c, cfg["emoji_max"])
            fixes += k1 + k2
            if not LETTER_RE.search(EMOJI_RE.sub("", c)):
                empty_turns += 1
        fixed.append((spk, c))

    if empty_turns >= 2:
        return bad("emoji_only_turns(%d)" % empty_turns)

    aiko = [c for s, c in fixed if s == "Aiko"]
    line_counts = [_nlines(c) for c in aiko]
    best_turn = max(line_counts)
    if best_turn < 3:
        return bad("no_turn_reaches_min_length(best=%d)" % best_turn)

    # Soft flags -- kept conservative to avoid false positives on gold data.
    flags = []
    # NOTE: double-tag fixes are applied silently. Not a quality problem.
    if empty_turns == 1:
        flags.append("emoji_only_turn_once")

    total_emoji = sum(len(EMOJI_RE.findall(c)) for c in aiko)
    if total_emoji < 0.5 * len(aiko):
        flags.append("emoji_low(%d)" % total_emoji)

    joined = "\n".join(aiko)
    for rx in cfg["scene"]:
        if rx.search(joined):
            flags.append("scene:" + rx.pattern[:24])
    for rx in cfg["over"]:
        if len(rx.findall(joined)) > cfg["over_max"]:
            flags.append("overused:" + rx.pattern[:16])

    if any(p in joined.lower() for p in REFUSAL_PHRASES):
        flags.append("refusal")

    res.update(
        ok=True,
        reason="ok",
        flags=flags,
        fixes=fixes,
        turns=fixed,
        text=render(fixed),
    )
    return res


def validate(text: str):
    r = process(text)
    return r["ok"], ("ok" if r["ok"] else "rejected: " + r["reason"])