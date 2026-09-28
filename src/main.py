# src/main.py
"""
Orchestrator: one invocation = one GitHub Actions run.

Timing budget (never tweak casually):
  RUN_DEADLINE_SECONDS = 52 min   -> graceful shutdown begins
  GitHub timeout       = 58 min   -> hard kill (safety net)
  Lock TTL             = 75 min   -> orphan recovery
  Cron                 = hourly   -> next dispatch at :02

Circuit breaker:
  - per-slot, in KeyRotator
  - opens when all keys return 5xx since last success
  - closes on any success
  - while open, _call_with_key_rotation returns reason "server_side_down"
    and the slot exits gracefully (flushes batches, releases lock)
  - 5xx retry sleep is a flat 15s, so an outage is detected in ~1 min
    instead of ~5 min with the old 60/90 schedule
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

from src import converter, db, dedup, notifier, storage, validator
from src.generator import (
    FALLBACK_MODEL,
    PRIMARY_MODEL,
    GeminiCallError,
    build_prompt,
    call_gemini,
    flatten_topics,
    load_category_map,
    parse_conversation,
    pick_axes,
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


def _env_int(name, default):
    val = os.getenv(name, "")
    try:
        return int(val) if val.strip() else default
    except ValueError:
        return default


LOOP_ITERATIONS = _env_int("BATCH_SIZE", 100)
BATCH_UPLOAD_THRESHOLD = 15
DAILY_TARGET = _env_int("DAILY_TARGET", 6700)
RUN_DEADLINE_SECONDS = 52 * 60
TONE_HISTORY_SIZE = 25
RATE_LIMIT_COOLDOWN_SECONDS = 90

# Flat sleep between consecutive 5xx retries. Short enough to detect a
# sustained outage in ~1 minute; long enough not to hammer the API.
SERVER_5XX_SLEEP_SECONDS = 15

# How many consecutive 5xx on a single model before we either switch to
# the fallback model (primary) or give up on this prompt (fallback).
MAX_CONSECUTIVE_5XX = 3

# --- Signal handling for graceful shutdown on GitHub cancel ---
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.warning("Shutdown signal %s received; will stop after current iteration", signum)


def _interruptible_sleep(seconds: float, deadline: float | None = None, chunk: float = 5.0) -> bool:
    """Sleep in small chunks so signals and the hard deadline are honoured.

    Returns True if the full duration was slept, False if interrupted
    (shutdown requested or deadline reached). Worst-case delay between
    interrupt and return is ~chunk seconds (default 5s).
    """
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if _shutdown_requested:
            return False
        if deadline is not None and time.monotonic() >= deadline:
            return False
        now = time.monotonic()
        if now >= end:
            return True
        time.sleep(min(chunk, end - now))


# ─────────────────────────────────────────────────────────
# Config loading
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
) -> tuple[dict | None, str, list[str]]:
    """Returns (chatml_record_or_None, reason, flags).

    On success, the returned dict is a ready-to-upload ChatML record.
    Dedup bookkeeping (hash / signature / opening) is done right here.
    """
    recent_signatures = db.get_recent_signatures()
    recent_openings = db.get_recent_openings()
    category_map = load_category_map()

    for attempt in range(1, 4):
        if deadline is not None and time.monotonic() >= deadline:
            return None, "deadline_reached", []

        # 1. Axes
        axes = pick_axes(leaf, recent_tone_cats)

        # 2. Tone category
        tone_cat = pick_tone_category(recent_tone_cats, allowed=axes["allowed_categories"])
        cat_name = tone_cat["category"]

        # 3. Conversation type
        conv_type = category_map.get(cat_name, "playful-banter")

        # 4. Variety bundle
        bundle = pick_variety_bundle(conv_type)

        # 5. Ending category
        ending_cat = pick_ending_category(
            conv_type, bundle["env"].get("ending_lean", [])
        )

        # 6. Prompt
        prompt = build_prompt(leaf, tone_cat, bundle, ending_cat, axes)

        # 7. Gemini
        text, call_reason = _call_with_key_rotation(key_rotator, prompt, deadline=deadline)
        if text is None:
            # Bail out immediately on any non-retriable reason.
            if call_reason in (
                "server_side_down",
                "shutdown",
                "deadline_reached",
                "interrupted",
                "all_keys_dead",
            ):
                return None, call_reason, []
            # Retriable (unknown_error, retries_exhausted) — allow another attempt.
            if attempt == 3:
                return None, f"call_failed:{call_reason}", []
            continue

        # 8. Validate + safe-fix
        conversation = parse_conversation(text)
        result = validator.process(conversation)
        if not result["ok"]:
            logger.warning("Validation reject (attempt %d): %s", attempt, result["reason"])
            if attempt == 3:
                return None, f"rejected_validation:{result['reason']}", []
            continue
        conversation = result["text"]
        flags = result["flags"]

        # 9. Dedup
        is_dup, dup_reason = dedup.is_duplicate(
            conversation, db.hash_exists, recent_signatures, recent_openings
        )
        if is_dup:
            logger.warning("Dedup reject (attempt %d): %s", attempt, dup_reason)
            if attempt == 3:
                # Accept as last resort — still record + convert.
                record = converter.to_record(result["turns"])
                db.add_hash(dedup.hash_text(conversation))
                db.add_signature(dedup.signature(conversation))
                db.add_opening(dedup.opening_text(conversation))
                _track_tone(recent_tone_cats, cat_name)
                return record, "accepted_after_dedup_retries_exhausted", flags
            continue

        # 10. Success — convert to ChatML + record bookkeeping, both here.
        record = converter.to_record(result["turns"])
        db.add_hash(dedup.hash_text(conversation))
        db.add_signature(dedup.signature(conversation))
        db.add_opening(dedup.opening_text(conversation))
        _track_tone(recent_tone_cats, cat_name)
        return record, "ok", flags

    return None, "exhausted_retries", []


# ─────────────────────────────────────────────────────────
# Gemini call with key rotation + circuit breaker
# ─────────────────────────────────────────────────────────
def _call_with_key_rotation(
    key_rotator: KeyRotator, prompt: str, deadline: float | None = None
) -> tuple[str | None, str]:
    """Return (text_or_None, reason).

    Reasons:
      "ok"                 — success, text is the raw response
      "server_side_down"   — circuit opened (all keys 5xx'd); server issue
      "shutdown"           — shutdown signal received
      "deadline_reached"   — deadline hit
      "all_keys_dead"      — every key 401/403'd
      "retries_exhausted"  — both models gave up after repeated 5xx
      "interrupted"        — interruptible_sleep bailed during a 5xx wait
      "unknown_error"      — some other Gemini error
    """
    model = PRIMARY_MODEL
    consecutive_5xx = 0

    while True:
        # Circuit breaker: cheapest check, always first.
        if key_rotator.is_circuit_open():
            return None, "server_side_down"
        if deadline is not None and time.monotonic() >= deadline:
            return None, "deadline_reached"
        if _shutdown_requested:
            return None, "shutdown"

        key = key_rotator.wait_for_available_key(
            deadline=deadline,
            stop_check=lambda: _shutdown_requested,
        )
        if key is None:
            # Distinguish why: circuit, shutdown, deadline, or truly dead.
            if key_rotator.is_circuit_open():
                return None, "server_side_down"
            if _shutdown_requested:
                return None, "shutdown"
            if deadline is not None and time.monotonic() >= deadline:
                return None, "deadline_reached"
            logger.critical("All Gemini keys dead")
            notifier.send_critical_alert(
                "All keys dead",
                "Every Gemini API key is dead. Manual intervention needed.",
            )
            return None, "all_keys_dead"

        try:
            text = call_gemini(prompt, key, model=model)
            key_rotator.mark_success(key)
            db.increment_key_requests(_key_id(key))
            return text, "ok"
        except GeminiCallError as exc:
            status = exc.status_code

            if status == 429:
                key_rotator.mark_rate_limited(key)
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
                key_rotator.mark_server_error(key)
                keys_5xx = key_rotator.server_error_count()
                total_keys = key_rotator.total_keys()

                # Circuit check right after marking — server down?
                if key_rotator.is_circuit_open():
                    logger.critical(
                        "Circuit breaker triggered: %d/%d keys returned 5xx. "
                        "Server side issue — bailing out.",
                        keys_5xx, total_keys,
                    )
                    return None, "server_side_down"

                consecutive_5xx += 1

                # Model fallback after enough consecutive 5xx on primary.
                if consecutive_5xx >= MAX_CONSECUTIVE_5XX:
                    if model == PRIMARY_MODEL:
                        model = FALLBACK_MODEL
                        consecutive_5xx = 0
                        logger.info("Switching to FALLBACK_MODEL: %s", model)
                        continue
                    else:
                        logger.warning(
                            "Fallback model also %d consecutive 5xx — giving up on this prompt",
                            consecutive_5xx,
                        )
                        return None, "retries_exhausted"

                logger.warning(
                    "Gemini %d on %s (%d/%d keys 5xx, attempt %d). Retry in %ds.",
                    status, model, keys_5xx, total_keys,
                    consecutive_5xx, SERVER_5XX_SLEEP_SECONDS,
                )
                if not _interruptible_sleep(SERVER_5XX_SLEEP_SECONDS, deadline=deadline):
                    logger.warning(
                        "Interrupted during %d retry sleep (shutdown or deadline); bailing out",
                        status,
                    )
                    return None, "interrupted"
                continue

            logger.error("Gemini call failed: %s", exc)
            return None, "unknown_error"


def _key_id(key: str) -> str:
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

    batch: list[dict] = []
    flagged_batch: list[tuple[dict, list[str]]] = []
    generated = 0
    rejected = 0
    recent_tone_cats: list[str] = []
    run_start = time.monotonic()
    deadline = run_start + RUN_DEADLINE_SECONDS
    graceful_shutdown = False
    server_side_down = False

    for i in range(LOOP_ITERATIONS):
        if _shutdown_requested:
            logger.info(
                "Graceful shutdown requested via signal. Flushing %d batched conversations.",
                len(batch),
            )
            graceful_shutdown = True
            break

        # Circuit may have opened in a previous iteration.
        if key_rotator.is_circuit_open():
            logger.critical(
                "Circuit breaker is OPEN — server side outage. "
                "Flushing %d clean + %d flagged and exiting gracefully.",
                len(batch), len(flagged_batch),
            )
            server_side_down = True
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
            record, reason, flags = generate_one(
                key_rotator, leaf, recent_tone_cats, deadline=deadline,
            )
        except Exception as exc:
            logger.error("Unexpected error generating conversation %d: %s", i, exc)
            rejected += 1
            continue

        if record is None:
            rejected += 1
            if reason in ("server_side_down", "all_keys_dead"):
                logger.critical(
                    "=== %s: server side issue, gracefully shutting down slot ===",
                    reason,
                )
                if key_rotator.is_circuit_open():
                    logger.critical(
                        "All %d keys in this slot returned 5xx — Gemini appears down.",
                        key_rotator.total_keys(),
                    )
                server_side_down = True
                graceful_shutdown = True
                break
            if reason in ("deadline_reached", "shutdown"):
                logger.info(
                    "Graceful shutdown inside generate_one (%s). Flushing %d batched conversations.",
                    reason, len(batch),
                )
                graceful_shutdown = True
                break
            continue

        if flags:
            flagged_batch.append((record, flags))
        else:
            batch.append(record)
        db.increment_leaf_generated(leaf_path, 1)
        generated += 1

        if len(batch) >= BATCH_UPLOAD_THRESHOLD:
            _flush_batch(batch, tag_prefix="batch")
            batch = []
        if len(flagged_batch) >= BATCH_UPLOAD_THRESHOLD:
            _flush_batch([c for c, _ in flagged_batch], tag_prefix="flagged")
            flagged_batch = []

    if batch:
        _flush_batch(batch, tag_prefix="batch")
    if flagged_batch:
        _flush_batch([c for c, _ in flagged_batch], tag_prefix="flagged")

    db.record_generated(generated, rejected)

    if server_side_down:
        logger.critical(
            "Run complete: generated=%d rejected=%d — SERVER SIDE OUTAGE, gracefully shutdown",
            generated, rejected,
        )
    else:
        logger.info(
            "Run complete: generated=%d rejected=%d%s",
            generated, rejected,
            " [graceful_shutdown]" if graceful_shutdown else "",
        )

    _maybe_send_daily_report()
    return 0


def _flush_batch(batch: list[dict], tag_prefix: str = "batch") -> None:
    """Upload one batch and log it. tag_prefix='batch' for clean, 'flagged' for review."""
    ok, url = storage.upload_batch(batch, tag_prefix=tag_prefix)
    slot = os.getenv("SLOT_ID", "0")
    batch_id = f"{tag_prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_s{slot}"
    if ok:
        db.log_batch(batch_id, url, len(batch), tag_prefix=tag_prefix)
        logger.info("Uploaded %s batch of %d conversations to %s", tag_prefix, len(batch), url)
    else:
        logger.error("%s batch upload failed; %d conversations kept locally only", tag_prefix, len(batch))


def _maybe_send_daily_report() -> None:
    try:
        notifier.send_daily_report()
    except Exception as exc:
        logger.error("Failed to send daily report: %s", exc)


if __name__ == "__main__":
    sys.exit(run())