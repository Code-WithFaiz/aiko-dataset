# src/generator.py
"""
Core generation logic — slim prompt, strong rules, deterministic variant pick.

Design:
  - Prompt ~1200 tokens (was ~3500)
  - Variant picked at BUILD time (not by LLM) → smaller prompt, no choice overload
  - Hard rules FIRST, explicit forbidden words
  - Ending avoid-list to kill monotony
"""
from __future__ import annotations

import json
import logging
import os
import random
from functools import lru_cache
from pathlib import Path
from typing import Optional

from google import genai

logger = logging.getLogger(__name__)

CONFIG_DIR = Path("config")

PRIMARY_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODEL = "gemini-3.5-flash"
TEMPERATURE = float(os.getenv("TEMPERATURE", "1.1"))
TOP_P = 0.95
TOP_K = 40
MAX_OUTPUT_TOKENS = 1800

PLAYFUL_TYPES = {"playful-banter", "teasing-nakhra", "silly-random", "flirty-light"}

_ENDING_FALLBACK = {
    "soft-goodnight", "gentle-exit", "tomorrow-hook", "warm-reassurance",
    "playful-exit", "emotional-close", "question-linger", "callback-future",
    "protective-warm", "romantic", "quiet-close", "miss-you",
    "cute-nudge", "deep-close", "hopeful-close",
}


class GeminiCallError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ─────────────────────────────────────────────────────────
# Config loaders
# ─────────────────────────────────────────────────────────
@lru_cache(maxsize=None)
def load_variety() -> dict:
    return json.loads((CONFIG_DIR / "aiko_variety.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def load_tone_menu() -> dict:
    return json.loads((CONFIG_DIR / "aiko_tone_menu.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def load_category_map() -> dict:
    data = json.loads((CONFIG_DIR / "category_mapping.json").read_text(encoding="utf-8"))
    return data["tone_menu_category_to_type"]


@lru_cache(maxsize=None)
def load_endings() -> dict:
    return json.loads((CONFIG_DIR / "endings.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def load_personality() -> str:
    return (CONFIG_DIR / "personality.md").read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────
# Topic flattening
# ─────────────────────────────────────────────────────────
def flatten_topics(tree: dict) -> list[dict]:
    leaves: list[dict] = []

    def walk(node: dict, path: list[str], definitions: list[str]) -> None:
        subtopics = node.get("subtopics") or []
        new_path = path + [node["topic"]]
        new_defs = definitions + [node["definition"]]
        if not subtopics:
            leaves.append({
                "path": new_path,
                "definitions": new_defs,
                "main_subtopics_string": ". ".join(definitions) + ("." if definitions else ""),
                "core_topic": node["topic"],
                "core_definition": node["definition"],
                "leaf_path": " > ".join(new_path),
            })
            return
        for child in subtopics:
            walk(child, new_path, new_defs)

    for top in tree.get("topics", []):
        walk(top, [], [])
    return leaves


# ─────────────────────────────────────────────────────────
# Tone category picking
# ─────────────────────────────────────────────────────────
def pick_tone_category(recent: Optional[list[str]] = None) -> dict:
    tone_menu = load_tone_menu()
    cats = tone_menu["categories"]
    recent = set(recent or [])
    candidates = [c for c in cats if c["category"] not in recent]
    if not candidates:
        candidates = cats
    return random.choice(candidates)


# ─────────────────────────────────────────────────────────
# Conflict-filter helpers
# ─────────────────────────────────────────────────────────
def _mood_allowed(mood_key: str, conv_type: str, variety: dict) -> bool:
    for rule in variety["conflict_rules"]["type_vs_mood"]:
        if rule["type"] == conv_type and mood_key in rule["forbidden_moods"]:
            return False
    return True


def _arc_allowed(arc_name: str, arc_cfg: dict, conv_type: str, mood_key: str, variety: dict) -> bool:
    for rule in variety["conflict_rules"]["type_vs_arc"]:
        if rule["type"] == conv_type and arc_name in rule["forbidden_arcs"]:
            return False
    if conv_type in arc_cfg.get("never_pair_with", []):
        return False
    for rule in variety["conflict_rules"]["arc_vs_mood"]:
        if rule["arc"] == arc_name and mood_key in rule["forbidden_moods"]:
            return False
    return True


def _env_allowed(env: dict, arc_name: str, variety: dict) -> bool:
    scene_lower = env["scene"].lower()
    for rule in variety["conflict_rules"]["environment_vs_arc"]:
        if rule["environment_contains"] in scene_lower:
            if arc_name in rule["forbidden_arcs"]:
                return False
    return True


def _emoji_allowed(group_name: str, conv_type: str, variety: dict) -> bool:
    for rule in variety["conflict_rules"]["emoji_vs_type"]:
        if rule["for_type"] == conv_type and group_name in rule["forbidden_emoji_groups"]:
            return False
    return True


def _vibe_allowed(vibe: dict, conv_type: str, variety: dict) -> bool:
    for rule in variety["conflict_rules"]["reaction_vibe_vs_type"]:
        if rule["vibe_group"] == vibe.get("group") and conv_type in rule["forbidden_types"]:
            return False
    return True


def _pattern_section_allowed(section: str, conv_type: str, variety: dict) -> bool:
    for rule in variety["conflict_rules"]["playful_patterns_vs_type"]:
        if rule["patterns_section"] == section and conv_type in rule["allowed_types"]:
            return True
    return False


def _pick_mood(variety: dict, conv_type: str) -> dict:
    moods = [m for m in variety["aiko_moods"] if _mood_allowed(m["key"], conv_type, variety)]
    if not moods:
        for m in variety["aiko_moods"]:
            if m["key"] == "baseline-happy":
                return m
        return variety["aiko_moods"][0]
    return random.choice(moods)


def _pick_arc(variety: dict, conv_type: str, mood_key: str) -> dict:
    arcs = variety["arcs"]
    allowed = [
        (name, cfg) for name, cfg in arcs.items()
        if _arc_allowed(name, cfg, conv_type, mood_key, variety)
    ]
    if not allowed:
        if "share-listen-lift" in arcs:
            return {"name": "share-listen-lift", **arcs["share-listen-lift"]}
        name, cfg = next(iter(arcs.items()))
        return {"name": name, **cfg}
    name, cfg = random.choice(allowed)
    return {"name": name, **cfg}


def _pick_environment(variety: dict, arc_name: str) -> dict:
    envs = [e for e in variety["environments"] if _env_allowed(e, arc_name, variety)]
    if not envs:
        for e in variety["environments"]:
            if "shaam ka sukoon" in e["scene"]:
                return e
        return variety["environments"][0]
    return random.choice(envs)


def _pick_playful_pattern(variety: dict, conv_type: str) -> Optional[dict]:
    if conv_type not in PLAYFUL_TYPES:
        return None
    patterns = variety["playful_patterns"]
    allowed_sections = [s for s in patterns.keys() if _pattern_section_allowed(s, conv_type, variety)]
    if not allowed_sections:
        return None
    section = random.choice(allowed_sections)
    item = random.choice(patterns[section])
    return {"section": section, **item}


def _pick_reaction_vibes(variety: dict, conv_type: str, n: int = 2) -> list[dict]:
    vibes = [v for v in variety["reaction_vibes"] if _vibe_allowed(v, conv_type, variety)]
    if not vibes:
        vibes = variety["reaction_vibes"]
    n = min(n, len(vibes))
    return random.sample(vibes, n)


def _pick_emoji_group(variety: dict, conv_type: str) -> Optional[dict]:
    groups = variety["emoji_palette"]["groups"]
    allowed = [(name, cfg) for name, cfg in groups.items() if _emoji_allowed(name, conv_type, variety)]
    if not allowed:
        return None
    name, cfg = random.choice(allowed)
    return {"name": name, **cfg}


def _pick_response_shape(variety: dict) -> dict:
    shapes = variety["response_shape_variety"]["shapes"]
    return random.choice(shapes)


def pick_variety_bundle(conv_type: str) -> dict:
    variety = load_variety()
    mood = _pick_mood(variety, conv_type)
    arc = _pick_arc(variety, conv_type, mood["key"])
    env = _pick_environment(variety, arc["name"])
    pattern = _pick_playful_pattern(variety, conv_type)
    vibes = _pick_reaction_vibes(variety, conv_type, n=2)
    emoji = _pick_emoji_group(variety, conv_type)
    shape = _pick_response_shape(variety)
    type_cfg = variety["conversation_types"].get(conv_type, {})
    return {
        "type": conv_type,
        "type_cfg": type_cfg,
        "mood": mood,
        "arc": arc,
        "env": env,
        "pattern": pattern,
        "vibes": vibes,
        "emoji": emoji,
        "shape": shape,
    }


# ─────────────────────────────────────────────────────────
# Ending picking
# ─────────────────────────────────────────────────────────
def pick_ending_category(conv_type: str, env_lean: list[str]) -> dict:
    endings = load_endings()
    cats = endings["categories"]
    by_name = {c["category"]: c for c in cats}
    variety = load_variety()
    type_cfg = variety["conversation_types"].get(conv_type, {})
    prefs = set(type_cfg.get("ending_flavor_preference", []))
    env_set = set(env_lean)
    candidates = prefs & env_set
    if not candidates:
        candidates = prefs or env_set or _ENDING_FALLBACK
    candidates = {c for c in candidates if c in by_name}
    if not candidates:
        candidates = set(by_name.keys())
    picked = random.choice(list(candidates))
    return by_name[picked]


# ─────────────────────────────────────────────────────────
# Prompt sections — SLIM
# ─────────────────────────────────────────────────────────
def _guardrails_section() -> str:
    return (
        "## HARD RULES (never break)\n"
        "1. Aiko uses 'app' or 'aap' ONLY. FORBIDDEN words (never write): tum, tu, tumhe, tumko, tumhara, tumhari, tumhare, tumse, tumne, tera, teri, tere, tujhe, tujhko, tujhse. RULE: if a word starts with 'tum', 'tu', or 'ter' — STOP and rewrite it with 'app' form.\n"
        "2. Aiko speaks as 'I' — main, mujhe, mera, meri, mujhse. NEVER refer to herself as 'Aiko'.\n"
        "3. Never say you are AI, bot, model, assistant, or program. Never break character. NEVER write narration, stage directions, or actions — no *asterisk actions*, no (parentheses notes), no descriptions of what she's doing with her body. Only her spoken words. If you're about to write '(' to describe an action — STOP.\n"
        "4. Emoji: 0-2 per Aiko reply. NEVER an emoji-only reply. NEVER the same emoji twice in one conversation.\n"
        "5. Every Aiko reply is minimum 2 lines — reaction + warmth + small hook.\n"
        "6. Do NOT copy any phrasing from this prompt. Invent fresh words every line."
    )


def _tone_section(tone_cat: dict) -> str:
    variants = tone_cat.get("variants", [])
    if not variants:
        return ""
    v = random.choice(variants)
    return (
        "## THE OPENING MOMENT\n"
        f"Situation: {tone_cat['when']}\n"
        f"Her first move: {v['tone']}\n"
        f"Style: {v['behavior']}"
    )


def _bundle_section(bundle: dict) -> str:
    b = bundle
    lines = ["## THIS CONVERSATION"]
    type_cfg = b["type_cfg"]
    if type_cfg.get("aiko_tone"):
        lines.append(f"- Overall tone: {type_cfg['aiko_tone']}")
    if type_cfg.get("length"):
        lines.append(f"- Reply length: {type_cfg['length']}")
    lines.append(f"- Her mood: {b['mood']['mood']} → {b['mood']['shift']}")
    lines.append(f"- Chat flow: {b['arc']['shape']}")
    lines.append(f"- Setting: {b['env']['scene']} — {b['env']['feel']}")
    if b["pattern"]:
        lines.append(f"- Playful dynamic: {b['pattern']['feel']}")
    if b["vibes"]:
        lines.append(f"- Reaction energy: {'; '.join(v['feel'] for v in b['vibes'])}")
    if b["shape"]:
        lines.append(f"- Reply shape: {b['shape']['feel']}")
    return "\n".join(lines)


def _topic_section(leaf: dict) -> str:
    return (
        "## BACKGROUND THEME (shape the mood only — do NOT name it)\n"
        f"{leaf['core_topic']} — {leaf['core_definition']}"
    )


def _ending_section(ending_cat: dict) -> str:
    variants = ending_cat.get("variants", [])
    if not variants:
        return ""
    v = random.choice(variants)
    return (
        "## HOW IT ENDS\n"
        f"Situation: {ending_cat['when']}\n"
        f"Her last move: {v['tone']}\n"
        f"Style: {v['behavior']}\n"
        f"AVOID these closers (overused): 'so jao', 'aankh band karo', 'main hoon na', 'bojh hawale karo', 'good night app', 'subah milte hain', 'apna khayal rakhna', 'chup chaap so jao'."
    )


def _output_format_section() -> str:
    return (
        "## OUTPUT FORMAT\n"
        "Reply with ONLY the conversation. No intro, no closing, no explanation.\n"
        "- Wrap everything in { and }.\n"
        "- Every turn on its own line, starting with 'user: ' or 'Aiko: '.\n"
        "- One blank line between turns.\n"
        "- Exactly 12 turns: 6 user + 6 Aiko, alternating.\n"
        "- First turn is user. Last turn is Aiko.\n"
        "- No quotes around the speaker tags."
    )


def build_prompt(
    leaf: dict,
    tone_cat: dict,
    bundle: dict,
    ending_cat: dict,
) -> str:
    personality = load_personality()
    return "\n\n".join([
        _guardrails_section(),
        personality,
        _tone_section(tone_cat),
        _bundle_section(bundle),
        _topic_section(leaf),
        _ending_section(ending_cat),
        _output_format_section(),
    ])


# ─────────────────────────────────────────────────────────
# Gemini call
# ─────────────────────────────────────────────────────────
def _extract_text(response) -> str:
    for attr in ("output_text", "text", "content", "output"):
        val = getattr(response, attr, None)
        if isinstance(val, str) and val.strip():
            return val

    outputs = getattr(response, "outputs", None) or getattr(response, "output", None)
    if outputs is not None:
        if isinstance(outputs, str):
            return outputs
        if isinstance(outputs, list):
            parts = []
            for item in outputs:
                if isinstance(item, str):
                    parts.append(item)
                else:
                    for sub in ("text", "content", "output_text"):
                        v = getattr(item, sub, None)
                        if isinstance(v, str) and v:
                            parts.append(v)
                            break
            if parts:
                return "\n".join(parts)

    logger.warning("Could not extract text cleanly")
    logger.warning("Response repr: %r", response)
    return str(response)


def call_gemini(prompt: str, api_key: str, model: str = PRIMARY_MODEL) -> str:
    client = genai.Client(api_key=api_key)
    try:
        response = client.interactions.create(model=model, input=prompt)
    except Exception as exc:
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        try:
            response = client.interactions.create(model=model, input=prompt)
        except Exception as exc2:
            status2 = getattr(exc2, "code", None) or getattr(exc2, "status_code", None)
            raise GeminiCallError(str(exc2), status_code=status2 or status) from exc2

    text = _extract_text(response).strip()
    if not text:
        raise GeminiCallError("Empty response from Gemini", status_code=None)
    return text


def parse_conversation(text: str) -> str:
    """Normalize LLM output to canonical plain-text format."""
    import re as _re

    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [l for l in cleaned.splitlines() if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    if cleaned.startswith("{"):
        cleaned = cleaned[1:]
    if cleaned.endswith("}"):
        cleaned = cleaned[:-1]
    cleaned = cleaned.strip()

    if _re.search(r'"\s*(user|Aiko)\s*"\s*:', cleaned, _re.IGNORECASE):
        pattern = _re.compile(
            r'"\s*(user|Aiko)\s*"\s*:\s*"((?:[^"\\]|\\.)*)"',
            flags=_re.DOTALL | _re.IGNORECASE,
        )
        matches = pattern.findall(cleaned)
        parts: list[str] = []
        for speaker_raw, content in matches:
            speaker = "user" if speaker_raw.lower() == "user" else "Aiko"
            content = (
                content.replace("\\n", "\n")
                .replace('\\"', '"')
                .replace("\\'", "'")
                .strip()
            )
            if content:
                parts.append(f"{speaker}: {content}")
        if parts:
            return "{\n" + "\n\n".join(parts) + "\n}"

    if not cleaned.startswith("{"):
        cleaned = "{\n" + cleaned
    if not cleaned.endswith("}"):
        cleaned = cleaned + "\n}"
    return cleaned