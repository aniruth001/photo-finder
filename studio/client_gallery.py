"""Client gallery: one private link per event that shows every photo, for the studio's client (the couple, the
company). Guests keep using selfie search on the guest link; this link needs no face at all.

The link works whether or not the event is published to guests, so the client can look before anyone else does.
It can carry a PIN. Without the PIN nothing about the event is shown beyond its name.
"""
import hashlib
import hmac
import io
import re
import secrets
import zipfile
from pathlib import Path

from fastapi import Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import branding, db, live, security
from .storage import delivery_name, get_storage

STATIC = Path(__file__).parent / "static"
PIN_RE = re.compile(r"^\d{4,8}$")
PAGE = 120


class PinIn(BaseModel):
    pin: str = Field("", max_length=8)  # "" removes the PIN


class UnlockIn(BaseModel):
    pin: str = Field(max_length=8)


def new_token() -> str:
    return secrets.token_urlsafe(16)  # 128 bits: as hard to guess as a session


def pass_cookie(e) -> str:
    """Cookie value proving the PIN was entered. Derived from the link and the PIN's salted hash, so it changes
    (and every unlocked browser is locked again) when either the link is reset or the PIN changes."""
    return hashlib.sha256(f"{e['client_token']}:{e['client_pin']}".encode()).hexdigest()


class _Sink(io.RawIOBase):
    """A write-only stream that hands its bytes to the zip generator below (no temp file, no seeking)."""

    def __init__(self):
        self.buf = bytearray()

    def writable(self):
        return True

    def write(self, b):
        self.buf += b
        return len(b)

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def zip_stream(files):
    """Yield a zip of [(path, name in zip)] as it is written, so a 20 GB wedding never sits on the server's disk."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as z:  # JPEGs don't compress further
        for path, name in files:
            try:
                src = open(path, "rb")
            except OSError:
                continue  # deleted since the list was made
            with src, z.open(name, "w", force_zip64=True) as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
                    yield sink.take()
            yield sink.take()
    yield sink.take()


def unique_names(rows):
    """(row, file name) with no name twice; a camera RAW file is named as the JPEG it is delivered as."""
    used = set()
    for r in rows:
        ext = r["ext"] if "ext" in r.keys() else Path(r["orig_name"]).suffix
        name = delivery_name(r["orig_name"], ext)
        if name.lower() in used:
            stem, dot, ext = name.rpartition(".")
            name = f"{stem or name}-{r['public_id'][:6]}{dot}{ext}" if dot else f"{name}-{r['public_id'][:6]}"
        used.add(name.lower())
        yield r, name


def mount_client(app, h):
    """h: helpers from server.create_app (can, err, public_base, client_ip, limiter, https, ink_for, get_event)."""
    err = h.err

    def client_event(token: str):
        r = db.q1("""SELECT e.*, a.company, a.brand_color, a.disabled_at, a.logo, a.logo_size, a.logo_badge,
                     a.logo_badge_light, a.theme FROM events e
                     JOIN accounts a ON a.id=e.account_id WHERE e.client_token=?""", (token,))
        if not r or not r["client_enabled"] or r["disabled_at"]:
            raise err(404, "This gallery isn't available.", "unavailable")
        return r

    def unlocked(request: Request, e) -> bool:
        if not e["client_pin"]:
            return True
        got = request.cookies.get(f"pf_c{e['id']}", "")
        return hmac.compare_digest(got, pass_cookie(e))

    def open_event(token: str, request: Request):
        e = client_event(token)
        if not unlocked(request, e):
            raise err(401, "Enter the PIN to see these photos.", "pin")
        return e

    app.state.client_open = open_event  # the client gallery's videos (studio/videos.py) use the same PIN check

    def photo(e, pid):
        r = db.q1("SELECT * FROM photos WHERE public_id=? AND event_id=? AND status='ready'", (pid, e["id"]))
        if not r:
            raise err(404, "Photo not found.", "not_found")
        return r

    def file_or_404(path, **kw):
        if not path.exists():  # deleted a moment ago
            raise err(404, "Photo not found.", "not_found")
        return FileResponse(path, **kw)

    # ------------------------------------------------------------------ the studio's side (dashboard)
    @app.post("/api/events/{eid}/client-link/reset")
    def reset_link(eid: int, request: Request, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        db.run("UPDATE events SET client_token=? WHERE id=? AND account_id=?", (new_token(), eid, u["account_id"]))
        return h.event_out(h.get_event(u, eid), request)

    @app.post("/api/events/{eid}/client-pin")
    def set_pin(eid: int, body: PinIn, request: Request, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        pin = body.pin.strip()
        if pin and not PIN_RE.match(pin):
            raise err(422, "The PIN must be 4 to 8 digits.", "pin")
        db.run("UPDATE events SET client_pin=? WHERE id=? AND account_id=?",
               (security.hash_password(pin) if pin else "", eid, u["account_id"]))
        return h.event_out(h.get_event(u, eid), request)

    # ------------------------------------------------------------------ the client's side (no login)
    @app.get("/c/{token}", response_class=HTMLResponse)
    def page(token: str, request: Request):
        try:
            e = client_event(token)
        except Exception:
            return HTMLResponse((STATIC / "unavailable.html").read_text(encoding="utf-8"), status_code=404)
        db.run("UPDATE events SET client_views=client_views+1 WHERE id=?", (e["id"],))
        live.publish_event(e["id"], "client")
        out = branding.render((STATIC / "client.html").read_text(encoding="utf-8"), e, h.ink_for(e["brand_color"]),
                              {"__TITLE__": e["name"], "__SUBTITLE__": e["subtitle"] or "", "__COMPANY__": e["company"]},
                              nonce=request.state.nonce)
        return HTMLResponse(out, headers={"Cache-Control": "no-store"})

    @app.get("/c/{token}/info")
    def info(token: str, request: Request):
        e = client_event(token)
        ok = unlocked(request, e)
        n = db.q1("SELECT COUNT(*) n FROM photos WHERE event_id=? AND status='ready'", (e["id"],))["n"] if ok else None
        busy = db.q1("SELECT COUNT(*) n FROM photos WHERE event_id=? AND status IN ('uploading','uploaded','processing')",
                     (e["id"],))["n"] if ok else None
        videos = db.q1("SELECT COUNT(*) n FROM videos WHERE event_id=?", (e["id"],))["n"] if ok else None
        return {"name": e["name"], "has_pin": bool(e["client_pin"]), "unlocked": ok, "photos": n, "processing": busy, "videos": videos,
                "allow_download": bool(e["client_download"]), "searchable": not e["faces_purged"]}

    @app.post("/c/{token}/unlock")
    def unlock(token: str, body: UnlockIn, request: Request):
        e = client_event(token)
        ip = h.client_ip(request)
        # per visitor and per gallery, so a 6-digit PIN can't be walked through from many addresses either
        if not h.limiter.allow(("pin", ip, token), 10, 900) or not h.limiter.allow(("pin-all", token), 60, 3600):
            raise err(429, "Too many tries. Wait 15 minutes and try again.", "rate")
        if not e["client_pin"] or not security.verify_password(body.pin.strip(), e["client_pin"]):
            raise err(403, "That PIN isn't right.", "pin")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(f"pf_c{e['id']}", pass_cookie(e), max_age=30 * 86400, httponly=True, samesite="lax",
                        secure=h.https(), path=f"/c/{token}")
        return resp

    @app.get("/c/{token}/photos")
    def photos(token: str, request: Request, offset: int = 0, limit: int = PAGE):
        e = open_event(token, request)
        limit = max(1, min(limit, 500))
        rows = db.q("""SELECT public_id, orig_name, width, height, edited FROM photos WHERE event_id=? AND status='ready'
                       ORDER BY orig_name COLLATE NOCASE, id LIMIT ? OFFSET ?""", (e["id"], limit, max(0, offset)))
        return [{"id": r["public_id"], "name": r["orig_name"], "w": r["width"], "h": r["height"], "edited": bool(r["edited"])} for r in rows]

    @app.post("/c/{token}/search")
    def search(token: str, request: Request, file: UploadFile = File(...)):
        """Filter the gallery to one person: the photos the face in this selfie (or any photo of them) is in."""
        e = open_event(token, request)
        ip = h.client_ip(request)
        if not h.limiter.allow(("csearch", ip, token), 30, 600) or not h.limiter.allow(("search-ip", ip), 80, 600):
            raise err(429, "Too many tries. Please wait a few minutes.", "rate")
        rows, multiple = h.match_selfie(e, file)  # not counted in the guest statistics
        return {"count": len(rows), "multiple_faces": multiple,
                "photos": [{"id": r["public_id"], "name": r["orig_name"], "w": r["width"], "h": r["height"],
                            "edited": bool(r["edited"])} for r in rows]}

    @app.get("/c/{token}/thumb/{pid}.jpg")
    def thumb(token: str, pid: str, request: Request):
        r = photo(open_event(token, request), pid)
        return file_or_404(get_storage().photo_path(r, "thumb"), headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/c/{token}/web/{pid}.jpg")
    def web(token: str, pid: str, request: Request):
        r = photo(open_event(token, request), pid)
        return file_or_404(get_storage().photo_path(r, "web"), headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/c/{token}/download/{pid}")
    def download(token: str, pid: str, request: Request):
        e = open_event(token, request)
        if not e["client_download"]:
            raise err(403, "Downloads are turned off for this gallery.", "no_download")
        r = photo(e, pid)
        return file_or_404(get_storage().delivery_path(r), filename=delivery_name(r["orig_name"], r["ext"]),
                           content_disposition_type="attachment")

    @app.post("/c/{token}/zip")
    def zip_all(token: str, request: Request, ids: str = Form("")):
        e = open_event(token, request)
        if not e["client_download"]:
            raise err(403, "Downloads are turned off for this gallery.", "no_download")
        if not h.limiter.allow(("czip", h.client_ip(request), token), 6, 3600):
            raise err(429, "Too many downloads. Please wait a while.", "rate")
        want = [p for p in ids.split(",") if p][:5000]
        if want:  # a selection
            rows = []
            for i in range(0, len(want), 500):
                part = want[i:i + 500]
                rows += db.q(f"SELECT * FROM photos WHERE status='ready' AND event_id=? AND public_id IN "
                             f"({','.join('?' * len(part))}) ORDER BY orig_name COLLATE NOCASE", [e["id"]] + part)
        else:  # everything
            rows = db.q("SELECT * FROM photos WHERE status='ready' AND event_id=? ORDER BY orig_name COLLATE NOCASE",
                        (e["id"],))
        if not rows:
            raise err(400, "No photos to download.", "empty")
        store = get_storage()
        files = [(store.delivery_path(r), name) for r, name in unique_names(rows)]
        fname = re.sub(r"[^A-Za-z0-9]+", "-", e["name"]).strip("-").lower() or "photos"
        return StreamingResponse(zip_stream(files), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}-all-photos.zip"'})

