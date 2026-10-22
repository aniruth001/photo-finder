"""Album selection: the client picks photos in the client gallery and sends them to the studio for the album; the studio
can add photos to the same list from the event page.

The client selects photos and taps "Proceed for album"; those photos (and an optional note) are added to the event's
album list. The client can look at the list and take photos out again. The studio sees the list on the event page,
adds its own suggestions, copies the file names (to find them in Lightroom), downloads the originals, and locks the
list once the album is being designed, so it doesn't change underneath the designer. A locked list stops the client's
changes, not the studio's. A deleted photo leaves the list on its own.

Every photo in the list remembers who chose it: the client, the studio (and which team member), or both (each chose it
on their own). Taking a photo out takes it out for both.
"""
import re
import time
from typing import List

from fastapi import Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import db
from .client_gallery import unique_names, zip_stream
from .storage import get_storage

MAX_IDS = 5000


class AlbumIn(BaseModel):
    ids: List[str] = Field(max_length=MAX_IDS)
    note: str = Field("", max_length=1000)


class AlbumIds(BaseModel):
    ids: List[str] = Field(max_length=MAX_IDS)


class StudioAdd(BaseModel):
    ids: List[str] = Field(max_length=MAX_IDS)


class LockIn(BaseModel):
    locked: bool


def photo_ids(event_id: int, public_ids) -> List[int]:
    """Internal ids of this event's ready photos among public_ids (others are ignored)."""
    out, want = [], [p for p in dict.fromkeys(public_ids) if isinstance(p, str) and p]
    for i in range(0, len(want), 500):
        part = want[i:i + 500]
        out += [r["id"] for r in db.q(f"SELECT id FROM photos WHERE event_id=? AND status='ready' AND public_id IN "
                                      f"({','.join('?' * len(part))})", [event_id] + part)]
    return out


def add(event_id: int, ids, by: str, picker: str = ""):
    """Put photos in the list as chosen by `by` (client | studio). Already chosen by the other side: now both."""
    now = int(time.time())
    db.conn().executemany("""INSERT INTO album_picks(event_id, photo_id, picked_at, picked_by, picker) VALUES(?,?,?,?,?)
                             ON CONFLICT(event_id, photo_id) DO UPDATE SET
                               picked_by = CASE WHEN album_picks.picked_by IN (excluded.picked_by, 'both') THEN album_picks.picked_by ELSE 'both' END,
                               picker = CASE WHEN album_picks.picker = '' THEN excluded.picker ELSE album_picks.picker END""",
                          [(event_id, i, now, by, picker) for i in ids])


def picks(event_id: int):
    return db.q("""SELECT p.*, a.picked_at, a.picked_by, a.picker FROM album_picks a JOIN photos p ON p.id=a.photo_id
                   WHERE a.event_id=? AND p.status='ready' ORDER BY p.orig_name COLLATE NOCASE, p.id""", (event_id,))


def remove(event_id: int, public_ids) -> int:
    ids = photo_ids(event_id, public_ids)
    n = 0
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        n += db.run(f"DELETE FROM album_picks WHERE event_id=? AND photo_id IN ({','.join('?' * len(part))})", [event_id] + part)
    return n


def mount_album(app, h):
    """h: helpers from server.create_app (user_dep, can, err, get_event, client_open, client_ip, limiter)."""
    err = h.err

    def state(e) -> dict:
        rows = picks(e["id"])
        by = {k: sum(r["picked_by"] == k for r in rows) for k in ("client", "studio", "both")}
        return {"count": len(rows), "note": e["album_note"] or "", "sent_at": e["album_sent_at"], "locked": bool(e["album_locked"]),
                "by": by,
                "photos": [{"id": r["public_id"], "name": r["orig_name"], "w": r["width"], "h": r["height"], "size": r["size"],
                            "picked_at": r["picked_at"], "by": r["picked_by"], "picker": r["picker"]} for r in rows]}

    def fresh(eid):
        return db.q1("SELECT * FROM events WHERE id=?", (eid,))

    # ------------------------------------------------------------------ the client (no login; PIN if set)
    def client_event(token, request):
        e = h.client_open(token, request)
        if not h.limiter.allow(("album", h.client_ip(request), token), 120, 600):
            raise err(429, "Too many changes. Please wait a few minutes.", "rate")
        if e["album_locked"]:
            raise err(409, "The studio has finalised your album, so the selection can't be changed now.", "locked")
        return e

    @app.get("/c/{token}/album")
    def client_album(token: str, request: Request):
        return state(h.client_open(token, request))

    @app.post("/c/{token}/album")
    def client_add(token: str, body: AlbumIn, request: Request):
        e = client_event(token, request)
        ids = photo_ids(e["id"], body.ids)
        if not ids:
            raise err(400, "Choose the photos for your album first.", "empty")
        now = int(time.time())
        with db.tx() as c:
            add(e["id"], ids, "client")
            note = body.note.strip()
            c.execute("UPDATE events SET album_sent_at=?, album_note=CASE WHEN ?='' THEN album_note ELSE ? END WHERE id=?",
                      (now, note, note, e["id"]))
        return {**state(fresh(e["id"])), "added": len(ids)}

    @app.post("/c/{token}/album/remove")
    def client_remove(token: str, body: AlbumIds, request: Request):
        e = client_event(token, request)
        n = remove(e["id"], body.ids)
        return {**state(fresh(e["id"])), "removed": n}

    # ------------------------------------------------------------------ the studio (dashboard)
    @app.get("/api/events/{eid}/album")
    def album(eid: int, u: dict = Depends(h.user_dep)):
        return state(h.get_event(u, eid))

    @app.post("/api/events/{eid}/album/add")
    def studio_add(eid: int, body: StudioAdd, u: dict = Depends(h.can("events"))):
        e = h.get_event(u, eid)
        ids = photo_ids(e["id"], body.ids)
        if not ids:
            raise err(400, "Select ready photos to add to the album.", "empty")
        with db.tx():
            add(e["id"], ids, "studio", u["name"] or u["email"])
        return {**state(fresh(e["id"])), "added": len(ids)}

    @app.post("/api/events/{eid}/album/remove")
    def studio_remove(eid: int, body: AlbumIds, u: dict = Depends(h.can("events"))):
        e = h.get_event(u, eid)
        n = remove(e["id"], body.ids)
        return {**state(fresh(e["id"])), "removed": n}

    @app.post("/api/events/{eid}/album/lock")
    def lock(eid: int, body: LockIn, u: dict = Depends(h.can("events"))):
        e = h.get_event(u, eid)
        db.run("UPDATE events SET album_locked=? WHERE id=?", (int(body.locked), e["id"]))
        return state(fresh(e["id"]))

    @app.get("/api/events/{eid}/album.zip")
    def album_zip(eid: int, u: dict = Depends(h.user_dep)):
        e = h.get_event(u, eid)
        rows = picks(e["id"])
        if not rows:
            raise err(400, "No photos have been chosen for the album yet.", "empty")
        store = get_storage()
        files = [(store.photo_path(r, "orig"), name) for r, name in unique_names(rows)]
        fname = re.sub(r"[^A-Za-z0-9]+", "-", e["name"]).strip("-").lower() or "event"
        return StreamingResponse(zip_stream(files), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}-album-selection.zip"'})
