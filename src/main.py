# src/main.py
"""
Orchestrator: one invocation = one GitHub Actions run (every 5 min).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src import db, dedup, notifier, storage, validator
from src.generator import (
    FALLBACK_MODEL,
    PRIMARY_MODEL,
    GeminiCallError,
    build_prompt,
    call_gemini,
    flatten_topics,
    parse_conversation,
)
from src.key_rotator import KeyRotator

LOG_DIR = Path("data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
logger = logging.getLogger(__name__)

CONFIG_DIR = Path("config")
LOOP_ITERATIONS = int(os.getenv("BATCH_SIZE", "100"))
BATCH_UPLOAD_THRESHOLD = 30
DAILY_TARGET = int(os.getenv("DAILY_TARGET", "6700"))
OPENER_CATEGORIES = [
    "greeting", "question", "reaction", "concern", "playful", "miss",
    "direct", "callback", "mood", "romantic", "teasing", "warm",
]
TIME_OF_DAY_KEYWORDS = ["subah", "dopahar", "shaam", "raat", "morning", "night", "monday", "weekend", "sunday"]


def load_config() -> tuple[str, dict, list[str], list[dict]]:
    personality_text = (CONFIG_DIR / "personality.md").read_text(encoding="utf-8")
    topics_tree = json.loads((CONFIG_DIR / "topics.json").read_text(encoding="utf-8"))
    scenarios_data = json.loads((CONFIG_DIR / "scenarios.json").read_text(encoding="utf-8"))
    openers_data = json.loads((CONFIG_DIR / "openers.json").read_text(encoding="utf-8"))
    return personality_text, topics_tree, scenarios_data["scenarios"], openers_data["openers"]


def get_or_build_leaf_order(topics_tree: dict) -> tuple[dict[str, dict], list[str]]:
    leaves = flatten_topics(topics_tree)
    order = db.get_leaf_order()
    if order is None:
        random.seed(42)
        order = [l["leaf_path"] for l in leaves]
        random.shuffle(order)
        db.set_leaf_order(order)
        db.init_leaf_quotas(leaves)
    leaf_by_path = {l["leaf_path"]: l for l in leaves}
    return leaf_by_path, order


def pick_scenarios(all_scenarios: list[str], leaf: dict, recent: list[str]) -> list[str]:
    leaf_text = " ".join(leaf["path"]).lower()
    topic_implies_time = any(kw in leaf_text for kw in TIME_OF_DAY_KEYWORDS)

    candidates = []
    for s in all_scenarios:
        if s in recent:
            continue
        if topic_implies_time and any(kw in s.lower() for kw in TIME_OF_DAY_KEYWORDS):
            continue
        candidates.append(s)
    if len(candidates) < 2:
        candidates = [s for s in all_scenarios if s not in recent] or all_scenarios

    k = min(random.choice([2, 3]), len(candidates))
    return random.sample(candidates, k)


def pick_openers(openers: list[dict]) -> dict[str, str]:
    by_category: dict[str, list[str]] = {}
    for o in openers:
        by_category.setdefault(o["category"], []).append(o["text"])

    chosen: dict[str, str] = {}
    used: list[tuple[str, str]] = []
    for category in OPENER_CATEGORIES:
        pool = by_category.get(category, [])
        if not pool:
            continue
        recent = db.get_recent_openers(category)
        candidates = [t for t in pool if t not in recent] or pool
        pick = random.choice(candidates)
        chosen[category] = pick
        used.append((category, pick))

    for category, text in used:
        db.add_used_opener(category, text)

    return chosen


def generate_one(
    key_rotator: KeyRotator,
    personality_text: str,
    leaf: dict,
    all_scenarios: list[str],
    all_openers: list[dict],
) -> tuple[str | None, str]:
    """Returns (conversation_text_or_None, reason)."""
    recent_scenarios = db.get_recent_scenarios()
    recent_signatures = db.get_recent_signatures()
    recent_openings = db.get_recent_openings()

    for attempt in range(1, 4):  # Section 4.6: retry up to 3 times on reject
        scenarios = pick_scenarios(all_scenarios, leaf, recent_scenarios)
        openers = pick_openers(all_openers)
        prompt = build_prompt(personality_text, leaf, scenarios, openers)

        text = _call_with_key_rotation(key_rotator, prompt)
        if text is None:
            return None, "all_keys_exhausted"

        conversation = parse_conversation(text)
        ok, reason = validator.validate(conversation)
        if not ok:
            logger.warning("Validation reject (attempt %d): %s", attempt, reason)
            if attempt == 3:
                return None, f"rejected_validation:{reason}"
            continue

        is_dup, dup_reason = dedup.is_duplicate(conversation, db.hash_exists, recent_signatures, recent_openings)
        if is_dup:
            logger.warning("Dedup reject (attempt %d): %s", attempt, dup_reason)
            if attempt == 3:
                # Section 4.6: if still failing after 3 tries, accept anyway (rare)
                db.add_used_scenarios(scenarios)
                return conversation, "accepted_after_dedup_retries_exhausted"
            continue

        db.add_used_scenarios(scenarios)
        return conversation, "ok"

    return None, "exhausted_retries"


def _call_with_key_rotation(key_rotator: KeyRotator, prompt: str) -> str | None:
    model = PRIMARY_MODEL
    server_error_retries = 0

    while True:
        key = key_rotator.wait_for_available_key()
        if key is None:
            logger.critical("All Gemini keys dead")
            notifier.send_critical_alert("All keys dead", "Every Gemini API key is dead. Manual intervention needed.")
            return None
        try:
            text = call_gemini(prompt, key, model=model)
            key_rotator.mark_success(key)
            db.increment_key_requests(_key_id(key))
            return text
        except GeminiCallError as exc:
            status = exc.status_code
            if status == 429:
                key_rotator.mark_rate_limited(key)
                continue
            if status in (401, 403):
                key_rotator.mark_dead(key)
                db.update_key_stats(_key_id(key), dead=True)
                logger.warning("Key ...%s unauthorized/invalid, marking dead", key[-4:])
                continue
            if status in (500, 503):
                server_error_retries += 1
                if server_error_retries <= 2:
                    time.sleep(5)
                    continue
                if model == PRIMARY_MODEL:
                    model = FALLBACK_MODEL
                    server_error_retries = 0
                    continue
                return None
            logger.error("Gemini call failed: %s", exc)
            return None


def _key_id(key: str) -> str:
    """Stable key ID for MongoDB tracking (Python's builtin hash() is not
    stable across processes, so we use a deterministic digest instead)."""
    return f"key_{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def run() -> int:
    logger.info("=== Aiko generator run starting ===")
    run_id = db.acquire_run_lock()
    if run_id is None:
        logger.warning("Could not acquire run lock (another run in progress); exiting")
        return 0
    try:
        return _run_locked()
    finally:
        db.release_run_lock(run_id)


def _run_locked() -> int:
    try:
        personality_text, topics_tree, all_scenarios, all_openers = load_config()
    except (OSError, json.JSONDecodeError) as exc:
        logger.critical("Failed to load config files: %s", exc)
        return 1

    try:
        key_rotator = KeyRotator()
    except RuntimeError as exc:
        logger.critical("Key rotator init failed: %s", exc)
        return 1

    try:
        leaf_by_path, order = get_or_build_leaf_order(topics_tree)
    except Exception as exc:
        logger.critical("DB unreachable or leaf setup failed: %s", exc)
        return 1

    batch: list[str] = []
    generated = 0
    rejected = 0

    for i in range(LOOP_ITERATIONS):
        leaf_path = db.get_next_leaf_path(order)
        if leaf_path is None:
            logger.info("All leaf quotas filled — dataset complete!")
            break
        leaf = leaf_by_path[leaf_path]

        try:
            conversation, reason = generate_one(key_rotator, personality_text, leaf, all_scenarios, all_openers)
        except Exception as exc:
            # Section 11: never let one bad conversation crash a batch
            logger.error("Unexpected error generating conversation %d: %s", i, exc)
            rejected += 1
            continue

        if conversation is None:
            rejected += 1
            if reason == "all_keys_exhausted":
                break
            continue

        db.add_hash(dedup.hash_text(conversation))
        db.add_signature(dedup.signature(conversation))
        db.add_opening(dedup.opening_text(conversation))

        batch.append(conversation)
        db.increment_leaf_generated(leaf_path, 1)
        generated += 1

        if len(batch) >= BATCH_UPLOAD_THRESHOLD:
            _flush_batch(batch)
            batch = []

    if batch:
        _flush_batch(batch)

    db.record_generated(generated, rejected)
    logger.info("Run complete: generated=%d rejected=%d", generated, rejected)

    _maybe_send_daily_report()
    return 0


def _flush_batch(batch: list[str]) -> None:
    ok, url = storage.upload_batch(batch)
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    if ok:
        db.log_batch(batch_id, url, len(batch))
        logger.info("Uploaded batch of %d conversations to %s", len(batch), url)
    else:
        logger.error("Batch upload failed; %d conversations kept locally only", len(batch))


def _maybe_send_daily_report() -> None:
    try:
        progress = db.get_progress()
        today = datetime.now(timezone.utc)
        yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_stats = progress.get("daily_stats", {})
        y_stats = daily_stats.get(yesterday, {"generated": 0, "rejected": 0})
        total_generated = progress.get("total_generated", 0)
        started_at = progress.get("started_at", today)
        days_elapsed = max(1, (today - started_at).days) if isinstance(started_at, datetime) else 1
        total_leaves = len(db.get_leaf_order() or [])
        leaves_completed = db.leaves_completed_count()

        pct = (total_generated / 600_000) * 100
        remaining = 600_000 - total_generated
        daily_rate = max(1, total_generated // max(1, days_elapsed))
        eta_days = remaining // daily_rate if daily_rate else 0
        estimated_finish = (today + timedelta(days=eta_days)).strftime("%Y-%m-%d")

        stats = {
            "yesterday_generated": y_stats.get("generated", 0),
            "yesterday_rejected": y_stats.get("rejected", 0),
            "total_generated": total_generated,
            "pct_complete": pct,
            "days_elapsed": days_elapsed,
            "estimated_finish": estimated_finish,
            "validation_reject_pct": 0.0,
            "dup_reject_pct": 0.0,
            "top_reject_reasons": "n/a",
            "leaves_completed": leaves_completed,
            "total_leaves": total_leaves,
            "current_leaf": progress.get("current_leaf_index", "n/a"),
            "mongo_size_mb": 0.0,
            "error_count": 0,
            "top_errors": "none",
            "next_target": max(0, DAILY_TARGET - y_stats.get("generated", 0)),
        }
        notifier.send_daily_report(stats)
    except Exception as exc:
        logger.error("Failed to build/send daily report: %s", exc)


if __name__ == "__main__":
    sys.exit(run())