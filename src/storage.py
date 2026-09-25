# src/storage.py
"""
GitHub Releases storage: one release per DAY (tag `batch-YYYY-MM-DD`),
each batch of conversations uploaded as a JSONL asset. A local copy
under data/batches/ is always kept regardless of upload outcome.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
LOCAL_BATCH_DIR = Path("data/batches")


def _slot_suffix() -> str:
    slot = os.getenv("SLOT_ID", "").strip()
    return f"_s{slot}" if slot else ""


def _headers() -> dict:
    token = os.getenv("GH_PAT")
    if not token:
        raise RuntimeError("GH_PAT not set")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo() -> str:
    repo = os.getenv("GH_REPO")
    if not repo:
        raise RuntimeError("GH_REPO not set")
    return repo


def write_local_batch(conversations: list[str], filename: str) -> Path:
    LOCAL_BATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCAL_BATCH_DIR / filename
    with path.open("w", encoding="utf-8") as f:
        for conv in conversations:
            f.write(json.dumps({"conversation": conv}, ensure_ascii=False) + "\n")
    return path


def _get_or_create_daily_release(tag: str) -> dict | None:
    repo = _repo()
    try:
        r = requests.get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", headers=_headers(), timeout=30)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            r2 = requests.post(
                f"{GITHUB_API}/repos/{repo}/releases",
                headers=_headers(),
                json={"tag_name": tag, "name": tag, "body": f"Aiko dataset batches for {tag}"},
                timeout=30,
            )
            if r2.status_code in (200, 201):
                return r2.json()
            logger.error("Failed to create release %s: %s %s", tag, r2.status_code, r2.text)
            return None
        logger.error("Unexpected status checking release %s: %s %s", tag, r.status_code, r.text)
        return None
    except requests.RequestException as exc:
        logger.error("Network error contacting GitHub Releases: %s", exc)
        return None


def upload_batch(conversations: list[str]) -> tuple[bool, str]:
    """Save locally, then upload as an asset to today's release.

    Returns (uploaded_ok, release_url_or_empty). Local file is kept
    regardless of upload success (Section 11: upload fails -> keep
    local file, log, continue).
    """
    now = datetime.now(timezone.utc)
    filename = f"batch_{now.strftime('%Y%m%d_%H%M%S')}{_slot_suffix()}.jsonl"
    local_path = write_local_batch(conversations, filename)

    tag = f"batch-{now.strftime('%Y-%m-%d')}"
    release = _get_or_create_daily_release(tag)
    if release is None:
        logger.error("Could not get/create release %s; keeping local file only", tag)
        return False, ""

    upload_url = release["upload_url"].split("{")[0]
    try:
        with local_path.open("rb") as f:
            headers = _headers()
            headers["Content-Type"] = "application/jsonl"
            r = requests.post(upload_url, headers=headers, params={"name": filename}, data=f.read(), timeout=60)
        if r.status_code in (200, 201):
            asset = r.json()
            return True, asset.get("browser_download_url", release.get("html_url", ""))
        logger.error("Asset upload failed: %s %s", r.status_code, r.text)
        return False, ""
    except (requests.RequestException, OSError) as exc:
        logger.error("Exception uploading batch asset: %s", exc)
        return False, ""


def save_flagged(items: list[tuple[str, list[str]]]) -> Path:
    """Local-only bucket for conversations that passed the hard checks but
    were flagged for a soft quality issue. Never uploaded to GitHub
    Releases -- kept only so you can spot-check them by hand."""
    flagged_dir = Path("data/flagged")
    flagged_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = flagged_dir / f"flagged_{now.strftime('%Y%m%d_%H%M%S')}{_slot_suffix()}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for conv, flags in items:
            f.write(json.dumps({"conversation": conv, "flags": flags}, ensure_ascii=False) + "\n")
    logger.info("Saved %d flagged conversations locally to %s", len(items), path)
    return path
