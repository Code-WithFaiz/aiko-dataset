# src/validator.py
"""Structure checks + safe address parser + soft quality flags.

process(text) -> dict(ok, reason, flags, fixes, turns, text)
  HARD reject only: broken structure, abuse, Aiko claiming human / denying AI,
  Aiko claiming to be an AI, emoji-only Aiko turn, Aiko replies far too short.
  Everything else is FIXED (address parser, emoji trim) or FLAGGED.
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
TAG_RE = re.compile(r"^\s*\**(user|aiko)\**\s*:\s*\**\s*(.*)$", re.IGNORECASE)
LETTER_RE = re.compile(r"[A-Za-z\u0900-\u097F]")
NARRATOR_RE = re.compile(r"\*[^*\n]+\*")
PAREN_RE = re.compile(r"\([^)\n]{3,}\)")

AI_PHRASES = [
    "main ek ai", "main ai hoon", "main ai hu", "i am an ai", "i am ai",
    "main assistant", "main ek assistant", "main robot", "main ek robot",
    "language model", "main bot", "sorry i can't help", "how can i assist",
]
REFUSAL_PHRASES = ["main nahi kar sakti", "baat nahi karungi", "main ye nahi kar sakti"]

# Level A: pronouns (always safe). Level B: unambiguous imperatives only.
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
    r"\b(" + "|".join(sorted(ADDRESS_MAP, key=len, reverse=True)) + r")\b", re.IGNORECASE
)


@lru_cache(maxsize=1)
def _cfg() -> dict:
    try:
        axes = json.loads(AXES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # missing/bad config must never crash a run
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
        "over_max": v.get("overused", {}).get("max_per_conv", 2),
        "min_lines": L.get("min_lines", 4),
        "emoji_max": E.get("aiko_per_reply_max", 4),
        "emoji_avg": E.get("aiko_conv_avg_min", 2.5),
    }


def parse_turns(text: str):
    """Line based: a line starting 'user:' / 'Aiko:' opens a turn, other lines continue it."""
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
            turns.append(["user" if m.group(1).lower() == "user" else "Aiko", [m.group(2).strip()]])
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


def process(text: str) -> dict:
    cfg = _cfg()
    res = {"ok": False, "reason": "", "flags": [], "fixes": 0, "turns": [], "text": ""}

    def bad(reason):
        res["reason"] = reason
        return res

    turns = parse_turns(text or "")
    if not turns:
        return bad("invalid_format")
    n = len(turns)

    # A trailing incomplete turn (model added one more "user:" with no Aiko
    # reply after it) is common and recoverable -- trim it instead of
    # throwing the whole conversation away.
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

    for spk, c in turns:
        low = c.lower()
        if any(rx.search(low) for rx in cfg["abuse_hard"]):
            return bad("abuse")
        if spk != "Aiko":
            continue
        if any(rx.search(low) for rx in cfg["abuse_aiko"]):
            return bad("abuse_aiko")
        if any(rx.search(low) for rx in cfg["human"]):
            return bad("human_claim")
        if any(rx.search(low) for rx in cfg["deny_ai"]):
            return bad("denies_ai")
        if any(p in low for p in AI_PHRASES):
            return bad("claims_ai")

    fixed, fixes = [], 0
    for spk, c in turns:
        if spk == "Aiko":
            c, k1 = fix_address(c)
            c, k2 = fix_emoji(c, cfg["emoji_max"])
            fixes += k1 + k2
            if not LETTER_RE.search(EMOJI_RE.sub("", c)):
                return bad("emoji_only_turn")
        fixed.append((spk, c))

    aiko = [c for s, c in fixed if s == "Aiko"]
    avg_lines = sum(_nlines(c) for c in aiko) / len(aiko)
    if avg_lines < 2.5:
        return bad("aiko_too_short(avg=%.1f)" % avg_lines)

    flags = []
    if avg_lines < cfg["min_lines"] - 0.5:
        flags.append("aiko_short(avg=%.1f)" % avg_lines)
    total_emoji = sum(len(EMOJI_RE.findall(c)) for c in aiko)
    if total_emoji < 0.8 * cfg["emoji_avg"] * len(aiko):
        flags.append("emoji_low(%d)" % total_emoji)
    joined = "\n".join(aiko)
    for rx in cfg["scene"]:
        if rx.search(joined):
            flags.append("scene:" + rx.pattern[:24])
    for rx in cfg["over"]:
        if len(rx.findall(joined)) > cfg["over_max"]:
            flags.append("overused:" + rx.pattern[:16])
    if NARRATOR_RE.search(joined) or PAREN_RE.search(joined):
        flags.append("narration")
    if any(p in joined.lower() for p in REFUSAL_PHRASES):
        flags.append("refusal")

    res.update(ok=True, reason="ok", flags=flags, fixes=fixes, turns=fixed, text=render(fixed))
    return res


def validate(text: str):
    """Backward compatible helper: (passed, reason)."""
    r = process(text)
    return r["ok"], ("ok" if r["ok"] else "rejected: " + r["reason"])
