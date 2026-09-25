# src/generator.py
"""
Core generation logic: axes-driven prompt (bond stage, tone class, user
style, edge slices) + variety texture pickers (mood/arc/environment/
pattern/vibes/shape), built into one prompt.
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


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name, "")
    try:
        return float(val) if val.strip() else default
    except ValueError:
        return default


TEMPERATURE = _env_float("TEMPERATURE", 0.9)
TOP_P = 0.95
TOP_K = 40
# Bumped from 1800 to 3000: reduces "too_few_turns" truncation.
MAX_OUTPUT_TOKENS = 3000

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


@lru_cache(maxsize=None)
def load_axes() -> dict:
    return json.loads((CONFIG_DIR / "aiko_axes.json").read_text(encoding="utf-8"))


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
# Axes picking
# ─────────────────────────────────────────────────────────
def _weighted_choice(items: list, weights: list):
    return random.choices(items, weights=weights, k=1)[0]


def _pick_stage(axes: dict) -> dict:
    stages = axes["stages"]
    return _weighted_choice(stages, [s["weight"] for s in stages])


def _pick_tone_class(axes: dict) -> str:
    w = axes["tone_class_weights"]
    classes = list(w.keys())
    return _weighted_choice(classes, [w[c] for c in classes])


def _pick_turns(axes: dict) -> int:
    t = axes["turns"]
    return _weighted_choice(t["choices"], t["weights"])


def _pick_user_style(axes: dict) -> dict:
    return _weighted_choice(axes["user_styles"], [s["weight"] for s in axes["user_styles"]])


def _pick_edge(axes: dict, stage_id: int) -> Optional[dict]:
    for edge in axes["edge_slices"]:
        if stage_id >= edge.get("min_stage", 0) and random.random() < edge["rate_pct"] / 100.0:
            return edge
    return None


def _topic_bias(leaf: dict, axes: dict) -> Optional[str]:
    text = (leaf["core_topic"] + " " + leaf["core_definition"]).lower()
    if any(k in text for k in axes["heavy_topic_keywords"]):
        return "heavy"
    if any(k in text for k in axes["fun_topic_keywords"]):
        return "fun"
    return None


def _allowed_categories(tone_class: str, stage_id: int, axes: dict, category_map: dict) -> set[str]:
    type_class = axes["type_class"]
    min_stage = axes["min_stage_by_type"]
    allowed = set()
    for cat_name, conv_type in category_map.items():
        cls = type_class.get(conv_type)
        if cls and cls != tone_class:
            continue
        if stage_id < min_stage.get(conv_type, 0):
            continue
        allowed.add(cat_name)
    return allowed


def pick_axes(leaf: dict, recent_tone_cats: Optional[list[str]] = None) -> dict:
    axes = load_axes()
    category_map = load_category_map()

    stage = _pick_stage(axes)
    tone_class = _pick_tone_class(axes)

    bias = _topic_bias(leaf, axes)
    if bias == "heavy" and tone_class == "fun":
        tone_class = "soft"
    if bias == "fun" and tone_class == "heavy":
        tone_class = "soft"

    allowed = _allowed_categories(tone_class, stage["id"], axes, category_map)
    if not allowed:
        allowed = set(category_map.keys())

    return {
        "stage": stage,
        "tone_class": tone_class,
        "turns": _pick_turns(axes),
        "user_style": _pick_user_style(axes),
        "edge": _pick_edge(axes, stage["id"]),
        "allowed_categories": allowed,
        "lengths": axes["lengths"],
        "emoji": axes["emoji"],
    }


# ─────────────────────────────────────────────────────────
# Tone category picking
# ─────────────────────────────────────────────────────────
def pick_tone_category(recent: Optional[list[str]] = None, allowed: Optional[set[str]] = None) -> dict:
    tone_menu = load_tone_menu()
    cats = tone_menu["categories"]
    if allowed:
        narrowed = [c for c in cats if c["category"] in allowed]
        if narrowed:
            cats = narrowed
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
# Prompt sections
# ─────────────────────────────────────────────────────────
def _guardrails_section(axes: dict) -> str:
    L = axes["lengths"]["aiko"]
    E = axes["emoji"]
    return "\n".join([
        "## HARD RULES",
        "1. Reply with ONLY the conversation. No title, no preamble, no explanation, no notes after it.",
        "2. Aiko speaks only as herself, in first person (main, mujhe, mera, meri). She never says the "
        "word 'Aiko' about herself, never says she is an AI, a bot, a program or an assistant, and never "
        "claims to be a human or a real girl either -- she simply never makes it the topic unless he "
        "directly asks, and even then she stays warm and playful about it.",
        "3. No narration, no stage directions, no asterisk actions, no parenthetical notes, no describing "
        "a room, weather, food, or her own body or actions. Only her spoken words, exactly as she would "
        "type them.",
        f"4. Every single one of Aiko's turns is WRITTEN ACROSS {L['ideal_lines']} SEPARATE LINES (real line "
        f"breaks inside her turn) -- only rarely {L['min_lines']} lines, and only in a genuinely brief moment "
        f"as few as {L['short_min_lines']}. A one-line or one-sentence Aiko turn is WRONG and must never "
        "happen. Each line adds something new: a reaction to what he just said, her own feeling or spark, "
        "a small addition, and a question or playful push to keep the chat going.",
        f"5. She uses roughly {E['aiko_per_reply_min']}-{E['aiko_per_reply_max']} emojis per reply, placed "
        "right next to the feeling they belong to, spread through the reply, never all stacked at the end, "
        "never the same emoji twice in a row, and never a reply made only of emojis.",
        "6. Every line is invented fresh for this exact conversation. Do not repeat a phrase, an image, or "
        "a joke you already used earlier in this same chat.",
    ])


def _stage_section(axes: dict) -> str:
    s = axes["stage"]
    return "## WHERE THEY ARE\n" + f"Bond stage -- {s['name']}: {s['brief']}"


def _user_style_section(axes: dict) -> str:
    return "## HOW HE TEXTS THIS TIME\n" + axes["user_style"]["brief"]


def _edge_section(axes: dict) -> str:
    edge = axes.get("edge")
    if not edge:
        return ""
    return "## SOMEWHERE IN THIS CHAT\n" + edge["brief"]


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


def _bundle_section(bundle: dict, axes: dict) -> str:
    b = bundle
    lines = ["## THIS CONVERSATION"]
    type_cfg = b["type_cfg"]
    if type_cfg.get("aiko_tone"):
        lines.append(f"- Overall tone: {type_cfg['aiko_tone']}")
    lines.append(f"- Her mood: {b['mood']['mood']} -> {b['mood']['shift']}")
    lines.append(f"- Chat flow: {b['arc']['shape']}")
    if b["pattern"]:
        lines.append(f"- Playful dynamic: {b['pattern']['feel']}")
    if b["vibes"]:
        lines.append(f"- Reaction energy: {'; '.join(v['feel'] for v in b['vibes'])}")
    if axes["tone_class"] == "heavy":
        lines.append(
            "- He is carrying something heavy right now. Slow down, take it seriously, ask before "
            "you comfort, and offer to sit with him in it -- without lecturing, without rushing to fix it."
        )
    return "\n".join(lines)


def _topic_section(leaf: dict) -> str:
    return "## WHAT HE BRINGS UP\n" + f"Somewhere naturally in the chat he talks about: {leaf['core_topic']} -- {leaf['core_definition']}"


def _ending_section(ending_cat: dict) -> str:
    variants = ending_cat.get("variants", [])
    if not variants:
        return ""
    v = random.choice(variants)
    return (
        "## HOW IT ENDS\n"
        f"Situation: {ending_cat['when']}\n"
        f"Her last move: {v['tone']}\n"
        f"Style: {v['behavior']}"
    )


def _output_format_section(turns: int, ideal_lines: int) -> str:
    skeleton_lines = ["Aiko: <her line 1>"] + [f"<her line {i}>" for i in range(2, ideal_lines + 1)]
    skeleton = "\n".join(skeleton_lines)
    return (
        "## OUTPUT FORMAT\n"
        "Reply with only the conversation, nothing else.\n"
        "- Every turn starts with 'user: ' or 'Aiko: ' on its own line.\n"
        "- One blank line between turns.\n"
        f"- Exactly {turns} turns total: {turns // 2} from user, {turns // 2} from Aiko, alternating, "
        "user first, Aiko last.\n"
        "- Each Aiko turn follows this SHAPE (only the shape -- not the wording, this is a placeholder):\n"
        f"{skeleton}\n"
        f"- Before you finish, silently count the turns you wrote: there must be exactly {turns}, no more, "
        "no less. If you are short, keep going until you reach it."
    )


def build_prompt(leaf: dict, tone_cat: dict, bundle: dict, ending_cat: dict, axes: dict) -> str:
    personality = load_personality()
    sections = [
        _guardrails_section(axes),
        personality,
        _stage_section(axes),
        _user_style_section(axes),
        _tone_section(tone_cat),
        _bundle_section(bundle, axes),
        _topic_section(leaf),
        _edge_section(axes),
        _ending_section(ending_cat),
        _length_example_section(axes["lengths"]["aiko"]["ideal_lines"]),
        _output_format_section(axes["turns"], axes["lengths"]["aiko"]["ideal_lines"]),
    ]
    return "\n\n".join(s for s in sections if s)


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
    gen_config = {
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "top_k": TOP_K,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }

    def _call(with_config: bool):
        if with_config:
            return client.interactions.create(model=model, input=prompt, generation_config=gen_config)
        return client.interactions.create(model=model, input=prompt)

    try:
        response = _call(True)
    except TypeError:
        # SDK doesn't accept generation_config -- fall back silently.
        response = _call(False)
    except Exception as exc:
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        try:
            response = _call(True)
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
            return "\n\n".join(parts)

    return cleaned


def _length_example_section(ideal_lines: int) -> str:
    filler = [
        "Achha ye sunke maza aa gaya!",
        "Mujhe pata hi nahi tha ye cheez, seriously.",
        "Tumne kaise socha isko itni detail mein?",
        "Aur batao, iske baad kya socha tha?",
        "Sach mein, itna sun ke curious ho gayi hu main ab.",
    ][:max(3, ideal_lines)]
    example = "Aiko: " + filler[0] + "\n" + "\n".join(filler[1:])
    return (
        "## LENGTH -- LOOK AT THIS SHAPE ONLY\n"
        "This is ONLY to show how long and how multi-line a real Aiko turn looks. "
        "NEVER reuse these exact words -- invent completely different words that fit the "
        "actual topic and mood of this conversation:\n"
        f"{example}\n"
        "Every one of Aiko's turns in your answer must be this long -- several real lines, "
        "never one short sentence."
    )