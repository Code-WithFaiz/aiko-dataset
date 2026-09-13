# scripts/test_batch.py
"""
Local test run: generates 50 conversations using the same pipeline as
main.py, writes to data/batches/test_batch.jsonl, does NOT upload to
GitHub. Note: it still touches shared MongoDB dedup/opener buffers —
run this against a test MONGO_DB_NAME if you want full isolation.
"""
from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from src.key_rotator import KeyRotator
from src.main import generate_one, get_or_build_leaf_order, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TEST_COUNT = 50
OUTPUT_PATH = Path("data/batches/test_batch.jsonl")


def main() -> None:
    personality_text, topics_tree, all_scenarios, all_openers = load_config()
    key_rotator = KeyRotator()
    leaf_by_path, order = get_or_build_leaf_order(topics_tree)

    results = []
    generated = 0
    rejected = 0

    while generated < TEST_COUNT:
        leaf_path = random.choice(order)
        leaf = leaf_by_path[leaf_path]
        conversation, reason = generate_one(key_rotator, personality_text, leaf, all_scenarios, all_openers)
        if conversation is None:
            rejected += 1
            logger.warning("Rejected: %s", reason)
            if reason == "all_keys_exhausted":
                break
            continue
        results.append({"leaf": leaf_path, "conversation": conversation, "note": reason})
        generated += 1
        logger.info("Generated %d/%d (leaf: %s)", generated, TEST_COUNT, leaf["core_topic"])

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n=== TEST BATCH SUMMARY ===\nGenerated: {generated}\nRejected:  {rejected}\nSaved to:  {OUTPUT_PATH}\n")
    if results:
        print("=== SAMPLE (first conversation) ===\n")
        print(results[0]["conversation"])


if __name__ == "__main__":
    main()