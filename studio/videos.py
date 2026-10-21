"""Event videos: uploaded on the event page, shown only in the client gallery (never on the guests' page).

Videos aren't searched by face and get no thumbnails or re-encoding: the browser plays the original, with range
requests so it can seek. MP4 (H.264) plays in every browser; MOV from iPhones plays in Safari and usually Chrome.
Videos count towards the plan's storage, like photos.
"""
import hashlib
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import List

from fastapi import Depends, File, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, db
from .ingest import PlanLimit, photo_usage
from .storage import get_storage

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".webm"}
TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/mp4", ".webm": "video/webm"}  # MOV as MP4: Chrome plays it then


class VideoIds(BaseModel):
    ids: List[str] = Field(max_length=500)


def video_path(r) -> Path:
    return get_storage().event_dir(r["account_id"], r["event_id"]) / "video" / f"{r['public_id']}{r['ext']}"


def add_video(account_id: int, event_id: int, plan: dict, name: str, stream, vendor_id: int = None) -> dict:
    """Store one uploaded video. Returns {"name", "status": queued | duplicate | rejected, "error", "id"}."""
    name = Path(name or "video").name[:200] or "video"
    ext = Path(name).suffix.lower()
    res = {"name": name, "status": "rejected", "error": ""}
    if ext not in VIDEO_EXTS:
        res["error"] = "Use an MP4, MOV, M4V or WebM video."
        return res
    limit = config.settings.max_video_mb * 1024 * 1024
    pub = secrets.token_urlsafe(12)
    row = {"account_id": account_id, "event_id": event_id, "public_id": pub, "ext": ext}
    final = video_path(row)
    final.parent.mkdir(parents=True, exist_ok=True)
    part = final.with_name(final.name + ".part")
    h, size = hashlib.sha256(), 0
    with open(part, "wb") as out:
        while True:
            chunk = stream.read(4 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                break
            h.update(chunk)
            out.write(chunk)
    if size > limit or size == 0:
        part.unlink(missing_ok=True)
        res["error"] = f"Videos can be up to {config.settings.max_video_mb:,} MB." if size else "File is empty."
        return res
    try:
        with db.tx():  # counted inside the write lock, like photos
            if plan["storage"] is not None and photo_usage(account_id)["storage"] + size > plan["storage"]:
                raise PlanLimit(f"Storage limit of the {plan['label']} plan reached.")
            db.ex("""INSERT INTO videos(public_id, account_id, event_id, orig_name, ext, size, sha256, created_at, vendor_id)
                     VALUES(?,?,?,?,?,?,?,?,?)""", (pub, account_id, event_id, name, ext, size, h.hexdigest(), int(time.time()), vendor_id))
    except PlanLimit as e:
        part.unlink(missing_ok=True)
        res["error"] = str(e)
        return res
    except sqlite3.IntegrityError:
        part.unlink(missing_ok=True)
        res.update(status="duplicate", error="Already uploaded to this event.")
        return res
    os.replace(part, final)
    res.update(status="queued", error="", id=pub)
    return res


def out(r) -> dict:
    return {"id": r["public_id"], "name": r["orig_name"], "size": r["size"], "created_at": r["created_at"]}


def stream_file(r, err, download=False):
    p = video_path(r)
    if not p.exists():
        raise err(404, "Video not found.", "not_found")
    kw = {"filename": r["orig_name"], "content_disposition_type": "attachment"} if download else {}
    return FileResponse(p, media_type=TYPES.get(r["ext"], "video/mp4"), headers={"Cache-Control": "private, max-age=3600"}, **kw)


def mount_videos(app, h):
    """h: helpers from server.create_app (user_dep, can, err, get_event, plan_of, client_event)."""
    err = h.err

    def get_video(eid, vid, account_id=None):
        sql, args = "SELECT * FROM videos WHERE public_id=? AND event_id=?", [vid, eid]
        if account_id is not None:
            sql, args = sql + " AND account_id=?", args + [account_id]
        r = db.q1(sql, args)
        if not r:
            raise err(404, "Video not found.", "not_found")
        return r

    # ------------------------------------------------------------------ the studio (dashboard)
    @app.get("/api/events/{eid}/videos")
    def list_videos(eid: int, u: dict = Depends(h.user_dep)):
        h.get_event(u, eid)
        rows = db.q("""SELECT v.*, l.name AS vendor FROM videos v LEFT JOIN vendor_links l ON l.id=v.vendor_id
                       WHERE v.event_id=? AND v.account_id=? ORDER BY v.orig_name COLLATE NOCASE, v.id""", (eid, u["account_id"]))
        return [{**out(r), "vendor": r["vendor"]} for r in rows]  # which vendor sent it, if one did

    @app.post("/api/events/{eid}/videos")
    def upload_videos(eid: int, files: List[UploadFile] = File(...), u: dict = Depends(h.can("upload"))):
        h.get_event(u, eid)
        if len(files) > 5:
            raise err(413, "Send at most 5 videos per request.", "too_many")
        plan = h.plan_of(u["plan"])
        results = [add_video(u["account_id"], eid, plan, f.filename, f.file) for f in files]
        return {"results": results, "added": sum(r["status"] == "queued" for r in results)}

    @app.get("/api/events/{eid}/videos/{vid}")
    def play(eid: int, vid: str, download: bool = False, u: dict = Depends(h.user_dep)):
        h.get_event(u, eid)
        return stream_file(get_video(eid, vid, u["account_id"]), err, download)

    @app.post("/api/events/{eid}/videos/delete")
    def delete_videos(eid: int, body: VideoIds, u: dict = Depends(h.can("delete_photos"))):
        h.get_event(u, eid)
        n = 0
        for vid in body.ids:
            r = db.q1("SELECT * FROM videos WHERE public_id=? AND event_id=? AND account_id=?", (vid, eid, u["account_id"]))
            if r:
                db.run("DELETE FROM videos WHERE id=?", (r["id"],))
                video_path(r).unlink(missing_ok=True)
                n += 1
        return {"deleted": n}

    # ------------------------------------------------------------------ the client gallery (no login; PIN if set)
    @app.get("/c/{token}/videos")
    def client_videos(token: str, request: Request):
        e = h.client_open(token, request)
        return [out(r) for r in db.q("SELECT * FROM videos WHERE event_id=? ORDER BY orig_name COLLATE NOCASE, id", (e["id"],))]

    @app.get("/c/{token}/video/{vid}")
    def client_play(token: str, vid: str, request: Request):
        e = h.client_open(token, request)
        return stream_file(get_video(e["id"], vid), err)

    @app.get("/c/{token}/video/{vid}/download")
    def client_download(token: str, vid: str, request: Request):
        e = h.client_open(token, request)
        if not e["client_download"]:
            raise err(403, "Downloads are turned off for this gallery.", "no_download")
        return stream_file(get_video(e["id"], vid), err, download=True)
