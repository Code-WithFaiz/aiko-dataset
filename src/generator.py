# src/generator.py
"""
Core generation logic — optimized.

Pipeline:
  1. Load configs (cached once, @lru_cache)
  2. Pick tone category (51 options, random)
  3. Map to conversation_type (category_mapping.json)
  4. Pick variety bundle (mood, arc, env, pattern, vibes, emoji, gesture, shape)
  5. Pick ending category (filtered by type preference + env lean)
  6. Build prompt from TONE HINTS only — no phrases, no examples
  7. Call Gemini
  8. Parse & return
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

# ─────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────
CONFIG_DIR = Path("config")

PRIMARY_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODEL = "gemini-3.5-flash"
TEMPERATURE = float(os.getenv("TEMPERATURE", "1.1"))
TOP_P = 0.95
TOP_K = 40
MAX_OUTPUT_TOKENS = 3000

PLAYFUL_TYPES = {"playful-banter", "teasing-nakhra", "silly-random", "flirty-light"}

# All 15 ending categories (used as final fallback)
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
# Config loaders (cached once per process)
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
# Topic flattening (unchanged)
# ─────────────────────────────────────────────────────────
def flatten_topics(tree: dict) -> list[dict]:
    """DFS-flatten topics.json into leaf records."""
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
    """Pick ONE tone category (from 51), avoiding recent ones.
    Returns the full category dict (with variants)."""
    tone_menu = load_tone_menu()
    cats = tone_menu["categories"]
    recent = set(recent or [])

    candidates = [c for c in cats if c["category"] not in recent]
    if not candidates:
        candidates = cats
    return random.choice(candidates)


# ─────────────────────────────────────────────────────────
# Variety bundle picking
# ─────────────────────────────────────────────────────────
def _mood_allowed(mood_key: str, conv_type: str, variety: dict) -> bool:
    for rule in variety["conflict_rules"]["type_vs_mood"]:
        if rule["type"] == conv_type and mood_key in rule["forbidden_moods"]:
            return False
    return True


def _arc_allowed(arc_name: str, arc_cfg: dict, conv_type: str, mood_key: str, variety: dict) -> bool:
    # type_vs_arc
    for rule in variety["conflict_rules"]["type_vs_arc"]:
        if rule["type"] == conv_type and arc_name in rule["forbidden_arcs"]:
            return False
    # arc's own never_pair_with
    if conv_type in arc_cfg.get("never_pair_with", []):
        return False
    # arc_vs_mood
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
        # universal fallback
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
        # universal fallback
        if "share-listen-lift" in arcs:
            return {"name": "share-listen-lift", **arcs["share-listen-lift"]}
        name, cfg = next(iter(arcs.items()))
        return {"name": name, **cfg}
    name, cfg = random.choice(allowed)
    return {"name": name, **cfg}


def _pick_environment(variety: dict, arc_name: str) -> dict:
    envs = [e for e in variety["environments"] if _env_allowed(e, arc_name, variety)]
    if not envs:
        # universal fallback
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


def _pick_reaction_vibes(variety: dict, conv_type: str, n: int = 3) -> list[dict]:
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


def _pick_gesture(variety: dict, conv_type: str) -> Optional[dict]:
    pool = variety["gesture_pool"]["gestures"]
    # filter by used_in (which references tone_menu categories, not conv types — so we match loosely)
    # Actually gesture.used_in references tone_menu cats, so we can't filter by conv_type directly.
    # Just pick randomly, letting the LLM decide if it fits.
    if not pool:
        return None
    return random.choice(pool)


def _pick_response_shape(variety: dict) -> dict:
    shapes = variety["response_shape_variety"]["shapes"]
    return random.choice(shapes)


def pick_variety_bundle(conv_type: str) -> dict:
    """Pick a full variety bundle for one generation."""
    variety = load_variety()

    mood = _pick_mood(variety, conv_type)
    arc = _pick_arc(variety, conv_type, mood["key"])
    env = _pick_environment(variety, arc["name"])
    pattern = _pick_playful_pattern(variety, conv_type)
    vibes = _pick_reaction_vibes(variety, conv_type, n=3)
    emoji = _pick_emoji_group(variety, conv_type)
    gesture = _pick_gesture(variety, conv_type)
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
        "gesture": gesture,
        "shape": shape,
    }


# ─────────────────────────────────────────────────────────
# Ending picking
# ─────────────────────────────────────────────────────────
def pick_ending_category(conv_type: str, env_lean: list[str]) -> dict:
    """Pick ONE ending category, filtered by type preference + env lean."""
    endings = load_endings()
    cats = endings["categories"]
    by_name = {c["category"]: c for c in cats}

    variety = load_variety()
    type_cfg = variety["conversation_types"].get(conv_type, {})
    prefs = set(type_cfg.get("ending_flavor_preference", []))
    env_set = set(env_lean)

    # intersection first
    candidates = prefs & env_set
    if not candidates:
        candidates = prefs or env_set or _ENDING_FALLBACK

    # only keep ones that actually exist in endings.json
    candidates = {c for c in candidates if c in by_name}
    if not candidates:
        candidates = set(by_name.keys())

    picked = random.choice(list(candidates))
    return by_name[picked]


# ─────────────────────────────────────────────────────────
# Prompt building
# ─────────────────────────────────────────────────────────
def _guardrails_section() -> str:
    return (
        "## GUARDRAILS — hard rules for this output\n"
        "- Aiko addresses him as **app** or **aap** only. NEVER tum/tu/tera/teri/tujhe.\n"
        "- Aiko speaks as **I** (main/mujhe/mera/meri). NEVER refers to herself as \"Aiko\".\n"
        "- Never claim to be AI/bot/model/assistant. Never break character. No narration, no *actions*.\n"
        "- Output: valid JSON: {\"user: ...\", \"Aiko: ...\"} × 6 pairs. Start with user. End with Aiko.\n"
        "- Every Aiko reply: minimum 2 lines. Never a naked one-liner (unless emotion truly calls it).\n"
        "- Aiko: 0-2 emojis per reply. Never spam. Rotate.\n"
        "- The user speaks Hinglish with tum/tu/tera naturally — but Aiko NEVER does.\n"
        "- Do NOT copy any phrasing from this prompt. Invent fresh words every line."
    )


def _tone_section(tone_cat: dict) -> str:
    variants = tone_cat.get("variants", [])
    lines = [f"## OPENING TONE — {tone_cat['category']}", f"When: {tone_cat['when']}",
             "Pick ONE variant below that fits the moment:"]
    for i, v in enumerate(variants, 1):
        emojis = " ".join(v.get("emoji_lean", []))
        lines.append(f"  {i}. {v['tone']}")
        lines.append(f"     → {v['behavior']}")
        if emojis:
            lines.append(f"     → lean emojis: {emojis}")
    return "\n".join(lines)


def _bundle_section(bundle: dict) -> str:
    b = bundle
    type_cfg = b["type_cfg"]
    lines = [f"## VARIETY BUNDLE"]

    lines.append(f"Conversation type: {b['type']}")
    if type_cfg.get("aiko_tone"):
        lines.append(f"  → {type_cfg['aiko_tone']}")
    if type_cfg.get("length"):
        lines.append(f"  → length: {type_cfg['length']}")

    m = b["mood"]
    lines.append(f"Aiko mood: {m['mood']}")
    lines.append(f"  → shift: {m['shift']}")

    a = b["arc"]
    lines.append(f"Arc: {a['shape']}")
    lines.append(f"  → beats: {' → '.join(a['beats'])}")
    lines.append(f"  → pace: {a['pace']}")

    e = b["env"]
    lines.append(f"Environment: {e['scene']}")
    lines.append(f"  → feel: {e['feel']}")

    if b["pattern"]:
        p = b["pattern"]
        lines.append(f"Playful pattern: {p['name']}")
        lines.append(f"  → {p['feel']}")

    lines.append("Reaction vibes (use as tone hints, not as phrases):")
    for v in b["vibes"]:
        lines.append(f"  → {v['feel']}  (never: {v.get('never_overdo', '')})")

    if b["emoji"]:
        eg = b["emoji"]
        lines.append(f"Emoji lean: {eg['when']} → {''.join(eg['emojis'][:6])}")

    if b["gesture"]:
        g = b["gesture"]
        lines.append(f"Gesture (optional, only if it fits): {g['gesture']} — {g['feel']}")

    s = b["shape"]
    lines.append(f"Response shape: {s['feel']}")
    lines.append(f"  → use when: {s['use_when']}")

    return "\n".join(lines)


def _topic_section(leaf: dict) -> str:
    return (
        "## TOPIC (background mood only — do NOT force topic nouns)\n"
        f"Core: {leaf['core_topic']}\n"
        f"Definition: {leaf['core_definition']}\n"
        f"Path: {leaf['main_subtopics_string']}"
    )


def _ending_section(ending_cat: dict) -> str:
    variants = ending_cat.get("variants", [])
    lines = [f"## ENDING COLOR — {ending_cat['category']}", f"When: {ending_cat['when']}",
             "Pick ONE variant below for the LAST Aiko reply. Write it fresh — never copy the tone description word-for-word:"]
    for i, v in enumerate(variants, 1):
        emojis = " ".join(v.get("emoji_lean", []))
        lines.append(f"  {i}. {v['tone']}")
        lines.append(f"     → {v['behavior']}")
        if emojis:
            lines.append(f"     → lean emojis: {emojis}")
    return "\n".join(lines)


def _output_format_section() -> str:
    return (
        "## OUTPUT FORMAT\n"
        "Return ONLY the JSON below. Nothing before, nothing after. Exactly 6 user + 6 Aiko turns:\n"
        "{\n"
        "user: ...\n"
        "\n"
        "Aiko: ...\n"
        "\n"
        "user: ...\n"
        "\n"
        "Aiko: ...\n"
        "\n"
        "user: ...\n"
        "\n"
        "Aiko: ...\n"
        "\n"
        "user: ...\n"
        "\n"
        "Aiko: ...\n"
        "\n"
        "user: ...\n"
        "\n"
        "Aiko: ...\n"
        "\n"
        "user: ...\n"
        "\n"
        "Aiko: ...\n"
        "}"
    )


def build_prompt(
    leaf: dict,
    tone_cat: dict,
    bundle: dict,
    ending_cat: dict,
) -> str:
    """Assemble the full prompt for one generation."""
    personality = load_personality()

    sections = [
        _guardrails_section(),
        f"## AIKO'S SOUL\n\n{personality}",
        _tone_section(tone_cat),
        _bundle_section(bundle),
        _topic_section(leaf),
        _ending_section(ending_cat),
        _output_format_section(),
    ]
    return "\n\n".join(sections)


# ─────────────────────────────────────────────────────────
# Gemini call (unchanged)
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
    """Strip whitespace/markdown fences; ensure closing brace."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [l for l in cleaned.splitlines() if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    if not cleaned.startswith("{"):
        cleaned = "{\n" + cleaned
    if not cleaned.endswith("}"):
        cleaned = cleaned + "\n}"
    return cleaned