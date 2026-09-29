# src/storage.py
"""
GitHub Releases storage.

Normal conversations -> one release per day (tag `batch-YYYY-MM-DD`).
Flagged conversations -> ONE fixed release forever (tag `flagged-conversations`).

Records may be:
  - dict  (ChatML record: {"messages": [...]})
  - str   (raw conversation text; wrapped as {"conversation": ...} for back-compat)

A local copy under data/batches/ is kept for debugging during the run
only (ephemeral on GitHub Actions).
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
FLAGGED_RELEASE_TAG = "flagged-conversations"


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


def _serialize(rec) -> str:
    """Turn one item (dict or str) into a single JSONL line."""
    if isinstance(rec, dict):
        return json.dumps(rec, ensure_ascii=False)
    return json.dumps({"conversation": rec}, ensure_ascii=False)


def write_local_batch(records: list, filename: str) -> Path:
    LOCAL_BATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCAL_BATCH_DIR / filename
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(_serialize(rec) + "\n")
    return path


def _get_or_create_release(tag: str) -> dict | None:
    repo = _repo()
    url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
    try:
        r = requests.get(url, headers=_headers(), timeout=30)
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
            if r2.status_code == 422:
                r3 = requests.get(url, headers=_headers(), timeout=30)
                if r3.status_code == 200:
                    return r3.json()
            logger.error("Failed to create release %s: %s %s", tag, r2.status_code, r2.text)
            return None
        logger.error("Unexpected status checking release %s: %s %s", tag, r.status_code, r.text)
        return None
    except requests.RequestException as exc:
        logger.error("Network error contacting GitHub Releases: %s", exc)
        return None


def upload_batch(records: list, tag_prefix: str = "batch") -> tuple[bool, str]:
    """Save locally, then upload as one asset (3 attempts). Returns (ok, url)."""
    import time as _time

    now = datetime.now(timezone.utc)
    filename = f"{tag_prefix}_{now.strftime('%Y%m%d_%H%M%S')}{_slot_suffix()}.jsonl"
    local_path = write_local_batch(records, filename)

    if tag_prefix == "flagged":
        tag = FLAGGED_RELEASE_TAG
    else:
        tag = f"{tag_prefix}-{now.strftime('%Y-%m-%d')}"

    for attempt in range(1, 4):
        release = _get_or_create_release(tag)
        if release is not None:
            upload_url = release["upload_url"].split("{")[0]
            try:
                with local_path.open("rb") as f:
                    headers = _headers()
                    headers["Content-Type"] = "application/jsonl"
                    r = requests.post(
                        upload_url, headers=headers, params={"name": filename},
                        data=f.read(), timeout=60,
                    )
                if r.status_code in (200, 201):
                    asset = r.json()
                    return True, asset.get("browser_download_url", release.get("html_url", ""))
                if r.status_code == 422:
                    return True, release.get("html_url", "")
                logger.error("Asset upload failed (attempt %d): %s %s", attempt, r.status_code, r.text)
            except (requests.RequestException, OSError) as exc:
                logger.error("Exception uploading batch asset (attempt %d): %s", attempt, exc)
        if attempt < 3:
            _time.sleep(3 * attempt)

    logger.error("Could not upload %s after 3 attempts; local file only", filename)
    return False, ""