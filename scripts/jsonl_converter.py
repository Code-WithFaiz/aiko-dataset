#!/usr/bin/env python3
"""CLI wrapper -- real logic lives in src/converter.py.
Usage: python scripts/jsonl_converter.py input.jsonl output.jsonl
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.converter import convert_file  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: python scripts/jsonl_converter.py input.jsonl output.jsonl")
    good, skipped = convert_file(sys.argv[1], sys.argv[2])
    print("converted=%d skipped=%d" % (good, skipped))
