# scripts/jsonl_converter.py
"""
Converts raw batch files (data/batches/*.jsonl, format:
{"conversation": "<raw text>"}) into fine-tuning-ready chat-format
JSONL (Section 9.10).
"""
from __future__ import annotations

import glob
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    'You are "Aiko"\n'
    "A sweet, caring, cute, helpful, lovely, companion \U0001F496.\n"
    "Your main objective is to give a safe space of you to user!"
)

INPUT_GLOB = "data/batches/*.jsonl"
OUTPUT_PATH = Path("data/final_dataset.jsonl")
TURN_PATTERN = re.compile(r"(user|Aiko):\s*(.*?)(?=\n\n(?:user|Aiko):|\Z)", re.DOTALL)


def parse_raw_conversation(raw_text: str) -> list[dict] | None:
    body = raw_text.strip()
    if body.startswith("{"):
        body = body[1:]
    if body.endswith("}"):
        body = body[:-1]
    body = body.strip()

    matches = TURN_PATTERN.findall(body)
    if not matches:
        return None

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for speaker, content in matches:
        content = content.strip()
        if not content:
            continue
        role = "user" if speaker.lower() == "user" else "assistant"
        messages.append({"role": role, "content": content})

    if len(messages) < 3:  # system + at least 1 user + 1 assistant
        return None
    return messages


def convert_all() -> tuple[int, int]:
    files = sorted(glob.glob(INPUT_GLOB))
    if not files:
        logger.warning("No batch files found matching %s", INPUT_GLOB)
        return 0, 0

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    converted = 0
    skipped = 0

    with OUTPUT_PATH.open("w", encoding="utf-8") as out_f:
        for filepath in files:
            with open(filepath, encoding="utf-8") as in_f:
                for line_num, line in enumerate(in_f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        raw_text = record.get("conversation") or record.get("raw") or ""
                    except json.JSONDecodeError:
                        logger.warning("%s:%d - invalid JSON, skipping", filepath, line_num)
                        skipped += 1
                        continue

                    messages = parse_raw_conversation(raw_text)
                    if messages is None:
                        logger.warning("%s:%d - could not parse conversation, skipping", filepath, line_num)
                        skipped += 1
                        continue

                    out_f.write(json.dumps({"messages": messages}, ensure_ascii=False) + "\n")
                    converted += 1

    return converted, skipped


def main() -> int:
    converted, skipped = convert_all()
    print(f"Converted: {converted}\nSkipped:   {skipped}\nOutput:    {OUTPUT_PATH}")
    return 0 if converted > 0 else 1


if __name__ == "__main__":
    sys.exit(main())