"""Adding one photo to an event: the single path every upload takes (the dashboard and cameras over FTP).

Checks the file type and size, the plan's photo and storage limits (again inside the write lock, so parallel
uploads can't overshoot them), drops exact duplicates by SHA-256, stores the original and queues it for the
photo workers (thumbnail, web copy, face vectors).
"""
import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path

from . import config, db
from .storage import get_storage


class PlanLimit(Exception):
    pass


def remove_leftovers() -> int:
    """At start-up: half-written uploads (.part files) from before a restart. Their photos were marked failed."""
    root, n = get_storage().root / "accounts", 0
    for f in root.glob("*/events/*/orig/*.part") if root.exists() else ():
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def storage_quota(account_id: int) -> int:
    """The storage the platform admin allocated to this company, in bytes (0: none yet)."""
    r = db.q1("SELECT storage_quota FROM accounts WHERE id=?", (account_id,))
    return int(r["storage_quota"] or 0) if r else 0


def storage_full(quota: int) -> str:
    """Why a file doesn't fit."""
    if not quota:
        return "No storage has been allocated to your company yet. Ask the platform administrator to allocate storage."
    gb = quota / 1024 ** 3
    return f"Your storage ({gb:g} GB) is full. Ask the platform administrator to allocate more."


def photo_usage(account_id: int) -> dict:
    """Photos count, and storage: photos plus videos (both count towards the company's allocated storage)."""
    p = db.q1("""SELECT COUNT(*) c, COALESCE(SUM(size),0) + (SELECT COALESCE(SUM(size),0) FROM videos WHERE account_id=?) s
                 FROM photos WHERE account_id=?""", (account_id, account_id))
    return {"photos": p["c"], "storage": p["s"]}


def add_photo(account_id: int, event_id: int, plan: dict, name: str, stream, used: dict = None, vendor_id: int = None,
              edited: bool = False, edited_of: int = None) -> dict:
    """Store `stream` (anything with .read(n)) as a new photo of the event.

    Returns {"name", "status": queued | duplicate | rejected, "error", "id" (when queued)}. `used` is an optional
    running {"photos", "storage"} count the caller keeps across several files, for the cheap early checks; it is
    updated when the photo is queued. The authoritative check always re-counts inside the transaction. `vendor_id`
    tags a photo sent through a vendor's upload link. `edited` makes it an edited copy (studio/editing.py), made from
    the photo `edited_of` if it still exists: shown everywhere with an Edited tag.
    """
    cfg = config.settings
    name = Path(name or "photo").name[:200] or "photo"
    ext = Path(name).suffix.lower()
    res = {"name": name, "status": "rejected", "error": ""}
    if ext not in config.ALLOWED_EXTS:
        res["error"] = ("Camera RAW files need rawpy on the server (pip install rawpy)." if ext in config.RAW_EXTS
                        else "Unsupported file type (use JPG, PNG, WebP, HEIC or camera RAW).")
        return res
    if used is None:
        used = photo_usage(account_id)
    if plan["photos"] is not None and used["photos"] >= plan["photos"]:
        res["error"] = f"Photo limit of the {plan['label']} plan reached."
        return res
    store = get_storage()
    store.ensure(account_id, event_id)
    pub = secrets.token_urlsafe(12)
    row = {"account_id": account_id, "event_id": event_id, "public_id": pub, "ext": ext}
    final = store.photo_path(row, "orig")
    part = final.with_name(final.name + ".part")
    h, size, too_big = hashlib.sha256(), 0, False
    with open(part, "wb") as out:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > cfg.max_file_mb * 1024 * 1024:
                too_big = True
                break
            h.update(chunk)
            out.write(chunk)
    if too_big or size == 0:
        part.unlink(missing_ok=True)
        res["error"] = f"File is larger than {cfg.max_file_mb} MB." if too_big else "File is empty."
        return res
    quota = storage_quota(account_id)
    if used["storage"] + size > quota:
        part.unlink(missing_ok=True)
        res["error"] = storage_full(quota)
        return res
    replaced = None
    try:
        with db.tx():  # re-count inside the write lock: other uploads may have run in parallel
            # the same file sent again after it failed (an upload cut off by a restart, a worker crash): replace the
            # failed copy instead of refusing it as a duplicate
            old = db.q1("SELECT * FROM photos WHERE event_id=? AND sha256=? AND status='failed'", (event_id, h.hexdigest()))
            if old:
                db.run("DELETE FROM photos WHERE id=?", (old["id"],))
                replaced = old
            now_used = photo_usage(account_id)
            if plan["photos"] is not None and now_used["photos"] >= plan["photos"]:
                raise PlanLimit(f"Photo limit of the {plan['label']} plan reached.")
            if now_used["storage"] + size > quota:
                raise PlanLimit(storage_full(quota))
            now = int(time.time())
            db.ex("""INSERT INTO photos(public_id, account_id, event_id, orig_name, ext, size, sha256, status,
                     created_at, vendor_id, edited, edited_of, edited_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (pub, account_id, event_id, name, ext, size, h.hexdigest(), "uploading", now, vendor_id,
                   int(edited), edited_of, now if edited else None))
    except PlanLimit as e:
        part.unlink(missing_ok=True)
        res["error"] = str(e)
        return res
    except sqlite3.IntegrityError:
        part.unlink(missing_ok=True)
        res.update(status="duplicate", error="Already uploaded to this event.")
        return res
    if replaced:
        store.remove_photo(replaced)
        store.path(replaced["account_id"], replaced["event_id"], "orig", replaced["public_id"],
                   replaced["ext"] + ".part").unlink(missing_ok=True)  # what an interrupted upload left behind
    os.replace(part, final)
    db.run("UPDATE photos SET status='uploaded' WHERE public_id=?", (pub,))
    used["photos"] += 1
    used["storage"] += size
    res.update(status="queued", error="", id=pub)
    from .live import publish   # dashboards show the new photo (camera and vendor uploads too)

    publish(account_id, "photos", event=event_id)
    return res
