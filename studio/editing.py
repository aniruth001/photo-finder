"""Photo editing: the studio opens a photo in the editor (studio/static/editor.html), edits it in the browser and saves
it back to the event.

An original upload is never overwritten. The first save makes an edited copy in the same event; saving again updates
that copy in place. A copy made from a copy still points at the original it came from. Edited copies are photos of the
event like the others, tagged Edited wherever they are shown: on the dashboard (with the photo they came from), in the
client gallery and, when a guest's selfie matches them, on the guest page. They can go in the album.

The editor gets the photo through editor/source: the original, the right way up (EXIF), as JPEG whatever it was uploaded
as (HEIC too), at most MAX_SIDE pixels on the long side, which is what the editor can hold in memory comfortably.
"""
import hashlib
import io
import os
import sqlite3
import time
from pathlib import Path

from fastapi import Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from . import branding, config, db
from .ingest import add_photo, photo_usage
from .storage import get_storage

STATIC = Path(__file__).parent / "static"
MAX_SIDE = 6000        # the editor's longest side: a 24-megapixel photo at full size
JPEG = b"\xff\xd8\xff"


def mount_editing(app, h):
    """h: helpers from server.create_app (user_dep, can, err, get_event, plan_of, poke)."""
    err = h.err

    def photo(u, eid: int, pid: str):
        h.get_event(u, eid)
        r = db.q1("""SELECT p.*, src.public_id AS source_id, src.orig_name AS source_name FROM photos p
                     LEFT JOIN photos src ON src.id=p.edited_of WHERE p.public_id=? AND p.event_id=? AND p.account_id=?""",
                  (pid, eid, u["account_id"]))
        if not r:
            raise err(404, "Photo not found.", "not_found")
        return r

    def out(r) -> dict:
        return {"id": r["public_id"], "name": r["orig_name"], "size": r["size"], "status": r["status"], "width": r["width"],
                "height": r["height"], "edited": bool(r["edited"]),
                "source": {"id": r["source_id"], "name": r["source_name"]} if r["source_id"] else None, "v": r["edited_at"] or 0}

    def edited_jpeg(file: UploadFile) -> bytes:
        """What the editor sends: a JPEG that opens, within the upload limit."""
        limit = config.settings.max_file_mb * 1024 * 1024
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise err(413, f"The edited photo is larger than {config.settings.max_file_mb} MB.", "too_large")
        if not data.startswith(JPEG):
            raise err(422, "Send the edited photo as a JPEG.", "not_jpeg")
        from PIL import Image

        try:
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
        except Exception:
            raise err(422, "The edited photo couldn't be read. Try saving again.", "bad_image")
        return data

    # ------------------------------------------------------------------ the editor page (its API calls need a login)
    @app.get("/editor", response_class=HTMLResponse)
    def editor(request: Request):
        page = branding.with_nonce((STATIC / "editor.html").read_text(encoding="utf-8"), request.state.nonce)
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/api/events/{eid}/photos/{pid}/editor/info")  # not .../{pid}/{kind}: that route serves thumb/web/orig
    def info(eid: int, pid: str, u: dict = Depends(h.user_dep)):
        e = h.get_event(u, eid)
        return {**out(photo(u, eid, pid)), "event": {"id": e["id"], "name": e["name"]}}

    @app.get("/api/events/{eid}/photos/{pid}/editor/source")
    def source(eid: int, pid: str, u: dict = Depends(h.user_dep)):
        r = photo(u, eid, pid)
        if r["status"] != "ready":
            raise err(409, "This photo is still being processed. Try again in a moment.", "not_ready")
        path = get_storage().photo_path(r, "orig")
        if not path.exists():
            raise err(404, "Photo not found.", "not_found")
        from faces import open_image  # HEIC too, turned the right way up

        try:
            im = open_image(path)
        except Exception:
            raise err(422, "This photo couldn't be opened for editing.", "bad_image")
        im.thumbnail((MAX_SIDE, MAX_SIDE))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=95, subsampling=0)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ saving
    @app.post("/api/events/{eid}/photos/{pid}/edits")
    def save_copy(eid: int, pid: str, file: UploadFile = File(...), u: dict = Depends(h.can("upload"))):
        """A new edited copy of this photo, in the same event."""
        r = photo(u, eid, pid)
        data = edited_jpeg(file)
        root = r["edited_of"] if r["edited"] else r["id"]          # a copy of a copy points at the original
        base = r["source_name"] if r["edited"] and r["source_name"] else r["orig_name"]
        name = f"{Path(base).stem.removesuffix(' (edited)')} (edited).jpg"
        res = add_photo(u["account_id"], eid, h.plan_of(u["plan"]), name, io.BytesIO(data), edited=True, edited_of=root)
        if res["status"] == "duplicate":
            raise err(409, "This exact picture is already in the event.", "duplicate")
        if res["status"] != "queued":
            raise err(422, res["error"] or "The copy couldn't be saved.", "rejected")
        h.poke()
        return out(photo(u, eid, res["id"]))

    @app.put("/api/events/{eid}/photos/{pid}/edit")
    def update(eid: int, pid: str, file: UploadFile = File(...), u: dict = Depends(h.can("upload"))):
        """Save over an edited copy. Originals are never overwritten: those are saved as a copy first."""
        r = photo(u, eid, pid)
        if not r["edited"]:
            raise err(409, "Original uploads are never changed. Save your edit as a copy first.", "original")
        if r["status"] in ("uploading", "uploaded", "processing"):
            raise err(409, "The last save is still being processed. Try again in a moment.", "busy")
        data = edited_jpeg(file)
        plan = h.plan_of(u["plan"])
        if plan["storage"] is not None and photo_usage(u["account_id"])["storage"] - r["size"] + len(data) > plan["storage"]:
            raise err(422, f"Storage limit of the {plan['label']} plan reached.", "plan_limit")
        final = get_storage().photo_path(r, "orig")
        part = final.with_name(final.name + ".part")
        part.write_bytes(data)
        try:
            with db.tx():
                db.run("""UPDATE photos SET size=?, sha256=?, status='uploaded', error=NULL, edited_at=? WHERE id=?""",
                       (len(data), hashlib.sha256(data).hexdigest(), int(time.time()), r["id"]))
                os.replace(part, final)  # inside the transaction: the row and the file change together
        except sqlite3.IntegrityError:
            part.unlink(missing_ok=True)
            raise err(409, "This exact picture is already in the event.", "duplicate")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        h.poke()  # new thumbnail and web copy
        return out(photo(u, eid, pid))
