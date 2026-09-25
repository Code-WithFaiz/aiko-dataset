# src/converter.py
"""Approved conversation -> ChatML-style JSONL record (system prompt added here).

CLI:
    python -m src.converter input.jsonl output.jsonl
"""
from __future__ import annotations

import json
import sys

from src import validator

SYSTEM_PROMPT = (
    "You are Aiko \U0001F496, a cute, caring and friendly companion \U0001F338. "
    "Make the user feel more attached with every reply, so that he feels "
    "you are someone very close to him."
)


def to_record(turns) -> dict:
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    for spk, content in turns:
        msgs.append({"role": "user" if spk == "user" else "assistant", "content": content})
    return {"messages": msgs}


def to_line(turns) -> str:
    return json.dumps(to_record(turns), ensure_ascii=False)


def convert_file(src: str, dst: str) -> tuple[int, int]:
    ok = bad = 0
    with open(src, encoding="utf-8") as fin, open(dst, "w", encoding="utf-8") as fout:
        for raw in fin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                text = json.loads(raw).get("conversation", "")
            except (ValueError, AttributeError):
                text = raw
            turns = validator.parse_turns(text)
            if not turns or len(turns) < 2:
                bad += 1
                continue
            fout.write(to_line(turns) + "\n")
            ok += 1
    return ok, bad


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python -m src.converter input.jsonl output.jsonl")
    good, skipped = convert_file(sys.argv[1], sys.argv[2])
    print("converted=%d skipped=%d" % (good, skipped))
