"""Landing-page story: a short story of the couple (a hero, chapters of photos with captions, a thank-you note) shown
on the guest page after the PIN, ending with the selfie search, and on the client gallery before the photos.
See docs/landing-page-playbook.md for how a story is made.

One story per event. The studio edits a draft and publishes it; guests and the client only ever see the published
copy, and only where the studio switched it on. Photos are referred to by public_id. On the guest page a story photo
is served only if the published story names it, so the story never makes any other photo of the event reachable.
A photo deleted after publishing simply drops out of the story.
"""
import json
import time
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import branding, db
from .storage import get_storage

STATIC = Path(__file__).parent / "static"
FOCUS = r"^$|^(100|\d{1,2})% (100|\d{1,2})%$"   # crop position: "50% 30%" keeps the top-centre in view
MAX_PHOTOS = 300


class _M(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


class Shot(_M):
    file: str = Field(min_length=1, max_length=255)   # a public_id; an original file name is accepted and converted
    caption: str = Field("", max_length=300)
    focus: str = Field("", pattern=FOCUS)


class Item(_M):
    file: Optional[str] = Field(None, min_length=1, max_length=255)
    pair: Optional[List[Shot]] = Field(None, min_length=2, max_length=2)
    caption: str = Field("", max_length=300)
    focus: str = Field("", pattern=FOCUS)

    @model_validator(mode="after")
    def one_kind(self):
        if bool(self.file) == bool(self.pair):
            raise ValueError("each item is either one photo (file) or a pair")
        return self


class Chapter(_M):
    label: str = Field("", max_length=60)
    title: str = Field("", max_length=120)
    items: List[Item] = Field(default_factory=list, max_length=80)


class Story(_M):
    title: str = Field("", max_length=120)
    intro: str = Field("", max_length=1000)
    cover: str = Field("", max_length=255)
    chapters: List[Chapter] = Field(default_factory=list, max_length=30)
    thanks: str = Field("", max_length=3000)
    cta: str = Field("", max_length=300)
    signoff: str = Field("", max_length=120)

    @model_validator(mode="after")
    def not_too_big(self):
        if len(refs(self.model_dump())) > MAX_PHOTOS:
            raise ValueError(f"a story can have at most {MAX_PHOTOS} photos")
        return self


class StoryIn(BaseModel):
    story: Story


class ShowIn(BaseModel):
    for_guests: Optional[bool] = None
    for_client: Optional[bool] = None


def refs(s: dict) -> List[str]:
    """Every photo a story names, cover first, in story order."""
    out = [s["cover"]] if s.get("cover") else []
    for ch in s.get("chapters") or []:
        for it in ch.get("items") or []:
            out += [p["file"] for p in it["pair"]] if it.get("pair") else [it["file"]]
    return out


def ready_photos(event_id: int, pids) -> dict:
    """{public_id: row} of this event's ready photos among pids."""
    out, want = {}, list(dict.fromkeys(p for p in pids if p))
    for i in range(0, len(want), 500):
        part = want[i:i + 500]
        for r in db.q(f"SELECT id, public_id, orig_name, width, height FROM photos WHERE event_id=? AND status='ready' "
                      f"AND public_id IN ({','.join('?' * len(part))})", [event_id] + part):
            out[r["public_id"]] = r
    return out


def resolve(event_id: int, s: dict):
    """Turn file names into public_ids (a story written by hand or by Claude may use the camera's file names).
    Returns (story, the references that match no ready photo of the event)."""
    known = ready_photos(event_id, refs(s))
    names = {}
    rest = [r for r in refs(s) if r not in known]
    if rest:
        for i in range(0, len(rest), 500):
            part = [r.lower() for r in rest[i:i + 500]]
            for r in db.q(f"SELECT public_id, orig_name FROM photos WHERE event_id=? AND status='ready' AND lower(orig_name) "
                          f"IN ({','.join('?' * len(part))}) ORDER BY id", [event_id] + part):
                names.setdefault(r["orig_name"].lower(), r["public_id"])
    unknown = []

    def fix(ref):
        if ref in known:
            return ref
        pid = names.get(ref.lower())
        if not pid:
            unknown.append(ref)
        return pid or ref

    if s.get("cover"):
        s["cover"] = fix(s["cover"])
    for ch in s["chapters"]:
        for it in ch["items"]:
            if it.get("pair"):
                for p in it["pair"]:
                    p["file"] = fix(p["file"])
            else:
                it["file"] = fix(it["file"])
    return s, unknown


def compact(s: dict) -> dict:
    """The story without empty optional fields, as stored and as sent."""
    def item(it):
        if it.get("pair"):
            o = {"pair": [{k: v for k, v in p.items() if v} for p in it["pair"]]}
        else:
            o = {"file": it["file"]}
            if it.get("focus"):
                o["focus"] = it["focus"]
        if it.get("caption"):
            o["caption"] = it["caption"]
        return o
    return {**{k: s[k] for k in ("title", "intro", "cover", "thanks", "cta", "signoff") if s.get(k)},
            "chapters": [{**{k: ch[k] for k in ("label", "title") if ch.get(k)}, "items": [item(i) for i in ch["items"]]}
                         for ch in s.get("chapters") or []]}


def for_viewers(event_id: int, raw: str):
    """The published story as guests and the client get it: photos that are gone are left out (a pair missing one
    photo becomes a single photo). Returns (story, {public_id: {w, h}})."""
    s = json.loads(raw)
    rows = ready_photos(event_id, refs(s))
    if s.get("cover") not in rows:
        s.pop("cover", None)
    for ch in s["chapters"]:
        items = []
        for it in ch["items"]:
            if it.get("pair"):
                left = [p for p in it["pair"] if p["file"] in rows]
                if len(left) == 2:
                    items.append(it)
                elif left:
                    one = {"file": left[0]["file"], "caption": left[0].get("caption") or it.get("caption", "")}
                    if left[0].get("focus"):
                        one["focus"] = left[0]["focus"]
                    items.append(one)
            elif it["file"] in rows:
                items.append(it)
        ch["items"] = items
    return s, {pid: {"w": r["width"], "h": r["height"]} for pid, r in rows.items()}


def mount_story(app, h):
    """h: helpers from server.create_app (user_dep, can, err, get_event, guest_open, client_open)."""
    err = h.err

    def row(eid):
        return db.q1("SELECT * FROM event_stories WHERE event_id=?", (eid,))

    def state(eid: int) -> dict:
        r = row(eid)
        if not r:
            return {"exists": False, "draft": None, "published": None, "changed": False, "for_guests": True,
                    "for_client": True, "photos": {}, "missing": []}
        draft = json.loads(r["draft"]) if r["draft"] else None
        pub = json.loads(r["published"]) if r["published"] else None
        names = refs(draft or {}) + refs(pub or {})
        rows = ready_photos(eid, names)
        return {"exists": True, "draft": draft, "published": pub, "changed": r["draft"] != r["published"],
                "for_guests": bool(r["for_guests"]), "for_client": bool(r["for_client"]),
                "updated_at": r["updated_at"], "updated_by": r["updated_by"], "published_at": r["published_at"],
                "photos": {p: {"name": x["orig_name"], "w": x["width"], "h": x["height"]} for p, x in rows.items()},
                "missing": sorted({p for p in names if p not in rows})}

    # ------------------------------------------------------------------ the studio's side
    @app.get("/api/events/{eid}/story")
    def get_story(eid: int, u: dict = Depends(h.user_dep)):
        h.get_event(u, eid)
        return state(eid)

    @app.put("/api/events/{eid}/story")
    def save_story(eid: int, body: StoryIn, u: dict = Depends(h.can("events"))):
        """Save the draft. Guests and the client don't see it until it is published."""
        h.get_event(u, eid)
        s, unknown = resolve(eid, compact(body.story.model_dump()))
        if unknown:
            shown = ", ".join(unknown[:4]) + (f" and {len(unknown) - 4} more" if len(unknown) > 4 else "")
            raise err(422, f"These photos aren't in this event (or aren't ready yet): {shown}.", "photos")
        now = int(time.time())
        db.run("""INSERT INTO event_stories(event_id, draft, updated_at, updated_by) VALUES(?,?,?,?)
                  ON CONFLICT(event_id) DO UPDATE SET draft=excluded.draft, updated_at=excluded.updated_at,
                  updated_by=excluded.updated_by""", (eid, json.dumps(s, ensure_ascii=False), now, u["name"] or u["email"]))
        return state(eid)

    @app.post("/api/events/{eid}/story/publish")
    def publish(eid: int, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        r = row(eid)
        if not r or not r["draft"]:
            raise err(400, "Make the story first.", "empty")
        if not refs(json.loads(r["draft"])) and not json.loads(r["draft"]).get("title"):
            raise err(400, "Add a title or some photos before publishing.", "empty")
        db.run("UPDATE event_stories SET published=draft, published_at=? WHERE event_id=?", (int(time.time()), eid))
        return state(eid)

    @app.post("/api/events/{eid}/story/unpublish")
    def unpublish(eid: int, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        db.run("UPDATE event_stories SET published='', published_at=NULL WHERE event_id=?", (eid,))
        return state(eid)

    @app.patch("/api/events/{eid}/story")
    def show(eid: int, body: ShowIn, u: dict = Depends(h.can("events"))):
        """Where the published story appears: the guest page, the client gallery, or both."""
        h.get_event(u, eid)
        db.run("INSERT OR IGNORE INTO event_stories(event_id, updated_at) VALUES(?,?)", (eid, int(time.time())))
        for k in ("for_guests", "for_client"):
            v = getattr(body, k)
            if v is not None:
                db.run(f"UPDATE event_stories SET {k}=? WHERE event_id=?", (int(v), eid))
        return state(eid)

    @app.delete("/api/events/{eid}/story")
    def delete(eid: int, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        db.run("DELETE FROM event_stories WHERE event_id=?", (eid,))
        return state(eid)

    @app.get("/api/events/{eid}/story/preview", response_class=HTMLResponse)
    def preview(eid: int, request: Request, page: str = "guest", u: dict = Depends(h.user_dep)):
        """The story as guests (or the client) will see it, with the draft. Shown inside the dashboard, so this page,
        unlike every other one, may be framed by the studio's own pages."""
        e = h.get_event(u, eid)
        a = db.q1("SELECT company, brand_color, logo, logo_size, logo_badge, logo_badge_light, theme FROM accounts WHERE id=?",
                  (u["account_id"],))
        nonce = request.state.nonce
        out = branding.render((STATIC / "story_preview.html").read_text(encoding="utf-8"), {**dict(e), **dict(a)},
                              h.ink_for(a["brand_color"]), {"__TITLE__": e["name"], "__COMPANY__": a["company"],
                                                            "__PAGE__": "client" if page == "client" else "guest",
                                                            "__EID__": str(eid)}, nonce=nonce)
        csp = (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
               "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'self'")
        return HTMLResponse(out, headers={"Cache-Control": "no-store", "X-Frame-Options": "SAMEORIGIN",
                                          "Content-Security-Policy": csp})

    # ------------------------------------------------------------------ guests and the client
    def published(e, key):
        r = row(e["id"])
        if not r or not r["published"] or not r[key]:
            raise err(404, "No story for this gallery.", "no_story")
        return r

    @app.get("/e/{slug}/story")
    def guest_story(slug: str, request: Request):
        e = h.guest_open(slug, request)
        s, dims = for_viewers(e["id"], published(e, "for_guests")["published"])
        return {"story": s, "photos": dims}

    @app.get("/e/{slug}/story/{pid}.jpg")
    def guest_story_photo(slug: str, pid: str, request: Request):
        """A photo the published story names, and only those: the rest of the event stays behind the selfie search."""
        e = h.guest_open(slug, request)
        if pid not in refs(json.loads(published(e, "for_guests")["published"])):
            raise err(404, "Photo not found.", "not_found")
        r = db.q1("SELECT * FROM photos WHERE public_id=? AND event_id=? AND status='ready'", (pid, e["id"]))
        path = get_storage().photo_path(r, "web") if r else None
        if not path or not path.exists():
            raise err(404, "Photo not found.", "not_found")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/c/{token}/story")
    def client_story(token: str, request: Request):
        e = h.client_open(token, request)
        s, dims = for_viewers(e["id"], published(e, "for_client")["published"])
        return {"story": s, "photos": dims}

