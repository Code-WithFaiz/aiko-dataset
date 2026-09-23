# src/main.py
"""
Orchestrator: one invocation = one GitHub Actions run.
Graceful shutdown at 52 min (before GitHub's 55 min hard timeout).

Aligned with new generator.py:
  - Uses pick_tone_category / pick_variety_bundle / pick_ending_category
  - No scenarios / openers / endings-inline
  - Personality + variety configs loaded lazily by generator (cached)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import signal
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
    load_category_map,
    parse_conversation,
    pick_ending_category,
    pick_tone_category,
    pick_variety_bundle,
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
BATCH_UPLOAD_THRESHOLD = 15
DAILY_TARGET = int(os.getenv("DAILY_TARGET", "6700"))
RUN_DEADLINE_SECONDS = 52 * 60
TONE_HISTORY_SIZE = 25
RATE_LIMIT_COOLDOWN_SECONDS = 90

# --- Signal handling for graceful shutdown on GitHub cancel ---
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Set flag when GitHub sends SIGINT/SIGTERM (on cancel/timeout).
    The main loop checks this flag and exits gracefully, so the run lock
    gets released and no orphaned lock is left behind in MongoDB."""
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning("Shutdown signal %s received; will stop after current iteration", signum)


# ─────────────────────────────────────────────────────────
# Config loading — topics only (other configs live in generator)
# ─────────────────────────────────────────────────────────
def load_topics() -> dict:
    return json.loads((CONFIG_DIR / "topics.json").read_text(encoding="utf-8"))


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


# ─────────────────────────────────────────────────────────
# Conversation generation
# ─────────────────────────────────────────────────────────
def _track_tone(recent: list[str], cat_name: str) -> None:
    recent.append(cat_name)
    if len(recent) > TONE_HISTORY_SIZE:
        del recent[0 : len(recent) - TONE_HISTORY_SIZE]


def generate_one(
    key_rotator: KeyRotator,
    leaf: dict,
    recent_tone_cats: list[str],
    deadline: float | None = None,
) -> tuple[str | None, str]:
    """Returns (conversation_text_or_None, reason)."""
    recent_signatures = db.get_recent_signatures()
    recent_openings = db.get_recent_openings()
    category_map = load_category_map()

    for attempt in range(1, 4):
        if deadline is not None and time.monotonic() >= deadline:
            return None, "deadline_reached"

        # 1. Pick tone category (avoids recent ones)
        tone_cat = pick_tone_category(recent_tone_cats)
        cat_name = tone_cat["category"]

        # 2. Map to conversation type
        conv_type = category_map.get(cat_name, "playful-banter")

        # 3. Build variety bundle
        bundle = pick_variety_bundle(conv_type)

        # 4. Pick ending category
        ending_cat = pick_ending_category(
            conv_type, bundle["env"].get("ending_lean", [])
        )

        # 5. Build prompt
        prompt = build_prompt(leaf, tone_cat, bundle, ending_cat)

        # 6. Call Gemini
        text = _call_with_key_rotation(key_rotator, prompt, deadline=deadline)
        if text is None:
            return None, "all_keys_exhausted"

        # 7. Validate
        conversation = parse_conversation(text)
        ok, reason = validator.validate(conversation)
        if not ok:
            logger.warning("Validation reject (attempt %d): %s", attempt, reason)
            if attempt == 3:
                return None, f"rejected_validation:{reason}"
            continue

        # 8. Dedup
        is_dup, dup_reason = dedup.is_duplicate(
            conversation, db.hash_exists, recent_signatures, recent_openings
        )
        if is_dup:
            logger.warning("Dedup reject (attempt %d): %s", attempt, dup_reason)
            if attempt == 3:
                _track_tone(recent_tone_cats, cat_name)
                return conversation, "accepted_after_dedup_retries_exhausted"
            continue

        # 9. Success
        _track_tone(recent_tone_cats, cat_name)
        return conversation, "ok"

    return None, "exhausted_retries"


# ─────────────────────────────────────────────────────────
# Gemini call with key rotation
# ─────────────────────────────────────────────────────────
def _call_with_key_rotation(
    key_rotator: KeyRotator, prompt: str, deadline: float | None = None
) -> str | None:
    model = PRIMARY_MODEL
    server_error_retries = 0

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if _shutdown_requested:
            return None

        key = key_rotator.wait_for_available_key()
        if key is None:
            logger.critical("All Gemini keys dead")
            notifier.send_critical_alert(
                "All keys dead",
                "Every Gemini API key is dead. Manual intervention needed.",
            )
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
                # Persist cooldown to DB so daily report can show it
                cooldown_until = (
                    datetime.now(timezone.utc) + timedelta(seconds=RATE_LIMIT_COOLDOWN_SECONDS)
                ).replace(tzinfo=None)
                db.update_key_stats(_key_id(key), cooldown_until=cooldown_until)
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
    """Stable key ID for MongoDB tracking."""
    return f"key_{hashlib.sha256(key.encode()).hexdigest()[:12]}"


# ─────────────────────────────────────────────────────────
# Run orchestration
# ─────────────────────────────────────────────────────────
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
        logger.info("Run lock released")


def _run_locked() -> int:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    try:
        topics_tree = load_topics()
    except (OSError, json.JSONDecodeError) as exc:
        logger.critical("Failed to load topics.json: %s", exc)
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
    recent_tone_cats: list[str] = []
    run_start = time.monotonic()
    deadline = run_start + RUN_DEADLINE_SECONDS
    graceful_shutdown = False

    for i in range(LOOP_ITERATIONS):
        if _shutdown_requested:
            logger.info(
                "Graceful shutdown requested via signal. Flushing %d batched conversations.",
                len(batch),
            )
            graceful_shutdown = True
            break

        elapsed = time.monotonic() - run_start
        if elapsed >= RUN_DEADLINE_SECONDS:
            logger.info(
                "Graceful shutdown at %.1fs (deadline=%ds). Flushing %d batched conversations.",
                elapsed, RUN_DEADLINE_SECONDS, len(batch),
            )
            graceful_shutdown = True
            break

        leaf_path = db.get_next_leaf_path(order)
        if leaf_path is None:
            logger.info("All leaf quotas filled — dataset complete!")
            break
        leaf = leaf_by_path[leaf_path]

        try:
            conversation, reason = generate_one(
                key_rotator, leaf, recent_tone_cats, deadline=deadline,
            )
        except Exception as exc:
            logger.error("Unexpected error generating conversation %d: %s", i, exc)
            rejected += 1
            continue

        if conversation is None:
            rejected += 1
            if reason == "all_keys_exhausted":
                break
            if reason == "deadline_reached":
                logger.info(
                    "Graceful shutdown inside generate_one (deadline). Flushing %d batched conversations.",
                    len(batch),
                )
                graceful_shutdown = True
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
    logger.info(
        "Run complete: generated=%d rejected=%d%s",
        generated, rejected,
        " [graceful_shutdown]" if graceful_shutdown else "",
    )

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
        notifier.send_daily_report()
    except Exception as exc:
        logger.error("Failed to send daily report: %s", exc)


if __name__ == "__main__":
    sys.exit(run())