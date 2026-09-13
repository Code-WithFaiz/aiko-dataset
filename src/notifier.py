# src/notifier.py
"""
Daily email report via Gmail SMTP (Section 10). Sends at most once
per 24h, tracked in MongoDB `notifier_state`.
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText

from src import db

logger = logging.getLogger(__name__)


def _build_report_body(stats: dict) -> str:
    return f"""=== SUMMARY ===
Yesterday generated: {stats['yesterday_generated']}
Yesterday rejected:  {stats['yesterday_rejected']}
Total so far:        {stats['total_generated']} / 600,000 ({stats['pct_complete']:.2f}%)
Days elapsed:        {stats['days_elapsed']} / 90
Estimated finish:    {stats['estimated_finish']}

=== QUALITY ===
Reject rate:         {stats['validation_reject_pct']:.2f}% (validation) + {stats['dup_reject_pct']:.2f}% (duplicate)
Top reject reasons:  {stats['top_reject_reasons']}

=== PROGRESS ===
Leaves completed:    {stats['leaves_completed']} / {stats['total_leaves']}
Current leaf:        {stats['current_leaf']}
MongoDB size:        {stats['mongo_size_mb']:.1f} / 512 MB

=== ERRORS (last 24h) ===
{stats['error_count']}
{stats['top_errors']}

=== NEXT 24H TARGET ===
{stats['next_target']}
"""


def send_daily_report(stats: dict) -> None:
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
        logger.info("Daily report sent")
    except (smtplib.SMTPException, OSError) as exc:
        # Section 11: email fails -> log only, don't crash
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