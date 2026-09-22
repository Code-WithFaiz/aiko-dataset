# src/db.py
"""
MongoDB Atlas state layer.

Stores ONLY state: progress, leaf quotas, rolling buffers for
non-repeat checks, key stats, batch log, notifier state, and a run
lock for concurrency safety. Counters use atomic find_one_and_update
so concurrent GitHub Actions runs never corrupt state.
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from pymongo import ASCENDING, MongoClient, ReturnDocument, UpdateOne
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import PyMongoError
import random

logger = logging.getLogger(__name__)

_client: Optional[MongoClient] = None
_db: Optional[Database] = None


RUN_LOCK_SLOTS = ["1", "2", "3", "4", "5"]
RUN_LOCK_TTL_SECONDS = 60 * 60
SIGNATURE_BUFFER_CAP = 5000
SCENARIO_BUFFER_CAP = 50
OPENER_BUFFER_CAP = 200
OPENING_BUFFER_CAP = 500
SEEN_HASH_TTL_DAYS = 120


def get_db() -> Database:
    global _client, _db
    if _db is not None:
        return _db
    uri = os.getenv("MONGO_URI")
    db_name = os.getenv("MONGO_DB_NAME")
    if not uri or not db_name:
        raise RuntimeError("MONGO_URI / MONGO_DB_NAME not set")
    _client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    _client.admin.command("ping")  # fail fast if unreachable
    _db = _client[db_name]
    _ensure_indexes(_db)
    return _db


def _ensure_indexes(db: Database) -> None:
    """Create indexes needed for state queries. Idempotent and safe."""
    try:
        db.seen_hashes.create_index("hash", unique=True)
    except PyMongoError as e:
        logger.warning("Could not create seen_hashes.hash index: %s", e)
    try:
        db.seen_hashes.create_index("created_at", expireAfterSeconds=SEEN_HASH_TTL_DAYS * 86400)
    except PyMongoError as e:
        logger.warning("Could not create seen_hashes.created_at TTL index: %s", e)
    # Note: _id is already unique by default, no explicit index needed.


# ---------- Run lock (concurrency safety) ----------

def acquire_run_lock() -> Optional[str]:
    """Try any of the 5 slots. Returns lock_id if acquired, else None."""
    db = get_db()
    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    expires_at = now + timedelta(seconds=RUN_LOCK_TTL_SECONDS)

    slots = list(RUN_LOCK_SLOTS)
    random.shuffle(slots)

    for slot in slots:
        lock_id = f"run_lock_{slot}"
        existing = db.run_lock.find_one({"_id": lock_id})

        if existing is None:
            try:
                db.run_lock.insert_one({
                    "_id": lock_id,
                    "holder": run_id,
                    "expires_at": expires_at,
                    "started_at": now,
                })
                logger.info("Run lock acquired (slot %s): %s", slot, run_id[:8])
                return lock_id
            except PyMongoError:
                continue

        existing_expires = existing.get("expires_at")
        if existing_expires is not None:
            if existing_expires.tzinfo is not None:
                existing_expires = existing_expires.replace(tzinfo=None)
            if existing_expires > now:
                continue

        result = db.run_lock.update_one(
            {"_id": lock_id, "holder": existing.get("holder")},
            {"$set": {"holder": run_id, "expires_at": expires_at, "started_at": now}},
        )
        if result.modified_count == 1:
            logger.info("Run lock taken over (slot %s): %s", slot, run_id[:8])
            return lock_id

    logger.warning("All 4 run-lock slots busy, skipping")
    return None


def release_run_lock(lock_id: str) -> None:
    db = get_db()
    db.run_lock.delete_one({"_id": lock_id})


# ---------- Progress ----------

def get_progress() -> dict:
    db = get_db()
    doc = db.progress.find_one({"_id": "global"})
    if doc is None:
        doc = {
            "_id": "global",
            "total_generated": 0,
            "total_rejected": 0,
            "current_leaf_index": 0,
            "started_at": datetime.now(timezone.utc),
            "last_run_at": None,
            "daily_stats": {},
        }
        db.progress.insert_one(doc)
    return doc


def record_generated(count: int, rejected: int) -> None:
    db = get_db()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.progress.update_one(
        {"_id": "global"},
        {
            "$inc": {
                "total_generated": count,
                "total_rejected": rejected,
                f"daily_stats.{today}.generated": count,
                f"daily_stats.{today}.rejected": rejected,
            },
            "$set": {"last_run_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )


# ---------- Leaf order + quotas ----------

def get_leaf_order() -> Optional[list[str]]:
    db = get_db()
    doc = db.leaf_order.find_one({"_id": "order"})
    return doc["shuffled_paths"] if doc else None


def set_leaf_order(paths: list[str]) -> None:
    db = get_db()
    db.leaf_order.update_one(
        {"_id": "order"}, {"$set": {"shuffled_paths": paths}}, upsert=True
    )


def init_leaf_quotas(leaves: list[dict]) -> None:
    """Idempotent bulk upsert of leaf quotas. ~10x faster than sequential."""
    db = get_db()
    n = len(leaves)
    if n == 0:
        return
    base_quota = 600_000 // n
    remainder = 600_000 % n

    ops = []
    for i, leaf in enumerate(leaves):
        quota = base_quota + (1 if i < remainder else 0)
        ops.append(
            UpdateOne(
                {"_id": leaf["leaf_path"]},
                {"$setOnInsert": {"quota": quota, "generated": 0}},
                upsert=True,
            )
        )

    total_upserted = 0
    for i in range(0, len(ops), 500):
        chunk = ops[i : i + 500]
        try:
            result = db.leaf_state.bulk_write(chunk, ordered=False)
            total_upserted += result.upserted_count
        except PyMongoError as e:
            logger.warning("Bulk leaf init chunk %d failed: %s", i, e)
    logger.info(
        "init_leaf_quotas: %d leaves processed (%d new)", len(leaves), total_upserted
    )


def get_next_leaf_path(ordered_paths: list[str]) -> Optional[str]:
    """First leaf (in shuffled order) whose generated < quota.

    Only fetches unfilled leaves from Mongo, then walks the shuffled
    order to find the earliest one. Much faster than fetching all
    leaf_state docs on every call.
    """
    db = get_db()
    cursor = db.leaf_state.find(
        {"$expr": {"$lt": ["$generated", "$quota"]}},
        {"_id": 1},
    )
    unfilled = {d["_id"] for d in cursor}
    if not unfilled:
        return None
    for path in ordered_paths:
        if path in unfilled:
            return path
    return None


def increment_leaf_generated(leaf_path: str, n: int = 1) -> None:
    db = get_db()
    db.leaf_state.update_one(
        {"_id": leaf_path},
        {"$inc": {"generated": n}, "$set": {"last_updated": datetime.now(timezone.utc)}},
    )


def leaves_completed_count() -> int:
    db = get_db()
    return db.leaf_state.count_documents({"$expr": {"$gte": ["$generated", "$quota"]}})


# ---------- Dedup buffers ----------

def hash_exists(h: str) -> bool:
    db = get_db()
    return db.seen_hashes.find_one({"hash": h}) is not None


def add_hash(h: str) -> None:
    db = get_db()
    try:
        db.seen_hashes.insert_one({"hash": h, "created_at": datetime.now(timezone.utc)})
    except PyMongoError:
        pass  # duplicate key race is fine, hash already recorded


def get_recent_signatures(limit: int = SIGNATURE_BUFFER_CAP) -> list[str]:
    db = get_db()
    cursor = db.recent_signatures.find().sort("created_at", -1).limit(limit)
    return [d["signature"] for d in cursor]


def add_signature(sig: str) -> None:
    db = get_db()
    db.recent_signatures.insert_one({"signature": sig, "created_at": datetime.now(timezone.utc)})
    _trim_collection(db.recent_signatures, "created_at", SIGNATURE_BUFFER_CAP)


def get_recent_openings(limit: int = OPENING_BUFFER_CAP) -> list[str]:
    db = get_db()
    cursor = db.recent_openings.find().sort("created_at", -1).limit(limit)
    return [d["opening_text"] for d in cursor]


def add_opening(text: str) -> None:
    db = get_db()
    db.recent_openings.insert_one({"opening_text": text, "created_at": datetime.now(timezone.utc)})
    _trim_collection(db.recent_openings, "created_at", OPENING_BUFFER_CAP)


def _trim_collection(coll: Collection, sort_field: str, cap: int) -> None:
    count = coll.count_documents({})
    if count <= cap:
        return
    excess = count - cap
    old_docs = list(coll.find().sort(sort_field, ASCENDING).limit(excess))
    ids = [d["_id"] for d in old_docs]
    if ids:
        coll.delete_many({"_id": {"$in": ids}})


# ---------- Scenario / opener rolling buffers ----------

def get_recent_scenarios(limit: int = SCENARIO_BUFFER_CAP) -> list[str]:
    db = get_db()
    cursor = db.recent_scenarios.find().sort("used_at", -1).limit(limit)
    return [d["scenario_id"] for d in cursor]


def add_used_scenarios(scenario_ids: list[str]) -> None:
    db = get_db()
    now = datetime.now(timezone.utc)
    if scenario_ids:
        db.recent_scenarios.insert_many([{"scenario_id": s, "used_at": now} for s in scenario_ids])
    _trim_collection(db.recent_scenarios, "used_at", SCENARIO_BUFFER_CAP)


def get_recent_openers(category: str, limit: int = OPENER_BUFFER_CAP) -> list[str]:
    db = get_db()
    cursor = db.recent_openers.find({"category": category}).sort("used_at", -1).limit(limit)
    return [d["opener_id"] for d in cursor]


def add_used_opener(category: str, opener_id: str) -> None:
    db = get_db()
    db.recent_openers.insert_one(
        {"category": category, "opener_id": opener_id, "used_at": datetime.now(timezone.utc)}
    )
    count = db.recent_openers.count_documents({"category": category})
    if count > OPENER_BUFFER_CAP:
        excess = count - OPENER_BUFFER_CAP
        old = list(db.recent_openers.find({"category": category}).sort("used_at", ASCENDING).limit(excess))
        ids = [d["_id"] for d in old]
        if ids:
            db.recent_openers.delete_many({"_id": {"$in": ids}})


# ---------- Key stats ----------

def update_key_stats(key_id: str, **fields) -> None:
    db = get_db()
    db.key_stats.update_one({"_id": key_id}, {"$set": fields}, upsert=True)


def increment_key_requests(key_id: str) -> None:
    db = get_db()
    db.key_stats.update_one(
        {"_id": key_id},
        {"$inc": {"requests_today": 1}, "$set": {"last_used_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


# ---------- Batch log ----------

def log_batch(batch_id: str, release_url: str, count: int) -> None:
    db = get_db()
    db.batch_log.insert_one(
        {"batch_id": batch_id, "release_url": release_url, "count": count, "created_at": datetime.now(timezone.utc)}
    )


# ---------- Notifier state ----------

def get_last_sent_date() -> Optional[str]:
    db = get_db()
    doc = db.notifier_state.find_one({"_id": "email"})
    return doc["last_sent_date"] if doc else None


def set_last_sent_date(date_str: str) -> None:
    db = get_db()
    db.notifier_state.update_one({"_id": "email"}, {"$set": {"last_sent_date": date_str}}, upsert=True)