# src/notifier.py
"""
Daily email report via Gmail SMTP. Sends at most once per 24h,
tracked in MongoDB `notifier_state`.

Counts come from `batch_log` collection (ground truth: every uploaded
batch logs its conversation count), NOT from `progress.total_generated`
which only updates on cleanly-finished runs.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from src import db

logger = logging.getLogger(__name__)


def _safe_mongo_size_mb() -> float:
    """Query MongoDB dbStats for data size in MB (best effort)."""
    try:
        stats = db.get_db().command("dbStats")
        return stats.get("dataSize", 0) / (1024 * 1024)
    except Exception:
        return 0.0


def _build_stats_from_db() -> dict:
    """Build report stats from batch_log (ground truth)."""
    database = db.get_db()
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")

    total_uploaded = 0
    yesterday_uploaded = 0
    today_uploaded = 0
    total_batches = 0

    try:
        cursor = database.batch_log.find({}, {"count": 1, "created_at": 1})
        for doc in cursor:
            cnt = doc.get("count", 0)
            total_uploaded += cnt
            total_batches += 1
            created = doc.get("created_at")
            if created:
                if hasattr(created, "strftime"):
                    day_str = created.strftime("%Y-%m-%d")
                    if day_str == yesterday:
                        yesterday_uploaded += cnt
                    elif day_str == today:
                        today_uploaded += cnt
    except Exception as exc:
        logger.warning("batch_log query failed: %s", exc)

    # Progress doc (for days elapsed, leaves)
    progress = db.get_progress()
    started_at = progress.get("started_at", now)
    if isinstance(started_at, datetime):
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        days_elapsed = max(1, (now - started_at).days + 1)
    else:
        days_elapsed = 1

    # Leaves progress
    try:
        total_leaves = len(db.get_leaf_order() or [])
        leaves_completed = db.leaves_completed_count()
    except Exception:
        total_leaves = 0
        leaves_completed = 0

    # Percentage and estimate
    pct = (total_uploaded / 600_000) * 100 if total_uploaded else 0.0
    remaining = max(0, 600_000 - total_uploaded)

    # Average rate from actual data
    if days_elapsed > 0 and total_uploaded > 0:
        avg_per_day = total_uploaded / days_elapsed
    else:
        avg_per_day = 1

    # Recent 3-day rate (more accurate for current speed)
    recent_uploaded = 0
    three_days_ago = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    try:
        for doc in database.batch_log.find({}, {"count": 1, "created_at": 1}):
            created = doc.get("created_at")
            if created and hasattr(created, "strftime"):
                if created.strftime("%Y-%m-%d") >= three_days_ago:
                    recent_uploaded += doc.get("count", 0)
    except Exception:
        pass

    recent_rate = recent_uploaded / 3 if recent_uploaded else avg_per_day
    rate_for_estimate = max(recent_rate, 1)

    eta_days = int(remaining / rate_for_estimate) if remaining else 0
    estimated_finish = (now + timedelta(days=eta_days)).strftime("%Y-%m-%d")

    mongo_mb = _safe_mongo_size_mb()

    return {
        "yesterday_generated": yesterday_uploaded,
        "today_so_far": today_uploaded,
        "total_generated": total_uploaded,
        "total_batches": total_batches,
        "pct_complete": pct,
        "days_elapsed": days_elapsed,
        "estimated_finish": estimated_finish,
        "avg_per_day": int(avg_per_day),
        "recent_rate": int(recent_rate),
        "leaves_completed": leaves_completed,
        "total_leaves": total_leaves,
        "mongo_size_mb": mongo_mb,
    }


def _build_report_body(stats: dict) -> str:
    return f"""=== SUMMARY (ground truth from uploaded batches) ===
Total uploaded:        {stats['total_generated']:,} / 600,000 ({stats['pct_complete']:.2f}%)
Total batches:         {stats['total_batches']:,}
Yesterday uploaded:    {stats['yesterday_generated']:,}
Today so far:          {stats['today_so_far']:,}

=== RATE ===
Average per day:       {stats['avg_per_day']:,}
Last 3 days rate:      {stats['recent_rate']:,}/day
Days elapsed:          {stats['days_elapsed']}
Estimated finish:      {stats['estimated_finish']}

=== PROGRESS ===
Leaves completed:      {stats['leaves_completed']} / {stats['total_leaves']}
MongoDB size:          {stats['mongo_size_mb']:.1f} MB / 512 MB

=== NEXT 24H TARGET ===
Target:                ~{stats['recent_rate']:,} conversations
Remaining to 600k:     {max(0, 600_000 - stats['total_generated']):,}

---
Note: Numbers are counted from actual uploaded batches
(ground truth), not from run counters.
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

    # If stats not passed, build from DB (ground truth)
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
        logger.info("Daily report sent (total=%d)", stats["total_generated"])
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