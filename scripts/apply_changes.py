#!/usr/bin/env python3
# apply_changes.py -- single-file patcher for the aiko-dataset repo.
#
# HOW IT WORKS
#   * Keep this file in scripts/ and run it from the repo root.
#   * The engine (top part) reads THIS file's own text and applies every patch
#     block found below the engine.
#   * A patch block = a start line, content lines, an end line. Every content
#     line is stored as a comment (hash, pipe, space, text) so quoting and
#     indentation can never break.
#   * Block types: WRITE <path> | DELETE <path> | FUNC <path> <Name or Class.method> | PY <label>
#   * Safe: any error or syntax problem restores every file automatically.
#   * Re-running is harmless (unchanged files are skipped).
#   * Options: --dry (show only), --restore (undo the last apply)

import ast
import json
import re
import shutil
import sys
import time
from pathlib import Path

SELF = Path(__file__).resolve()
ROOT = SELF.parent.parent
BACKUP_DIR = ROOT / ".patch_backup"
START = "#@@ "
DRY = "--dry" in sys.argv
ORIG = {}
TOUCHED = []


class PatchError(Exception):
    pass


def log(msg):
    print(msg)


def target(rel):
    p = (ROOT / rel).resolve()
    if ROOT != p and ROOT not in p.parents:
        raise PatchError("path outside repo: " + rel)
    return p


def remember(p):
    if p not in ORIG:
        ORIG[p] = p.read_bytes() if p.exists() else None


def put(rel, text):
    p = target(rel)
    old = p.read_text(encoding="utf-8") if p.exists() else None
    if old == text:
        log("  same      " + rel)
        return False
    log("  " + ("new      " if old is None else "changed  ") + rel)
    if DRY:
        return True
    remember(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if p not in TOUCHED:
        TOUCHED.append(p)
    return True


def read_blocks(text):
    blocks, cur = [], None
    for n, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if line.startswith(START):
            head = line[len(START):].strip()
            if head == "END":
                if cur is None:
                    raise PatchError("line %d: END without a block" % n)
                blocks.append(cur)
                cur = None
                continue
            if cur is not None:
                raise PatchError("line %d: block '%s' is not closed" % (n, cur["head"]))
            parts = head.split()
            cur = {"head": head, "op": parts[0].upper(), "args": parts[1:], "lines": [], "line": n}
        elif cur is not None:
            if line.startswith("#| "):
                cur["lines"].append(line[3:])
            elif line.startswith("#|"):
                cur["lines"].append(line[2:])
            elif line.strip() == "" or line.startswith("#"):
                continue
            else:
                raise PatchError("line %d: stray text inside block '%s'" % (n, cur["head"]))
    if cur is not None:
        raise PatchError("block '%s' never closed" % cur["head"])
    return blocks


def find_def(body, names):
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == names[0]:
            if len(names) == 1:
                return node
            return find_def(node.body, names[1:])
    return None


def op_func(rel, qual, body):
    p = target(rel)
    if not p.exists():
        raise PatchError("FUNC: file missing " + rel)
    text = p.read_text(encoding="utf-8")
    lines = text.split("\n")
    node = find_def(ast.parse(text).body, qual.split("."))
    new = body.rstrip("\n").split("\n")
    if node is None:
        if "." in qual:
            raise PatchError("FUNC: %s not found in %s" % (qual, rel))
        while lines and lines[-1].strip() == "":
            lines.pop()
        out = lines + ["", ""] + new + [""]
    else:
        start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
        end = node.end_lineno
        first = lines[start]
        indent = first[: len(first) - len(first.lstrip())]
        if indent:
            new = [(indent + l) if l.strip() else l for l in new]
        out = lines[:start] + new + lines[end:]
    put(rel, "\n".join(out))


def run_block(b):
    op, args = b["op"], b["args"]
    body = "\n".join(b["lines"]) + "\n"
    log("[%s] %s" % (op, " ".join(args)))
    if op == "WRITE" and len(args) == 1:
        put(args[0], body)
    elif op == "DELETE" and len(args) == 1:
        p = target(args[0])
        if p.exists():
            log("  delete    " + args[0])
            if not DRY:
                remember(p)
                p.unlink()
        else:
            log("  absent    " + args[0])
    elif op == "FUNC" and len(args) == 2:
        op_func(args[0], args[1], body)
    elif op == "PY" and len(args) >= 1:
        ns = {
            "ROOT": ROOT, "re": re, "json": json, "log": log, "write": put,
            "read": lambda rel: target(rel).read_text(encoding="utf-8"),
            "exists": lambda rel: target(rel).exists(),
            "load_json": lambda rel: json.loads(target(rel).read_text(encoding="utf-8")),
            "dump_json": lambda rel, obj: put(rel, json.dumps(obj, ensure_ascii=False, indent=2) + "\n"),
        }
        exec(compile(body, "<PY %s>" % args[0], "exec"), ns)
    else:
        raise PatchError("line %d: bad block '%s'" % (b["line"], b["head"]))


def ensure_ignore():
    p = target(".gitignore")
    cur = p.read_text(encoding="utf-8") if p.exists() else ""
    if ".patch_backup/" not in cur:
        put(".gitignore", cur.rstrip("\n") + "\n\n# patcher backups\n.patch_backup/\n")


def verify():
    for p in TOUCHED:
        if not p.exists():
            continue
        if p.suffix == ".py":
            compile(p.read_text(encoding="utf-8"), str(p), "exec")
        elif p.suffix == ".json":
            json.loads(p.read_text(encoding="utf-8"))


def rollback():
    for p, data in ORIG.items():
        try:
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
        except OSError as exc:
            log("  rollback problem %s: %s" % (p, exc))


def save_backup():
    if not ORIG:
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    base = BACKUP_DIR / stamp
    base.mkdir(parents=True, exist_ok=True)
    created = []
    for p, data in ORIG.items():
        rel = p.relative_to(ROOT)
        if data is None:
            created.append(str(rel))
            continue
        dst = base / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
    (base / "_created.json").write_text(json.dumps(created), encoding="utf-8")
    olds = sorted(d for d in BACKUP_DIR.iterdir() if d.is_dir())
    for d in olds[:-8]:
        shutil.rmtree(d, ignore_errors=True)
    log("backup saved: .patch_backup/" + stamp)


def restore():
    dirs = sorted(d for d in BACKUP_DIR.iterdir() if d.is_dir()) if BACKUP_DIR.exists() else []
    if not dirs:
        raise PatchError("no backup found")
    base = dirs[-1]
    for f in base.rglob("*"):
        if f.is_file() and f.name != "_created.json":
            rel = f.relative_to(base)
            dst = ROOT / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst)
            log("  restored  " + str(rel))
    for rel in json.loads((base / "_created.json").read_text(encoding="utf-8")):
        q = ROOT / rel
        if q.exists():
            q.unlink()
            log("  removed   " + rel)
    shutil.rmtree(base, ignore_errors=True)
    log("restore done")


def main():
    try:
        if "--restore" in sys.argv:
            restore()
            return 0
        blocks = read_blocks(SELF.read_text(encoding="utf-8"))
        log("%d patch block(s)%s" % (len(blocks), "  [DRY RUN]" if DRY else ""))
        for b in blocks:
            run_block(b)
        if not DRY:
            ensure_ignore()
            verify()
            save_backup()
        log("done. " + ("nothing written (dry run)" if DRY else "%d file(s) written" % len(TOUCHED)))
        return 0
    except Exception as exc:
        log("\nERROR: %s: %s" % (type(exc).__name__, exc))
        if not DRY:
            rollback()
            log("all files are back to the state before this run")
        return 1


if __name__ == "__main__":
    sys.exit(main())

# ============================= PART 11 =============================

#@@ FUNC src/generator.py call_gemini
#| def call_gemini(prompt: str, api_key: str, model: str = PRIMARY_MODEL) -> str:
#|     client = genai.Client(api_key=api_key)
#|     gen_config = {
#|         "temperature": TEMPERATURE,
#|         "top_p": TOP_P,
#|         "top_k": TOP_K,
#|         "max_output_tokens": MAX_OUTPUT_TOKENS,
#|     }
#|
#|     def _call(with_config: bool):
#|         if with_config:
#|             return client.interactions.create(model=model, input=prompt, generation_config=gen_config)
#|         return client.interactions.create(model=model, input=prompt)
#|
#|     try:
#|         response = _call(True)
#|     except TypeError:
#|         response = _call(False)
#|     except Exception as exc:
#|         status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
#|         if status in (400, 401, 403, 404, 429):
#|             raise GeminiCallError(str(exc), status_code=status) from exc
#|         try:
#|             response = _call(True)
#|         except Exception as exc2:
#|             status2 = getattr(exc2, "code", None) or getattr(exc2, "status_code", None)
#|             raise GeminiCallError(str(exc2), status_code=status2 or status) from exc2
#|
#|     text = _extract_text(response).strip()
#|     if not text:
#|         raise GeminiCallError("Empty response from Gemini", status_code=None)
#|     return text
#@@ END

#@@ FUNC src/db.py claim_daily_report
#| def claim_daily_report(date_str: str) -> bool:
#|     """Atomically claim today's report so only ONE slot sends it."""
#|     db = get_db()
#|     try:
#|         r = db.notifier_state.update_one(
#|             {"_id": "email", "last_sent_date": {"$ne": date_str}},
#|             {"$set": {"last_sent_date": date_str}},
#|             upsert=True,
#|         )
#|         return r.modified_count == 1 or r.upserted_id is not None
#|     except PyMongoError:
#|         return False
#@@ END

#@@ FUNC src/db.py unclaim_daily_report
#| def unclaim_daily_report() -> None:
#|     get_db().notifier_state.update_one({"_id": "email"}, {"$set": {"last_sent_date": ""}})
#@@ END

#@@ FUNC src/notifier.py send_daily_report
#| def send_daily_report(stats: dict | None = None) -> None:
#|     today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
#|     if db.get_last_sent_date() == today:
#|         logger.info("Daily report already sent today, skipping")
#|         return
#|
#|     gmail_user = os.getenv("GMAIL_USER")
#|     gmail_pass = os.getenv("GMAIL_APP_PASSWORD")
#|     notify_email = os.getenv("NOTIFY_EMAIL")
#|     if not all([gmail_user, gmail_pass, notify_email]):
#|         logger.error("Email env vars missing, skipping daily report")
#|         return
#|
#|     if not db.claim_daily_report(today):
#|         logger.info("Daily report already claimed by another slot, skipping")
#|         return
#|
#|     try:
#|         if stats is None:
#|             stats = _build_stats_from_db()
#|         body = _build_report_body(stats)
#|         msg = MIMEText(body)
#|         msg["Subject"] = f"Aiko Dataset — Daily Report [{today}]"
#|         msg["From"] = gmail_user
#|         msg["To"] = notify_email
#|         with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
#|             server.login(gmail_user, gmail_pass)
#|             server.sendmail(gmail_user, [notify_email], msg.as_string())
#|         logger.info(
#|             "Daily report sent (total=%d clean=%d flagged=%d)",
#|             stats["total_generated"], stats["total_clean"], stats["total_flagged"],
#|         )
#|     except Exception as exc:
#|         db.unclaim_daily_report()
#|         logger.error("Failed to send daily report: %s", exc)
#@@ END

#@@ FUNC src/storage.py _get_or_create_release
#| def _get_or_create_release(tag: str) -> dict | None:
#|     repo = _repo()
#|     url = f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}"
#|     try:
#|         r = requests.get(url, headers=_headers(), timeout=30)
#|         if r.status_code == 200:
#|             return r.json()
#|         if r.status_code == 404:
#|             r2 = requests.post(
#|                 f"{GITHUB_API}/repos/{repo}/releases",
#|                 headers=_headers(),
#|                 json={"tag_name": tag, "name": tag, "body": f"Aiko dataset batches for {tag}"},
#|                 timeout=30,
#|             )
#|             if r2.status_code in (200, 201):
#|                 return r2.json()
#|             if r2.status_code == 422:
#|                 r3 = requests.get(url, headers=_headers(), timeout=30)
#|                 if r3.status_code == 200:
#|                     return r3.json()
#|             logger.error("Failed to create release %s: %s %s", tag, r2.status_code, r2.text)
#|             return None
#|         logger.error("Unexpected status checking release %s: %s %s", tag, r.status_code, r.text)
#|         return None
#|     except requests.RequestException as exc:
#|         logger.error("Network error contacting GitHub Releases: %s", exc)
#|         return None
#@@ END

#@@ FUNC src/storage.py upload_batch
#| def upload_batch(records: list, tag_prefix: str = "batch") -> tuple[bool, str]:
#|     """Save locally, then upload as one asset (3 attempts). Returns (ok, url)."""
#|     import time as _time
#|
#|     now = datetime.now(timezone.utc)
#|     filename = f"{tag_prefix}_{now.strftime('%Y%m%d_%H%M%S')}{_slot_suffix()}.jsonl"
#|     local_path = write_local_batch(records, filename)
#|
#|     if tag_prefix == "flagged":
#|         tag = FLAGGED_RELEASE_TAG
#|     else:
#|         tag = f"{tag_prefix}-{now.strftime('%Y-%m-%d')}"
#|
#|     for attempt in range(1, 4):
#|         release = _get_or_create_release(tag)
#|         if release is not None:
#|             upload_url = release["upload_url"].split("{")[0]
#|             try:
#|                 with local_path.open("rb") as f:
#|                     headers = _headers()
#|                     headers["Content-Type"] = "application/jsonl"
#|                     r = requests.post(
#|                         upload_url, headers=headers, params={"name": filename},
#|                         data=f.read(), timeout=60,
#|                     )
#|                 if r.status_code in (200, 201):
#|                     asset = r.json()
#|                     return True, asset.get("browser_download_url", release.get("html_url", ""))
#|                 if r.status_code == 422:
#|                     return True, release.get("html_url", "")
#|                 logger.error("Asset upload failed (attempt %d): %s %s", attempt, r.status_code, r.text)
#|             except (requests.RequestException, OSError) as exc:
#|                 logger.error("Exception uploading batch asset (attempt %d): %s", attempt, exc)
#|         if attempt < 3:
#|             _time.sleep(3 * attempt)
#|
#|     logger.error("Could not upload %s after 3 attempts; local file only", filename)
#|     return False, ""
#@@ END