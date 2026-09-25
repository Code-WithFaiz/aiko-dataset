# src/notifier.py
"""
Daily email report via Gmail SMTP. Sends at most once per 24h,
tracked in MongoDB `notifier_state`.

Counts come from `batch_log` collection (ground truth):
  - clean batches   (tag_prefix="batch")   -> training data
  - flagged batches (tag_prefix="flagged") -> review bucket

Both count toward leaf-quota progress (every generated conversation
increments its leaf's counter, clean or flagged). The report shows
clean vs flagged separately so the training-data total stays honest.

Legacy batch_log entries without tag_prefix default to "batch".

Key health section is READ-ONLY from MongoDB `key_stats`.
ZERO Gemini API calls -- no impact on running system.
"""
from __future__ import annotations

import hashlib
import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from src import db

logger = logging.getLogger(__name__)


def _key_id(key: str) -> str:
    """Same hash used by main.py -- deterministic mapping key -> key_stats._id."""
    return f"key_{hashlib.sha256(key.encode()).hexdigest()[:12]}"


def _safe_mongo_size_mb() -> float:
    try:
        stats = db.get_db().command("dbStats")
        return stats.get("dataSize", 0) / (1024 * 1024)
    except Exception:
        return 0.0


def _build_key_health() -> dict:
    """Read-only key health from MongoDB. ZERO Gemini API calls."""
    database = db.get_db()
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    working, dead, cooling, unknown = [], [], [], []

    for i in range(1, 51):
        key = os.getenv(f"GEMINI_KEY_{i}")
        if not key:
            continue
        kid = _key_id(key)
        try:
            doc = database.key_stats.find_one({"_id": kid})
        except Exception:
            unknown.append(i)
            continue
        if doc is None:
            unknown.append(i)
            continue
        if doc.get("dead"):
            dead.append(i)
            continue
        cooldown = doc.get("cooldown_until")
        if cooldown and isinstance(cooldown, datetime):
            if cooldown.tzinfo is not None:
                cooldown = cooldown.replace(tzinfo=None)
            if cooldown > now:
                cooling.append(i)
                continue
        working.append(i)

    return {
        "working": working,
        "dead": dead,
        "cooling": cooling,
        "unknown": unknown,
        "total": len(working) + len(dead) + len(cooling) + len(unknown),
    }


def _build_stats_from_db() -> dict:
    """Build report stats from batch_log (single pass, per-tag buckets)."""
    database = db.get_db()
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d")

    total_by_tag = {"batch": 0, "flagged": 0}
    yesterday_by_tag = {"batch": 0, "flagged": 0}
    today_by_tag = {"batch": 0, "flagged": 0}
    recent_by_tag = {"batch": 0, "flagged": 0}
    total_batches = 0

    try:
        cursor = database.batch_log.find(
            {}, {"count": 1, "created_at": 1, "tag_prefix": 1}
        )
        for doc in cursor:
            cnt = doc.get("count", 0) or 0
            tag = doc.get("tag_prefix", "batch") or "batch"
            if tag not in total_by_tag:
                tag = "batch"  # unknown tag -> treat as batch

            total_batches += 1
            total_by_tag[tag] += cnt

            created = doc.get("created_at")
            if created and hasattr(created, "strftime"):
                day_str = created.strftime("%Y-%m-%d")
                if day_str == yesterday:
                    yesterday_by_tag[tag] += cnt
                elif day_str == today:
                    today_by_tag[tag] += cnt
                if day_str >= three_days_ago:
                    recent_by_tag[tag] += cnt
    except Exception as exc:
        logger.warning("batch_log query failed: %s", exc)

    clean_total = total_by_tag["batch"]
    flagged_total = total_by_tag["flagged"]
    grand_total = clean_total + flagged_total

    yesterday_total = yesterday_by_tag["batch"] + yesterday_by_tag["flagged"]
    today_total = today_by_tag["batch"] + today_by_tag["flagged"]
    recent_total = recent_by_tag["batch"] + recent_by_tag["flagged"]

    progress = db.get_progress()
    started_at = progress.get("started_at", now)
    if isinstance(started_at, datetime):
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        days_elapsed = max(1, (now - started_at).days + 1)
    else:
        days_elapsed = 1

    try:
        total_leaves = len(db.get_leaf_order() or [])
        leaves_completed = db.leaves_completed_count()
    except Exception:
        total_leaves = 0
        leaves_completed = 0

    pct = (grand_total / 600_000) * 100 if grand_total else 0.0
    remaining = max(0, 600_000 - grand_total)

    avg_per_day = (grand_total / days_elapsed) if days_elapsed > 0 and grand_total else 1
    recent_rate = recent_total / 3 if recent_total else avg_per_day
    rate_for_estimate = max(recent_rate, 1)
    eta_days = int(remaining / rate_for_estimate) if remaining else 0
    estimated_finish = (now + timedelta(days=eta_days)).strftime("%Y-%m-%d")

    clean_pct = (clean_total / grand_total * 100) if grand_total else 0.0
    flagged_pct = (flagged_total / grand_total * 100) if grand_total else 0.0

    return {
        "yesterday_total": yesterday_total,
        "yesterday_clean": yesterday_by_tag["batch"],
        "yesterday_flagged": yesterday_by_tag["flagged"],
        "today_total": today_total,
        "today_clean": today_by_tag["batch"],
        "today_flagged": today_by_tag["flagged"],
        "total_generated": grand_total,
        "total_clean": clean_total,
        "total_flagged": flagged_total,
        "clean_pct": clean_pct,
        "flagged_pct": flagged_pct,
        "total_batches": total_batches,
        "pct_complete": pct,
        "days_elapsed": days_elapsed,
        "estimated_finish": estimated_finish,
        "avg_per_day": int(avg_per_day),
        "recent_rate": int(recent_rate),
        "leaves_completed": leaves_completed,
        "total_leaves": total_leaves,
        "mongo_size_mb": _safe_mongo_size_mb(),
        "key_health": _build_key_health(),
    }


def _format_key_list(keys: list) -> str:
    if not keys:
        return "none"
    return ", ".join(f"KEY_{i}" for i in sorted(keys))


def _build_report_body(stats: dict) -> str:
    kh = stats.get("key_health", {})
    working = kh.get("working", [])
    dead = kh.get("dead", [])
    cooling = kh.get("cooling", [])
    unknown = kh.get("unknown", [])

    return f"""=== SUMMARY ===
Total generated:       {stats['total_generated']:,} / 600,000 ({stats['pct_complete']:.2f}%)
Total batches:         {stats['total_batches']:,}
Yesterday generated:   {stats['yesterday_total']:,}
Today so far:          {stats['today_total']:,}

=== CLEAN vs FLAGGED ===
Clean (training data): {stats['total_clean']:,}  ({stats['clean_pct']:.1f}% of total)
Flagged (review):      {stats['total_flagged']:,}  ({stats['flagged_pct']:.1f}% of total)
Yesterday clean:       {stats['yesterday_clean']:,}
Yesterday flagged:     {stats['yesterday_flagged']:,}
Today clean:           {stats['today_clean']:,}
Today flagged:         {stats['today_flagged']:,}

=== RATE ===
Average per day:       {stats['avg_per_day']:,}
Last 3 days rate:      {stats['recent_rate']:,}/day
Days elapsed:          {stats['days_elapsed']}
Estimated finish:      {stats['estimated_finish']}

=== KEY HEALTH (read-only, zero API calls) ===
Total keys in env:     {kh.get('total', 0)}
Working:               {len(working)}  -> {_format_key_list(working)}
Cooling (rate limit):  {len(cooling)}  -> {_format_key_list(cooling)}
Dead:                  {len(dead)}  -> {_format_key_list(dead)}
Untested (never used): {len(unknown)}  -> {_format_key_list(unknown)}

=== PROGRESS ===
Leaves completed:      {stats['leaves_completed']} / {stats['total_leaves']}
MongoDB size:          {stats['mongo_size_mb']:.1f} MB / 512 MB

=== NEXT 24H TARGET ===
Target:                ~{stats['recent_rate']:,} conversations
Remaining to 600k:     {max(0, 600_000 - stats['total_generated']):,}

---
Key health is READ-ONLY from MongoDB key_stats. No Gemini API calls.
"""


def send_daily_report(stats: dict | None = None) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if db.get_last_sent_date() == today:
        logger.info("Daily report already sent today, skipping")
        return

    gmail_user = os.getenv("GMAIL_USER")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
    notify_email = os.getenv("NOTIFY_EMAIL")
    if not all([gmail_user, gmail_pass, notify_email]):
        logger.error("Email env vars missing, skipping daily report")
        return

    if stats is None:
        stats = _build_stats_from_db()

    body = _build_report_body(stats)
    msg = MIMEText(body)
    msg["Subject"] = f"Aiko Dataset — Daily Report [{today}]"
    msg["From"] = gmail_user
    msg["To"] = notify_email

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [notify_email], msg.as_string())
        db.set_last_sent_date(today)
        logger.info(
            "Daily report sent (total=%d clean=%d flagged=%d)",
            stats["total_generated"],
            stats["total_clean"],
            stats["total_flagged"],
        )
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Failed to send daily report: %s", exc)


def send_critical_alert(subject: str, body: str) -> None:
    gmail_user = os.getenv("GMAIL_USER")
    gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
    notify_email = os.getenv("NOTIFY_EMAIL")
    if not all([gmail_user, gmail_pass, notify_email]):
        logger.error("Email env vars missing, cannot send critical alert: %s", subject)
        return
    msg = MIMEText(body)
    msg["Subject"] = f"[CRITICAL] Aiko Dataset — {subject}"
    msg["From"] = gmail_user
    msg["To"] = notify_email
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(gmail_user, gmail_pass)
            server.sendmail(gmail_user, [notify_email], msg.as_string())
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Failed to send critical alert: %s", exc)