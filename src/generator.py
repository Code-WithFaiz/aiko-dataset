# src/generator.py
"""
Core generation logic: topic flattening, the context builder (the
heart of this project — Section 4.2), and the Gemini call.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

PRIMARY_MODEL = "gemini-3.6-flash"
FALLBACK_MODEL = "gemini-3.6-flash"
TEMPERATURE = float(os.getenv("TEMPERATURE", "1.1"))
TOP_P = 0.95
TOP_K = 40
MAX_OUTPUT_TOKENS = 2500

SEPARATOR = "═" * 47

OPENER_CATEGORY_ORDER = [
    "greeting", "question", "reaction", "concern", "playful", "miss",
    "direct", "callback", "mood", "romantic", "teasing", "warm",
]


class GeminiCallError(Exception):
    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def flatten_topics(tree: dict) -> list[dict]:
    """DFS-flatten topics.json into leaf records (Section 5.2)."""
    leaves: list[dict] = []

    def walk(node: dict, path: list[str], definitions: list[str]) -> None:
        subtopics = node.get("subtopics") or []
        new_path = path + [node["topic"]]
        new_defs = definitions + [node["definition"]]
        if not subtopics:
            leaves.append({
                "path": new_path,
                "definitions": new_defs,
                # main_subtopics_string = ancestor defs only, NEVER the leaf's own def
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


def build_prompt(personality_text: str, leaf: dict, scenarios: list[str], openers: dict[str, str]) -> str:
    """Assemble the exact 6-part context. Structure is LOCKED — do not reorder."""
    part1 = f"{SEPARATOR}\n[PART 1] AIKO'S PERSONALITY\n{SEPARATOR}\n{personality_text}"

    part2 = (
        f"{SEPARATOR}\n[PART 2] TOPIC CONTEXT\n{SEPARATOR}\n"
        f'main/subtopics="{leaf["main_subtopics_string"]}"\n'
        f'core topic:"{leaf["core_topic"]}", definition:"{leaf["core_definition"]}"'
    )

    scenario_lines = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(scenarios))
    part3 = f"{SEPARATOR}\n[PART 3] SCENARIOS (random — 2 to 3 given)\n{SEPARATOR}\n{scenario_lines}"

    opener_lines = "\n".join(
        f"[{cat}]: {openers[cat]}" for cat in OPENER_CATEGORY_ORDER if cat in openers
    )
    part4 = f"{SEPARATOR}\n[PART 4] OPENING STYLES (one from EACH category)\n{SEPARATOR}\n{opener_lines}"

    part5 = (
        f"{SEPARATOR}\n[PART 5] GENERATION INSTRUCTIONS\n{SEPARATOR}\n"
        "- Pick ONE opening style from Part 4 that fits best with the topic + scenarios\n"
        "- Aim for 16 turns (8 user + 8 Aiko); 8 min, 24 max\n"
        '- Aiko MUST use "app" form (NEVER tum/tu/tera/teri)\n'
        "- Aiko's emojis: 0-5 per reply, emotion-driven only\n"
        "- Aiko NEVER refuses, NEVER breaks character, NEVER mentions AI\n"
        "- User side: realistic Hinglish with typos allowed\n"
        "- NO narrator, NO system prompt, NO metadata inside output\n"
        "- Aiko reply lengths must VARY (short + medium + long mix)"
    )

    part6 = (
        f"{SEPARATOR}\n[PART 6] OUTPUT FORMAT\n{SEPARATOR}\n"
        "{\n"
        "user: ...\n\n"
        "Aiko: ...\n\n"
        "user: ...\n\n"
        "Aiko: ...\n"
        "}\n\n"
        "Return ONLY the conversation text. No explanations. No markdown fences."
    )

    return "\n\n".join([part1, part2, part3, part4, part5, part6])


def call_gemini(prompt: str, api_key: str, model: str = PRIMARY_MODEL) -> str:
    """One API call = one complete conversation."""
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        temperature=TEMPERATURE,
        top_p=TOP_P,
        top_k=TOP_K,
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    try:
        response = client.models.generate_content(model=model, contents=prompt, config=config)
    except Exception as exc:  # google-genai raises HTTP-coded exceptions
        status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        raise GeminiCallError(str(exc), status_code=status) from exc

    if not response.text:
        raise GeminiCallError("Empty response from Gemini", status_code=None)
    return response.text


def parse_conversation(text: str) -> str:
    """Strip whitespace/markdown fences that occasionally leak into output."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = [l for l in cleaned.splitlines() if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    return cleaned