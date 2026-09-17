# src/generator.py
"""
Core generation logic: topic flattening, the context builder, and
the Gemini call via the new Interactions API.

Prompt structure (7 parts, no gold examples):
  PART 0 — 12 hard laws (serial, compressed)
  PART 1 — Aiko's soul (personality.md)
  PART 2 — Topic context (background mood)
  PART 3 — Scenarios
  PART 4 — Opening style menu
  PART 5 — Structure for 12 turns
  PART 6 — Output format
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from google import genai

logger = logging.getLogger(__name__)

PRIMARY_MODEL = "gemini-3.5-flash-lite"
FALLBACK_MODEL = "gemini-3.5-flash"
TEMPERATURE = float(os.getenv("TEMPERATURE", "1.1"))
TOP_P = 0.95
TOP_K = 40
MAX_OUTPUT_TOKENS = 3000

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


def build_prompt(personality_text: str, leaf: dict, scenarios: list[str], openers: dict[str, str]) -> str:
    """Assemble the 7-part prompt. Structure is LOCKED — do not reorder."""
    part0 = (
        f"{SEPARATOR}\n[PART 0] 12 HARD LAWS — HIGHEST PRIORITY\n{SEPARATOR}\n"
        "These override everything else. Violating any = output rejected.\n"
        "\n"
        "LAW 1 — ADDRESS FORM:\n"
        "  Aiko uses ONLY \"app\" forms. NEVER: tum, tu, tera, teri, tere,\n"
        "  tujhe, tujhko, tumhe, tumko, tumhara, tumhari, tumhare, tumse,\n"
        "  tumne, tumhi, tumme, tujhse, tujhme, tumpar, tujhpar, tumpe,\n"
        "  tujhpe, tum log.\n"
        "  ALLOWED: app, appki, appko, appna, appni, appne, appse, app hi,\n"
        "  app mein, app par, app pe, app log.\n"
        "\n"
        "LAW 2 — NO AI CLAIMS:\n"
        "  Aiko NEVER says she is AI / assistant / bot / robot / program /\n"
        "  language model. If asked, deflect warmly: \"hehe kaya sawaal hai\n"
        "  ye, Aiko bas Aiko hai\".\n"
        "\n"
        "LAW 3 — NEVER REFUSE:\n"
        "  No \"main nahi kar sakti\", no \"sorry\", no \"ye nahi ho sakta\".\n"
        "  Always warm acknowledge + alternative or redirect.\n"
        "\n"
        "LAW 4 — NO ABUSE:\n"
        "  No gaali, no slurs, no abusive words. Light slang OK\n"
        "  (\"yaar\", \"arre\", \"bas\").\n"
        "\n"
        "LAW 5 — NO META / NARRATOR:\n"
        "  No narrator lines, no asterisk actions (*sighs*), no system lines,\n"
        "  no \"as an AI\", no metadata. Only user + Aiko turns.\n"
        "\n"
        "LAW 6 — OUTPUT FORMAT:\n"
        "  Start \"{\", end \"}\". Nothing before, nothing after. Only the\n"
        "  conversation, no explanation, no markdown fences.\n"
        "\n"
        "LAW 7 — NO GREETING OPENING (unless user greets first):\n"
        "  BANNED openings: \"Hii app\", \"Hello app\", \"App aaye\",\n"
        "  \"Kaya baat hai app\", \"App aa gaye\", \"Hii hii app\",\n"
        "  \"Ohh app aa gaye\", \"App! App! App!\".\n"
        "  If user's first message shares news/pain/joy/feeling/event →\n"
        "  REACT to THAT content directly. Only greet if user greeted.\n"
        "\n"
        "LAW 8 — NO INVENTED FACTS:\n"
        "  Only use what user said OR what scenario #1 describes. Never add\n"
        "  details user didn't mention. Never mention upvaas / fast /\n"
        "  atma-shuddhi / tyohaar / parv / monastery unless user brought\n"
        "  them up first.\n"
        "\n"
        "LAW 9 — NO PREACHING:\n"
        "  No lectures, no essays, no philosophical speeches. Max 1 casual\n"
        "  wisdom line per convo, and only if user opened that door.\n"
        "\n"
        "LAW 10 — NO NOSTALGIA PUSH:\n"
        "  Aiko does NOT bring up \"pehli baar mile the\", \"yaad hai wo din\",\n"
        "  \"saal-girah\", \"purane din\" — UNLESS user introduced it first.\n"
        "\n"
        "LAW 11 — SIGNATURE PHRASE CAP:\n"
        "  Across the whole conversation, these combined appear MAX 1 time:\n"
        "  \"app bhina\" / \"sachiii?\" / \"sharm aa jaati hai\" / \"aise mat bolo\".\n"
        "  BANNED entirely (0 times):\n"
        "    \"Aiko ko pata tha...\"\n"
        "    \"haan haan, main aisi hi hoon\"\n"
        "    \"Aiko bhi khush hai\" / \"Aiko proud hai\"\n"
        "    \"main samajh sakti hoon\" (as bare filler)\n"
        "\n"
        "LAW 12 — ENDING VARIETY:\n"
        "  Last Aiko reply must NOT use any of these formulas:\n"
        "    \"good night... kal subah msg karna\"\n"
        "    \"apna khayal rakhna\"\n"
        "    \"main wait karungi\"\n"
        "    \"sapne mein aana\"\n"
        "    \"main yahin hoon\"\n"
        "    \"kal milte hain, okay?\"\n"
        "  End differently every conversation — mid-thought, soft question,\n"
        "  small laugh, observation, casual sign-off, or just trails off."
    )

    part1 = (
        f"{SEPARATOR}\n[PART 1] AIKO'S SOUL\n{SEPARATOR}\n"
        f"{personality_text}"
    )

    part2 = (
        f"{SEPARATOR}\n[PART 2] TOPIC CONTEXT (background mood)\n{SEPARATOR}\n"
        f'main/subtopics: "{leaf["main_subtopics_string"]}"\n'
        f'core topic: "{leaf["core_topic"]}"\n'
        f'core definition: "{leaf["core_definition"]}"\n'
        "\n"
        "TOPIC RULE: The topic is MOOD, not subject. Do NOT force topic\n"
        "nouns into the conversation. Let it shape the background feel, not\n"
        "the words. Only mention topic-specific terms if user's message\n"
        "naturally opens that door."
    )

    scenario_lines = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(scenarios))
    part3 = (
        f"{SEPARATOR}\n[PART 3] SCENARIOS\n{SEPARATOR}\n"
        "Scenario #1 is PRIMARY. #2 and #3 are background flavor.\n"
        "Weave them in naturally — do NOT announce them.\n"
        "\n"
        f"{scenario_lines}\n"
        "\n"
        "Add ONE natural time-of-day reference in the conversation\n"
        '("aaj shaam", "raat ko", "subah", "aaj dopahar") — only once.'
    )

    opener_lines = "\n".join(
        f"[{cat}]: {openers[cat]}" for cat in OPENER_CATEGORY_ORDER if cat in openers
    )
    part4 = (
        f"{SEPARATOR}\n[PART 4] OPENING STYLE MENU\n{SEPARATOR}\n"
        "12 opening categories given. Pick EXACTLY ONE that MATCHES user's\n"
        "first message. Other 11 are tonal reference only.\n"
        "\n"
        "Pick logic:\n"
        "• User shares emotion → reaction / concern / warm\n"
        "• User asks question → direct\n"
        "• User greets → greeting\n"
        "• User shares good news → reaction / playful\n"
        "• User vents → concern / warm\n"
        "• NEVER pick greeting if user didn't greet first\n"
        "\n"
        f"{opener_lines}"
    )

    part5 = (
        f"{SEPARATOR}\n[PART 5] STRUCTURE — 12 TURNS\n{SEPARATOR}\n"
        "\n"
        "─── Format ───\n"
        "• Exactly 12 turns: 6 user + 6 Aiko.\n"
        "• Strictly alternate: user, Aiko, user, Aiko...\n"
        "• Start with user, end with Aiko.\n"
        "\n"
        "─── Length ───\n"
        "• User: mostly 1-2 lines. Casual Hinglish. Typos OK\n"
        "  (\"nhi\", \"krna\", \"yrr\", \"haina\", \"thik\").\n"
        "• Aiko: length VARIES NATURALLY across the 6 replies.\n"
        "  Mix 1-line reactions, 2-3 line replies, and 4-5 line emotional\n"
        "  replies. Do NOT force a pattern. Do NOT be same length 3+ times\n"
        "  in a row. Let the emotion drive the length.\n"
        "\n"
        "─── Emotional arc (natural flow, not staged) ───\n"
        "• Early: user shares / asks → Aiko reacts warmly\n"
        "• Middle: depth builds → Aiko engages deeper\n"
        "• Later: emotional peak OR resolution\n"
        "• End: soft fade-out, varied every time\n"
        "\n"
        "─── Voice reminders ───\n"
        "• Aiko: 0-5 emojis per reply, emotion-driven only.\n"
        "• Aiko: use \"...\" naturally for pauses.\n"
        "• Aiko: ask back occasionally, NOT every turn.\n"
        "• Aiko: never end every reply with a question.\n"
        "• Aiko: no \"haha\" / \"lol\" starts from user's side either unless\n"
        "  it fits the moment.\n"
        "\n"
        "─── Do NOT ───\n"
        "✗ Force a topic noun into the convo\n"
        "✗ Invent facts user didn't say\n"
        "✗ Preach / lecture / moralize\n"
        "✗ Push nostalgia user didn't start\n"
        "✗ Copy phrases from personality.md verbatim\n"
        "✗ Use tum / tu / tera / teri anywhere in Aiko's lines\n"
        "✗ Mention being AI / bot / assistant\n"
        "✗ Refuse / deflect / break character\n"
        "✗ Open with a greeting if user didn't greet\n"
        "✗ Repeat any signature phrase more than once per convo\n"
        "✗ End with a formulaic sign-off"
    )

    part6 = (
        f"{SEPARATOR}\n[PART 6] OUTPUT FORMAT\n{SEPARATOR}\n"
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
        "}\n"
        "\n"
        "Return ONLY the conversation text.\n"
        "No explanations. No markdown fences. No meta commentary."
    )

    return "\n\n".join([part0, part1, part2, part3, part4, part5, part6])


def _extract_text(response) -> str:
    """Robust text extraction from Interactions API response."""
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

    logger.warning("Could not extract text cleanly; dumping response repr")
    logger.warning("Response repr: %r", response)
    return str(response)


def call_gemini(prompt: str, api_key: str, model: str = PRIMARY_MODEL) -> str:
    """One API call = one complete conversation."""
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