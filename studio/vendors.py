"""Vendor uploads: photos and videos sent to an event by people outside the studio, without an account.

A second shooter, the drone team or the video editor gets an upload link for one event (/v/<link>). On that page they
send photos and videos; each file lands in the event like the studio's own uploads (photos are processed for guest
search, videos go to the client gallery) and is tagged with the vendor's name, so the event page can show who sent
what. The page shows the vendor only the names of what they sent through their link, never the event's other photos.
The studio turns a link off (it stops working at once) or deletes it (what they sent stays in the event). Uploads
count towards the studio's plan, like its own.
"""
import secrets
import time
from pathlib import Path
from typing import List

from fastapi import Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import branding, config, db
from .ingest import add_photo, photo_usage
from .videos import add_video

STATIC = Path(__file__).parent / "static"


class VendorIn(BaseModel):
    name: str = Field(max_length=120)
    phone: str = Field("", max_length=40)


class VendorPatch(BaseModel):
    enabled: bool


def mount_vendors(app, h):
    """h: helpers from server.create_app (user_dep, can, err, get_event, plan_of, poke, client_ip, limiter, public_base, ink_for)."""
    err = h.err

    def counts(vid) -> dict:
        p = db.q1("""SELECT COUNT(*) n, COALESCE(SUM(status='ready'),0) ready, COALESCE(SUM(status='failed'),0) failed,
                     COALESCE(SUM(size),0) size FROM photos WHERE vendor_id=?""", (vid,))
        v = db.q1("SELECT COUNT(*) n, COALESCE(SUM(size),0) size FROM videos WHERE vendor_id=?", (vid,))
        return {"photos": p["n"], "ready": p["ready"], "failed": p["failed"], "videos": v["n"], "size": p["size"] + v["size"]}

    def out(r, request) -> dict:
        return {"id": r["id"], "name": r["name"], "phone": r["phone"], "enabled": bool(r["enabled"]),
                "url": f"{h.public_base(request)}/v/{r['token']}", "created_at": r["created_at"],
                "last_upload_at": r["last_upload_at"], **counts(r["id"])}

    def get_link(u, eid, lid):
        r = db.q1("SELECT * FROM vendor_links WHERE id=? AND event_id=? AND account_id=?", (lid, eid, u["account_id"]))
        if not r:
            raise err(404, "Vendor link not found.", "not_found")
        return r

    # ------------------------------------------------------------------ the studio (dashboard)
    @app.get("/api/events/{eid}/vendors")
    def list_links(eid: int, request: Request, u: dict = Depends(h.user_dep)):
        h.get_event(u, eid)
        return [out(r, request) for r in db.q("SELECT * FROM vendor_links WHERE event_id=? AND account_id=? ORDER BY id", (eid, u["account_id"]))]

    @app.post("/api/events/{eid}/vendors")
    def create_link(eid: int, body: VendorIn, request: Request, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        name = body.name.strip()
        if not name:
            raise err(422, "Add the vendor's name (for example \"Drone team\" or \"Ravi, second shooter\").", "name")
        if db.q1("SELECT COUNT(*) n FROM vendor_links WHERE event_id=?", (eid,))["n"] >= 50:
            raise err(422, "An event can have at most 50 vendor links.", "too_many")
        lid = db.ex("""INSERT INTO vendor_links(account_id, event_id, name, phone, token, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?)""", (u["account_id"], eid, name, body.phone.strip(), secrets.token_urlsafe(16),
                                                  u["user_id"], int(time.time())))
        return out(get_link(u, eid, lid), request)

    @app.patch("/api/events/{eid}/vendors/{lid}")
    def switch_link(eid: int, lid: int, body: VendorPatch, request: Request, u: dict = Depends(h.can("events"))):
        get_link(u, eid, lid)
        db.run("UPDATE vendor_links SET enabled=? WHERE id=?", (int(body.enabled), lid))
        return out(get_link(u, eid, lid), request)

    @app.post("/api/events/{eid}/vendors/{lid}/reset")
    def reset_link(eid: int, lid: int, request: Request, u: dict = Depends(h.can("events"))):
        """A new address; the old one stops working (a link sent to the wrong person)."""
        get_link(u, eid, lid)
        db.run("UPDATE vendor_links SET token=? WHERE id=?", (secrets.token_urlsafe(16), lid))
        return out(get_link(u, eid, lid), request)

    @app.delete("/api/events/{eid}/vendors/{lid}")
    def delete_link(eid: int, lid: int, u: dict = Depends(h.can("events"))):
        get_link(u, eid, lid)
        db.run("DELETE FROM vendor_links WHERE id=?", (lid,))  # their photos and videos stay, untagged
        return {"ok": True}

    # ------------------------------------------------------------------ the vendor (no login)
    def vendor_link(token: str):
        r = db.q1("""SELECT l.*, e.name AS event_name, e.event_date, a.company, a.brand_color, a.disabled_at, a.plan, a.logo,
                     a.logo_size, a.logo_badge, a.logo_badge_light, a.theme FROM vendor_links l
                     JOIN events e ON e.id=l.event_id JOIN accounts a ON a.id=l.account_id WHERE l.token=?""", (token,))
        if not r or not r["enabled"] or r["disabled_at"]:
            raise err(404, "This upload link isn't available.", "unavailable")
        return r

    def uploading(token: str, request: Request, n: int):
        if request.headers.get("x-pf") != "1":
            raise err(403, "Missing X-PF header.", "csrf")  # a plain cross-site form can't fill the studio's storage
        r = vendor_link(token)
        if not h.limiter.allow(("vendor", token), 2000, 3600) or not h.limiter.allow(("vendor-ip", h.client_ip(request)), 2000, 3600):
            raise err(429, "Too many uploads at once. Wait a few minutes and try again.", "rate")
        if n > config.settings.max_files_per_request:
            raise err(413, f"Send at most {config.settings.max_files_per_request} files per request.", "too_many")
        return r

    def sent(r):
        rows = db.q("""SELECT orig_name, status, created_at, 'photo' AS kind FROM photos WHERE vendor_id=?
                       UNION ALL SELECT orig_name, 'ready', created_at, 'video' FROM videos WHERE vendor_id=?
                       ORDER BY created_at DESC LIMIT 500""", (r["id"], r["id"]))
        return [{"name": x["orig_name"], "kind": x["kind"], "status": "failed" if x["status"] == "failed" else "received"} for x in rows]

    @app.get("/v/{token}", response_class=HTMLResponse)
    def page(token: str):
        try:
            r = vendor_link(token)
        except Exception:
            return HTMLResponse((STATIC / "unavailable.html").read_text(encoding="utf-8"), status_code=404)
        html = branding.render((STATIC / "vendor.html").read_text(encoding="utf-8"), r, h.ink_for(r["brand_color"]),
                               {"__TITLE__": r["event_name"], "__COMPANY__": r["company"], "__VENDOR__": r["name"]})
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/v/{token}/info")
    def info(token: str):
        r = vendor_link(token)
        c = counts(r["id"])
        return {"event": r["event_name"], "event_date": r["event_date"], "company": r["company"], "vendor": r["name"],
                "photos": c["photos"], "videos": c["videos"], "sent": sent(r),
                "max_file_mb": config.settings.max_file_mb, "max_video_mb": config.settings.max_video_mb,
                "per_request": config.settings.max_files_per_request}

    @app.post("/v/{token}/photos")
    def upload_photos(token: str, request: Request, files: List[UploadFile] = File(...)):
        r = uploading(token, request, len(files))
        plan, used = h.plan_of(r["plan"]), photo_usage(r["account_id"])
        results = [add_photo(r["account_id"], r["event_id"], plan, f.filename, f.file, used, vendor_id=r["id"]) for f in files]
        queued = sum(x["status"] == "queued" for x in results)
        if queued:
            db.run("UPDATE vendor_links SET last_upload_at=? WHERE id=?", (int(time.time()), r["id"]))
            h.poke()
        return {"results": [{k: x[k] for k in ("name", "status", "error")} for x in results], "queued": queued}

    @app.post("/v/{token}/videos")
    def upload_videos(token: str, request: Request, files: List[UploadFile] = File(...)):
        r = uploading(token, request, len(files))
        if len(files) > 5:
            raise err(413, "Send at most 5 videos per request.", "too_many")
        plan = h.plan_of(r["plan"])
        results = [add_video(r["account_id"], r["event_id"], plan, f.filename, f.file, vendor_id=r["id"]) for f in files]
        added = sum(x["status"] == "queued" for x in results)
        if added:
            db.run("UPDATE vendor_links SET last_upload_at=? WHERE id=?", (int(time.time()), r["id"]))
        return {"results": [{k: x[k] for k in ("name", "status", "error")} for x in results], "added": added}
