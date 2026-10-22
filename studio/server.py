"""Photo Finder Studio: the web app.

Photography companies sign in, add clients, create events, upload photos and share one link per event.
Anyone with the link takes a selfie and sees only the photos they appear in.

Every dashboard query is scoped by account_id: a company can only ever see its own rows.
"""
import hashlib
import hmac
import html
import io
import ipaddress
import re
import secrets
import sys
import sqlite3
import shutil
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from faces import LIMITS, PRESETS

from . import branding, config, db, google, mailer, roles, security
from .processing import Processor, VectorIndex, switch_model, upgrade_events
from .storage import get_storage
from .camera import CameraServer, mount_camera
from .client_gallery import PIN_RE, PinIn, UnlockIn, mount_client, new_token, unique_names, zip_stream
from .estimates import mount_estimates
from .ingest import add_photo, photo_usage, remove_leftovers
from .team import mount_team, seats_used
from .videos import mount_videos
from .inventory import mount_inventory
from .bookings import mount_bookings
from .tasks import mount_tasks
from .scenes import SceneIndexer, mount_scenes
from .clips import mount_clips
from .editing import mount_editing
from .album import mount_album
from .vendors import mount_vendors

STATIC = Path(__file__).parent / "static"
COOKIE = "pf_session"
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SLUG_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
BUSY = ("uploading", "uploaded", "processing")
LEVEL_LABELS = {"strict": "Strict (fewest wrong photos)", "balanced": "Balanced",
                "relaxed": "Relaxed (finds more, some wrong)"}
SUSPENDED = "This studio account is suspended. Contact support to restore access."


# ---------------------------------------------------------------------------------------------- small helpers
def fix_proactor_resets():
    """Windows, Python before 3.11 (Python issue 39010): when a phone or browser drops a connection, asyncio's
    Proactor transport calls shutdown() on the dead socket, which raises "ConnectionResetError: [WinError 10054]".
    That prints a traceback and, worse, skips the rest of the clean-up: the socket is never closed and the server
    keeps counting the connection (so Ctrl+C waits for it). Do the same clean-up with that one error ignored."""
    if sys.platform != "win32" or sys.version_info >= (3, 11):
        return
    import socket
    from asyncio import proactor_events as pe
    cls = pe._ProactorBasePipeTransport
    if getattr(cls, "_pf_fixed", False):
        return

    def _call_connection_lost(self, exc):
        if self._sock is None:
            return
        try:
            self._protocol.connection_lost(exc)
        finally:
            if hasattr(self._sock, "shutdown"):
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:  # the other side already reset it: nothing left to shut down
                    pass
            self._sock.close()
            self._sock = None
            server = self._server
            if server is not None:
                server._detach()
                self._server = None
    cls._call_connection_lost = _call_connection_lost
    cls._pf_fixed = True


def ink_for(color: str) -> str:
    """Readable text colour (dark or white) on top of a brand colour."""
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#1b1406" if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.55 else "#ffffff"


def make_slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:28].strip("-") or "event"
    return f"{base}-{''.join(secrets.choice(SLUG_ALPHABET) for _ in range(8))}"


def _private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return a.is_loopback or a.is_private or a.is_link_local


def client_ip(request: Request) -> str:
    """The visitor's address, for rate limits. Behind a reverse proxy every request comes from the proxy, so its
    X-Forwarded-For is read: the last address in it that isn't one of our own proxies (the first one can be typed
    by anyone, so it is never trusted)."""
    peer = request.client.host if request.client else "?"
    mode = config.settings.trust_proxy
    if mode in ("0", "false", "no", "off") or (mode not in ("1", "true", "yes", "on") and not _private(peer)):
        return peer
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    for h in reversed(hops):
        if not _private(h):
            return h
    return hops[0] if hops else peer


def public_base(request: Request) -> str:
    return config.settings.public_url or str(request.base_url).rstrip("/")


def email_base(request: Request) -> str:
    """The address for links in emails (password reset, invitations). Never the request's Host header, which the
    sender of the request chooses: with no public URL set, the address this server is actually listening on."""
    if config.settings.public_url:
        return config.settings.public_url
    host, port = (request.scope.get("server") or ("localhost", 80))[:2]
    scheme = request.scope.get("scheme", "http")
    if ":" in str(host):
        host = f"[{host}]"  # IPv6
    return f"{scheme}://{host}" + ("" if (scheme, port) in (("http", 80), ("https", 443)) else f":{port}")


def csp(nonce: str) -> str:
    """Scripts run only if they carry this response's nonce (branding.with_nonce puts it on the pages' own), never
    because they are inline; inline event handlers and eval are blocked too. Everything else (frames, plugins, form
    targets, where a page may connect or load from) is limited to this site. Inline style attributes stay allowed."""
    return (f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'")


def err(status: int, msg: str, code: str = "") -> HTTPException:
    return HTTPException(status, {"message": msg, "code": code})


def valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except Exception:  # unknown, malformed, or no tz database installed (pip install tzdata on Windows)
        return False


def today_in(tz: str) -> str:
    """Today's date where the studio is (ISO); the server's own date if the zone is unset or unknown."""
    if tz:
        try:
            return datetime.now(ZoneInfo(tz)).date().isoformat()
        except Exception:
            pass
    return date.today().isoformat()


def mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


def email_header(company: str, logo_url: str) -> str:
    """Top of an HTML email: the company logo if there is one (alt text = the name), else nothing."""
    if not logo_url:
        return ""
    e = html.escape(company, quote=True)
    return f'<p style="margin:0 0 18px"><img src="{html.escape(logo_url, quote=True)}" alt="{e}" style="max-height:48px;max-width:240px"></p>'


def reset_email(name: str, company: str, link: str, minutes: int, logo_url: str = ""):
    """(plain text, html) of the password-reset message."""
    hi = f"Hi {name}," if name else "Hi,"
    text = (f"{hi}\n\nSomeone (hopefully you) asked to reset the password for {company} on Photo Finder Studio.\n\n"
            f"Choose a new password here (the link works for {minutes} minutes, once):\n{link}\n\n"
            "If you didn't ask for this, ignore this email. Your password stays the same.\n")
    e = lambda v: html.escape(v, quote=True)
    page = f"""<div style="font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;max-width:480px;margin:0 auto;color:#1d1b16">
  {email_header(company, logo_url)}<h2 style="font-size:20px;margin:0 0 16px">Reset your password</h2>
  <p>{e(hi)}</p>
  <p>Someone (hopefully you) asked to reset the password for <b>{e(company)}</b> on Photo Finder Studio.</p>
  <p style="margin:24px 0"><a href="{e(link)}" style="background:#1d1b16;color:#fff;padding:12px 20px;border-radius:9px;text-decoration:none;font-weight:600">Choose a new password</a></p>
  <p style="color:#6f6a5d;font-size:14px">The link works once, for {minutes} minutes. If the button doesn't work, paste this into your browser:<br><span style="word-break:break-all">{e(link)}</span></p>
  <p style="color:#6f6a5d;font-size:14px">If you didn't ask for this, ignore this email. Your password stays the same.</p></div>"""
    return text, page


def plan_of(account_plan: str) -> dict:
    return config.PLANS.get(account_plan, config.PLANS["free"])


def usage(account_id: int) -> dict:
    e = db.q1("SELECT COUNT(*) c FROM events WHERE account_id=?", (account_id,))["c"]
    return {"events": e, **photo_usage(account_id), "users": seats_used(account_id)}


# ---------------------------------------------------------------------------------------------- request models
class SignupIn(BaseModel):
    company: str = Field(min_length=1, max_length=100)
    name: str = Field("", max_length=100)
    email: str = Field(max_length=320)
    password: str = Field(min_length=8, max_length=200)
    timezone: str = Field("", max_length=64)  # the browser's IANA zone, e.g. Asia/Kolkata


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


class ClientIn(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    email: Optional[str] = Field(None, max_length=320)
    phone: Optional[str] = Field(None, max_length=40)
    notes: Optional[str] = Field(None, max_length=2000)


class EventIn(BaseModel):
    client_id: int
    name: str = Field(min_length=1, max_length=120)
    subtitle: str = Field("", max_length=200)
    event_date: Optional[str] = Field(None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")


class EventPatch(BaseModel):
    client_id: Optional[int] = None
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    event_date: Optional[str] = Field(None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    expires_at: Optional[str] = Field(None, pattern=r"^(\d{4}-\d{2}-\d{2})?$")
    status: Optional[str] = Field(None, pattern=r"^(draft|published)$")
    allow_download: Optional[bool] = None
    tolerance: Optional[float] = None  # range depends on the event's face engine (faces.LIMITS)
    client_enabled: Optional[bool] = None   # the client gallery: every photo, on its own private link
    client_download: Optional[bool] = None


class AccountPatch(BaseModel):
    company: Optional[str] = Field(None, min_length=1, max_length=100)
    brand_color: Optional[str] = None
    name: Optional[str] = Field(None, max_length=100)
    timezone: Optional[str] = Field(None, max_length=64)
    logo_size: Optional[str] = Field(None, pattern=r"^(small|medium|large)$")
    theme: Optional[str] = Field(None, pattern=r"^(dark|light|auto)$")  # how guest and client pages look


class PasswordIn(BaseModel):
    current: str = Field("", max_length=200)  # not needed by a Google-only user setting a first password
    new: str = Field(min_length=8, max_length=200)


class PasswordOnly(BaseModel):
    password: str = Field("", max_length=200)
    confirm: str = Field("", max_length=100)  # users without a password type the company name instead


class ForgotIn(BaseModel):
    email: str = Field(max_length=320)


class TokenIn(BaseModel):
    token: str = Field(max_length=200)


class ResetIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(min_length=8, max_length=200)


class GoogleSignupIn(BaseModel):
    company: str = Field(min_length=1, max_length=100)
    timezone: str = Field("", max_length=64)


class IdsIn(BaseModel):
    ids: List[str] = Field(max_length=2000)


# ---------------------------------------------------------------------------------------------- the app
def create_app(data_dir=None, workers: Optional[int] = None) -> FastAPI:
    fix_proactor_resets()
    if data_dir is not None:
        config.settings.data_dir = Path(data_dir)
    if workers is not None:
        config.settings.workers = workers

    processor = Processor()
    scene_indexer = SceneIndexer(busy=lambda: not processor.idle())  # search by description, after face processing
    camera = CameraServer(poke=lambda: processor.poke(), plan_of=plan_of)
    vindex = VectorIndex()
    limiter = security.RateLimiter()
    engines = {}
    engine_lock = threading.Lock()  # one selfie is embedded at a time; dlib and OpenCV nets are not thread-safe

    def face_engine(model):
        if model not in engines:
            from faces import make_engine

            engines[model] = make_engine(model, config.settings.detector)
        return engines[model]

    @asynccontextmanager
    async def lifespan(app):
        db.init()
        remove_leftovers()  # half-written uploads cut off by the last restart
        upgrade_events()  # events still on an older face engine are re-indexed in the background
        processor.start()
        scene_indexer.start()
        if config.settings.ftp_port:
            camera.start()  # cameras upload to events over FTP(S)
        yield
        camera.stop()
        scene_indexer.stop()
        processor.stop()

    app = FastAPI(title="Photo Finder Studio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.processor = processor
    app.state.scene_indexer = scene_indexer

    @app.middleware("http")
    async def headers(request: Request, call_next):
        request.state.nonce = secrets.token_urlsafe(18)  # new on every response: a page's scripts carry it (see csp)
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")  # event links must not leak to other sites
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", csp(request.state.nonce))
        if request.url.path.startswith("/api/") and "cache-control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(HTTPException)
    async def http_err(request: Request, exc: HTTPException):
        d = exc.detail
        body = d if isinstance(d, dict) else {"message": str(d), "code": ""}
        return JSONResponse({"error": body.get("code") or "error", "message": body["message"]},
                            status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def bad_input(request: Request, exc: RequestValidationError):
        """A request that doesn't fit the model (too long, out of range, missing): say which field, in words."""
        e = (exc.errors() or [{}])[0]
        field = " ".join(str(x) for x in e.get("loc", ())[1:] if not isinstance(x, int)).replace("_", " ") or "the request"
        msg = str(e.get("msg", "isn't valid")).replace("String should have", "should have").replace("Input should be", "should be")
        return JSONResponse({"error": "invalid", "message": f"Check {field}: {msg}."}, status_code=422)

    # ---------------------------------------------------------------- auth plumbing
    def https() -> bool:
        return config.settings.public_url.startswith("https")

    def set_session(resp: Response, user_id: int) -> str:
        token = security.create_session(user_id)
        resp.set_cookie(COOKIE, token, max_age=config.settings.session_days * 86400,
                        httponly=True, samesite="lax", secure=https(), path="/")
        return token

    def user_dep(request: Request) -> dict:
        tok = request.cookies.get(COOKIE)
        u = security.lookup_session(tok) if tok else None
        if not u:
            raise err(401, "Please sign in.", "auth")
        if u["disabled_at"]:
            raise err(403, SUSPENDED, "suspended")
        if request.method not in ("GET", "HEAD") and request.headers.get("x-pf") != "1":
            raise err(403, "Missing X-PF header.", "csrf")  # a custom header can't be sent cross-site without CORS
        return u

    def can(perm: str):
        """Dependency: the signed-in user, if their role allows `perm` (studio/roles.py)."""
        def dep(u: dict = Depends(user_dep)) -> dict:
            if perm not in roles.perms(u["role"]):
                raise err(403, f"Your role ({roles.label(u['role'])}) can't {roles.PERMS[perm]}.", "role")
            return u
        return dep

    def me_payload(u: dict) -> dict:
        plan = plan_of(u["plan"])
        return {
            "user": {"email": u["email"], "name": u["name"], "role": u["role"], "role_label": roles.label(u["role"]),
                     "permissions": sorted(roles.perms(u["role"])),
                     "platform_admin": bool(u["platform_admin"]), "has_password": bool(u["pw_hash"]),
                     "google": bool(u["google_sub"])},
            "account": {"id": u["account_id"], "company": u["company"], "brand_color": u["brand_color"],
                        "plan": u["plan"], "plan_label": plan["label"], "timezone": u["timezone"],
                        "logo_url": branding.logo_url(u["logo"]), "logo_size": u["logo_size"],
                        "logo_badge": bool(u["logo_badge"]), "logo_badge_light": bool(u["logo_badge_light"]),
                        "theme": u["theme"]},
            "limits": {k: plan.get(k) for k in ("events", "photos", "storage", "users")},
            "usage": usage(u["account_id"]),
            "public_url": config.settings.public_url,
            "max_file_mb": config.settings.max_file_mb,
            "max_video_mb": config.settings.max_video_mb,
        }

    @app.post("/api/auth/signup")
    def signup(body: SignupIn, request: Request, response: Response):
        if not config.settings.allow_signup:
            raise err(403, "Sign-ups are closed.", "closed")
        if not limiter.allow(("signup", client_ip(request)), 8, 3600):
            raise err(429, "Too many sign-ups from this network. Try again later.", "rate")
        email = body.email.strip().lower()
        if not EMAIL_RE.match(email):
            raise err(422, "Enter a valid email address.", "email")
        if not body.company.strip():
            raise err(422, "Company name is required.", "company")
        uid = create_company(body.company, email, body.name, security.hash_password(body.password), body.timezone)
        return me_payload(security.lookup_session(set_session(response, uid)))

    def create_company(company, email, name, pw_hash, tz, google_sub=None) -> int:
        """New account + its owner; returns the user id."""
        tz = tz.strip() if valid_tz(tz.strip()) else ""
        now = int(time.time())
        try:
            with db.tx():
                aid = db.ex("INSERT INTO accounts(company, brand_color, plan, timezone, created_at) VALUES(?,?,?,?,?)",
                            (company.strip(), config.DEFAULT_BRAND, "free", tz, now))
                return db.ex("""INSERT INTO users(account_id, email, name, pw_hash, role, google_sub, created_at)
                                VALUES(?,?,?,?,?,?,?)""", (aid, email, name.strip(), pw_hash, "owner", google_sub, now))
        except sqlite3.IntegrityError:
            raise err(409, "An account with this email already exists.", "exists")

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response):
        email = body.email.strip().lower()
        ip = client_ip(request)
        if not limiter.allow(("login-ip", ip), 40, 600) or not limiter.allow(("login", ip, email), 10, 600):
            raise err(429, "Too many attempts. Wait a few minutes and try again.", "rate")
        r = db.q1("""SELECT u.id, u.pw_hash, a.disabled_at FROM users u JOIN accounts a ON a.id=u.account_id
                     WHERE u.email=?""", (email,))
        ok = security.verify_password(body.password, r["pw_hash"] if r else security.DUMMY_HASH)
        if not (r and ok):
            raise err(401, "Wrong email or password.", "login")
        if r["disabled_at"]:  # only told after the right password, so it doesn't reveal which emails exist
            raise err(403, SUSPENDED, "suspended")
        set_session(response, r["id"])
        return {"ok": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        tok = request.cookies.get(COOKIE)
        if tok:
            security.destroy_session(tok)
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    # ---------------------------------------------------------------- Google sign-in
    oauth_flows = google.Pending(600)     # state -> PKCE verifier, nonce, redirect URI
    google_signups = google.Pending(900)  # token -> verified Google profile waiting for a company name
    OAUTH_COOKIE, SIGNUP_COOKIE, GPATH = "pf_oauth", "pf_gsignup", "/api/auth/google"

    def back_to(hash_: str, set_cookie=None) -> RedirectResponse:
        r = RedirectResponse("/#/" + hash_, status_code=303)
        r.delete_cookie(OAUTH_COOKIE, path=GPATH)
        if set_cookie:
            r.set_cookie(set_cookie[0], set_cookie[1], max_age=900, httponly=True, samesite="lax", secure=https(),
                         path=GPATH)
        return r

    @app.get("/api/auth/providers")
    def providers():
        return {"google": google.enabled(), "signup": config.settings.allow_signup}

    @app.get(GPATH + "/start")
    def google_start(request: Request, invite: str = ""):
        if not google.enabled():
            raise err(404, "Google sign-in isn't set up on this server.", "not_found")
        if not limiter.allow(("google", client_ip(request)), 30, 600):
            return back_to("login?google=rate")
        state, nonce, verifier, challenge = google.new_flow()
        redirect = google.redirect_uri(public_base(request))
        oauth_flows.put({"nonce": nonce, "verifier": verifier, "redirect": redirect, "invite": invite[:200]}, key=state)
        r = RedirectResponse(google.auth_url(state, nonce, challenge, redirect), status_code=303)
        # binds the flow to this browser, so nobody can finish it with a link of their own (login CSRF)
        r.set_cookie(OAUTH_COOKIE, state, max_age=600, httponly=True, samesite="lax", secure=https(), path=GPATH)
        return r

    @app.get(GPATH + "/callback")
    def google_callback(request: Request, code: str = "", state: str = "", error: str = ""):
        flow = oauth_flows.pop(state) if state and state == request.cookies.get(OAUTH_COOKIE) else None
        if error:
            return back_to("login?google=" + ("cancelled" if error == "access_denied" else "error"))
        if not flow or not code:
            return back_to("login?google=state")
        try:
            g = google.claims(google.exchange_code(code, flow["verifier"], flow["redirect"]), flow["nonce"])
        except google.GoogleError as e:
            return back_to(f"login?google={e}")
        if flow.get("invite"):  # joining a company: the Google account must be the invited address
            try:
                inv = app.state.pending_invite(flow["invite"])
                if g["email"] != inv["email"].lower():
                    return back_to("join?google=wrong_email")
                uid = app.state.join_team(flow["invite"], g["name"], "", g["sub"])
            except HTTPException as e:
                return back_to("join?google=" + ((e.detail or {}).get("code") or "error"))
            resp = back_to("")
            set_session(resp, uid)
            return resp
        sql = "SELECT u.id, u.google_sub, a.disabled_at FROM users u JOIN accounts a ON a.id=u.account_id WHERE "
        r = db.q1(sql + "u.google_sub=?", (g["sub"],))
        if not r:
            r = db.q1(sql + "u.email=?", (g["email"],))
            if r and r["google_sub"]:
                return back_to("login?google=other_google")  # this email is linked to a different Google account
            if r:  # Google verified this address, so it's the same person: link it
                db.run("UPDATE users SET google_sub=? WHERE id=?", (g["sub"], r["id"]))
        if r:
            if r["disabled_at"]:
                return back_to("login?google=suspended")
            resp = back_to("")
            set_session(resp, r["id"])
            return resp
        if not config.settings.allow_signup:
            return back_to("login?google=closed")
        return back_to("google-finish", (SIGNUP_COOKIE, google_signups.put(g)))

    @app.get(GPATH + "/pending")
    def google_pending(request: Request):
        g = google_signups.get(request.cookies.get(SIGNUP_COOKIE))
        if not g:
            raise err(410, "That Google sign-up has expired. Start again.", "expired")
        return {"email": g["email"], "name": g["name"]}

    @app.post(GPATH + "/complete")
    def google_complete(body: GoogleSignupIn, request: Request, response: Response):
        if request.headers.get("x-pf") != "1":
            raise err(403, "Missing X-PF header.", "csrf")
        if not config.settings.allow_signup:
            raise err(403, "Sign-ups are closed.", "closed")
        if not limiter.allow(("signup", client_ip(request)), 8, 3600):
            raise err(429, "Too many sign-ups from this network. Try again later.", "rate")
        g = google_signups.get(request.cookies.get(SIGNUP_COOKIE))
        if not g:
            raise err(410, "That Google sign-up has expired. Start again.", "expired")
        if not body.company.strip():
            raise err(422, "Company name is required.", "company")
        uid = create_company(body.company, g["email"], g["name"], "", body.timezone, g["sub"])  # '' = no password
        google_signups.pop(request.cookies.get(SIGNUP_COOKIE))
        response.delete_cookie(SIGNUP_COOKIE, path=GPATH)
        return me_payload(security.lookup_session(set_session(response, uid)))

    # ---------------------------------------------------------------- forgot password
    def reset_row(token: str):
        r = db.q1("""SELECT pr.user_id, pr.expires_at, pr.used_at, u.email, a.disabled_at FROM password_resets pr
                     JOIN users u ON u.id=pr.user_id JOIN accounts a ON a.id=u.account_id WHERE pr.token_hash=?""",
                  (security._digest(token),))
        if not r or r["used_at"] or r["expires_at"] < time.time():
            raise err(410, "This reset link has expired or was already used. Ask for a new one.", "expired")
        return r

    @app.post("/api/auth/forgot")
    def forgot_password(body: ForgotIn, request: Request, tasks: BackgroundTasks):
        email = body.email.strip().lower()
        if not EMAIL_RE.match(email):
            raise err(422, "Enter a valid email address.", "email")
        if not limiter.allow(("forgot-ip", client_ip(request)), 10, 3600):
            raise err(429, "Too many requests. Wait a while and try again.", "rate")
        minutes = config.settings.reset_minutes
        # the same answer whether or not the account exists, so this can't be used to find out who has one
        same = {"ok": True, "message": f"If an account exists for {email}, we've emailed it a link to reset the "
                                       f"password. The link works for {minutes} minutes. Check your spam folder too."}
        if not limiter.allow(("forgot", email), 3, 3600):  # quietly: don't let anyone flood an inbox
            return same
        u = db.q1("""SELECT u.id, u.name, u.email, a.company, a.disabled_at, a.logo FROM users u
                     JOIN accounts a ON a.id=u.account_id WHERE u.email=?""", (email,))
        if u and not u["disabled_at"]:
            token, now = secrets.token_urlsafe(32), int(time.time())
            with db.tx():
                db.run("DELETE FROM password_resets WHERE expires_at < ? OR (user_id=? AND used_at IS NULL)", (now, u["id"]))
                db.ex("INSERT INTO password_resets(token_hash, user_id, expires_at, created_at) VALUES(?,?,?,?)",
                      (security._digest(token), u["id"], now + minutes * 60, now))
            # in the fragment (#), so the token never reaches a server log or a Referer header
            link = f"{email_base(request)}/#/reset?token={token}"
            text, page = reset_email(u["name"], u["company"], link, minutes,
                                     branding.logo_url(u["logo"], email_base(request)))
            tasks.add_task(mailer.send, u["email"], "Reset your Photo Finder Studio password", text, page)  # after the reply
        return same

    @app.post("/api/auth/reset/check")
    def reset_check(body: TokenIn):
        return {"email": mask_email(reset_row(body.token)["email"])}

    @app.post("/api/auth/reset")
    def reset_password(body: ResetIn, request: Request, response: Response):
        if not limiter.allow(("reset-ip", client_ip(request)), 20, 3600):
            raise err(429, "Too many attempts. Wait a while and try again.", "rate")
        r = reset_row(body.token)
        if r["disabled_at"]:
            raise err(403, SUSPENDED, "suspended")
        pw = security.hash_password(body.password)  # slow on purpose: do it before taking the write lock
        with db.tx():
            if db.run("UPDATE password_resets SET used_at=? WHERE token_hash=? AND used_at IS NULL",
                      (int(time.time()), security._digest(body.token))) != 1:
                raise err(410, "This reset link has expired or was already used. Ask for a new one.", "expired")
            db.run("UPDATE users SET pw_hash=? WHERE id=?", (pw, r["user_id"]))
            db.run("DELETE FROM sessions WHERE user_id=?", (r["user_id"],))  # whoever knew the old password is out
            db.run("DELETE FROM password_resets WHERE user_id=? AND used_at IS NULL", (r["user_id"],))
        set_session(response, r["user_id"])
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(u: dict = Depends(user_dep)):
        return me_payload(u)

    # ---------------------------------------------------------------- account
    @app.patch("/api/account")
    def patch_account(body: AccountPatch, request: Request, u: dict = Depends(user_dep)):
        if body.brand_color is not None and not COLOR_RE.match(body.brand_color):
            raise err(422, "Brand colour must look like #f0c52f.", "color")
        if body.timezone is not None and body.timezone.strip() and not valid_tz(body.timezone.strip()):
            raise err(422, "Unknown time zone. Use a name like Asia/Kolkata.", "timezone")
        company_fields = any(v is not None for v in (body.company, body.brand_color, body.timezone, body.logo_size,
                                                     body.theme))
        if company_fields and "company" not in roles.perms(u["role"]):
            raise err(403, f"Your role ({roles.label(u['role'])}) can't {roles.PERMS['company']}.", "role")
        if body.company is not None and not body.company.strip():
            raise err(422, "Company name is required.", "company")
        if body.company is not None:
            db.run("UPDATE accounts SET company=? WHERE id=?", (body.company.strip(), u["account_id"]))
        if body.brand_color is not None:
            db.run("UPDATE accounts SET brand_color=? WHERE id=?", (body.brand_color.lower(), u["account_id"]))
        if body.name is not None:
            db.run("UPDATE users SET name=? WHERE id=?", (body.name.strip(), u["user_id"]))
        if body.timezone is not None:
            db.run("UPDATE accounts SET timezone=? WHERE id=?", (body.timezone.strip(), u["account_id"]))
        if body.logo_size is not None:
            db.run("UPDATE accounts SET logo_size=? WHERE id=?", (body.logo_size, u["account_id"]))
        if body.theme is not None:
            db.run("UPDATE accounts SET theme=? WHERE id=?", (body.theme, u["account_id"]))
        return me_payload(security.lookup_session(request.cookies[COOKIE]))

    @app.post("/api/account/logo")
    def upload_logo(request: Request, file: UploadFile = File(...), u: dict = Depends(can("company"))):
        data = file.file.read(branding.MAX_UPLOAD + 1)
        if len(data) > branding.MAX_UPLOAD:
            raise err(413, "That image is too large. Use one under 8 MB.", "too_large")
        try:
            branding.save_logo(u["account_id"], data)
        except ValueError as e:
            raise err(422, str(e), "bad_image")
        return me_payload(security.lookup_session(request.cookies[COOKIE]))

    @app.delete("/api/account/logo")
    def delete_logo(request: Request, u: dict = Depends(can("company"))):
        branding.remove_logo(u["account_id"])
        return me_payload(security.lookup_session(request.cookies[COOKIE]))

    @app.get("/logo/{token}.png")
    def logo_image(token: str):
        """Public: guest and client pages show it. The random name changes with every upload."""
        r = db.q1("SELECT id FROM accounts WHERE logo=? AND logo != ''", (token,))
        f = branding.logo_file(r["id"], token) if r else None
        if not f or not f.exists():
            raise err(404, "Not found.", "not_found")
        return FileResponse(f, media_type="image/png", headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.post("/api/account/password")
    def change_password(body: PasswordIn, request: Request, u: dict = Depends(user_dep)):
        r = db.q1("SELECT pw_hash FROM users WHERE id=?", (u["user_id"],))
        if r["pw_hash"] and not security.verify_password(body.current, r["pw_hash"]):
            raise err(403, "Current password is wrong.", "password")
        db.run("UPDATE users SET pw_hash=? WHERE id=?", (security.hash_password(body.new), u["user_id"]))
        keep = security._digest(request.cookies.get(COOKIE, ""))
        db.run("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (u["user_id"], keep))  # sign out other devices
        return {"ok": True}

    @app.post("/api/account/delete")
    def delete_account(body: PasswordOnly, response: Response, u: dict = Depends(user_dep)):
        if "delete_account" not in roles.perms(u["role"]):
            raise err(403, "Only the owner can delete the account.", "role")
        r = db.q1("SELECT pw_hash FROM users WHERE id=?", (u["user_id"],))
        if r["pw_hash"]:
            if not security.verify_password(body.password, r["pw_hash"]):
                raise err(403, "Password is wrong.", "password")
        elif body.confirm.strip() != u["company"].strip():  # signed up with Google: no password to check
            raise err(403, "Type the company name exactly to confirm.", "password")
        aid = u["account_id"]
        db.run("DELETE FROM accounts WHERE id=?", (aid,))  # cascades users, sessions, clients, events, photos, faces
        shutil.rmtree(get_storage().root / "accounts" / str(aid), ignore_errors=True)
        response.delete_cookie(COOKIE, path="/")
        return {"ok": True}

    # ---------------------------------------------------------------- clients
    def get_client(u, cid):
        r = db.q1("SELECT * FROM clients WHERE id=? AND account_id=?", (cid, u["account_id"]))
        if not r:
            raise err(404, "Client not found.", "not_found")
        return r

    def client_out(r):
        return {"id": r["id"], "name": r["name"], "email": r["email"], "phone": r["phone"], "notes": r["notes"],
                "event_count": r["event_count"] if "event_count" in r.keys() else None}

    @app.get("/api/clients")
    def list_clients(u: dict = Depends(user_dep)):
        rows = db.q("""SELECT c.*, (SELECT COUNT(*) FROM events e WHERE e.client_id=c.id) AS event_count
                       FROM clients c WHERE c.account_id=? ORDER BY c.name COLLATE NOCASE""", (u["account_id"],))
        return [client_out(r) for r in rows]

    @app.post("/api/clients")
    def create_client(body: ClientIn, u: dict = Depends(can("clients"))):
        if not body.name or not body.name.strip():
            raise err(422, "Client name is required.", "name")
        cid = db.ex("INSERT INTO clients(account_id, name, email, phone, notes, created_at) VALUES(?,?,?,?,?,?)",
                    (u["account_id"], body.name.strip(), (body.email or "").strip(), (body.phone or "").strip(),
                     body.notes or "", int(time.time())))
        return client_out(db.q1("SELECT *, 0 AS event_count FROM clients WHERE id=?", (cid,)))

    @app.patch("/api/clients/{cid}")
    def update_client(cid: int, body: ClientIn, u: dict = Depends(can("clients"))):
        get_client(u, cid)
        for col in ("name", "email", "phone", "notes"):
            v = getattr(body, col)
            if v is not None:
                if col == "name" and not v.strip():
                    raise err(422, "Client name is required.", "name")
                db.run(f"UPDATE clients SET {col}=? WHERE id=? AND account_id=?", (v.strip(), cid, u["account_id"]))
        r = db.q1("""SELECT c.*, (SELECT COUNT(*) FROM events e WHERE e.client_id=c.id) AS event_count
                     FROM clients c WHERE c.id=?""", (cid,))
        return client_out(r)

    @app.delete("/api/clients/{cid}")
    def delete_client(cid: int, u: dict = Depends(can("clients"))):
        get_client(u, cid)
        if db.q1("SELECT 1 FROM events WHERE client_id=?", (cid,)):
            raise err(409, "This client still has events. Delete or move them first.", "has_events")
        db.run("DELETE FROM clients WHERE id=? AND account_id=?", (cid, u["account_id"]))
        return {"ok": True}

    # ---------------------------------------------------------------- events
    EVENT_SQL = """SELECT e.*, c.name AS client_name,
      (SELECT COUNT(*) FROM photos p WHERE p.event_id=e.id) AS n_photos,
      (SELECT COUNT(*) FROM photos p WHERE p.event_id=e.id AND p.status='ready') AS n_ready,
      (SELECT COUNT(*) FROM photos p WHERE p.event_id=e.id AND p.status IN ('uploading','uploaded','processing')) AS n_busy,
      (SELECT COUNT(*) FROM photos p WHERE p.event_id=e.id AND p.status='failed') AS n_failed,
      (SELECT COUNT(*) FROM photos p WHERE p.event_id=e.id AND p.edited=1) AS n_edited,
      (SELECT COUNT(*) FROM videos v WHERE v.event_id=e.id) AS n_videos,
      (SELECT COUNT(*) FROM album_picks a JOIN photos p ON p.id=a.photo_id WHERE a.event_id=e.id AND p.status='ready') AS n_album,
      (SELECT COUNT(*) FROM guest_searches s WHERE s.event_id=e.id) AS n_searches,
      (SELECT COALESCE(SUM(s.matches > 0), 0) FROM guest_searches s WHERE s.event_id=e.id) AS n_found
      FROM events e JOIN clients c ON c.id=e.client_id """

    def event_out(r, request: Request) -> dict:
        return {
            "id": r["id"], "client_id": r["client_id"], "client_name": r["client_name"], "name": r["name"],
            "subtitle": r["subtitle"], "event_date": r["event_date"], "expires_at": r["expires_at"],
            "status": r["status"], "allow_download": bool(r["allow_download"]), "tolerance": r["tolerance"],
            "faces_purged": bool(r["faces_purged"]), "slug": r["slug"], "url": f"{public_base(request)}/e/{r['slug']}",
            "face_model": r["face_model"],
            "tolerance_presets": [[v, LEVEL_LABELS[k]] for k, v in PRESETS.get(r["face_model"], PRESETS["dlib"]).items()],
            "photos": r["n_photos"], "ready": r["n_ready"], "processing": r["n_busy"], "failed": r["n_failed"],
            "edited": r["n_edited"],
            "videos": r["n_videos"], "album": r["n_album"], "album_locked": bool(r["album_locked"]),
            "searches": r["n_searches"], "found": r["n_found"], "guest_has_pin": bool(r["guest_pin"]),
            "client": {"enabled": bool(r["client_enabled"]), "has_pin": bool(r["client_pin"]),
                       "download": bool(r["client_download"]), "views": r["client_views"],
                       "url": f"{public_base(request)}/c/{r['client_token']}" if r["client_token"] else ""},
        }

    def get_event(u, eid, request=None):
        r = db.q1(EVENT_SQL + "WHERE e.id=? AND e.account_id=?", (eid, u["account_id"]))
        if not r:
            raise err(404, "Event not found.", "not_found")
        return r

    @app.get("/api/events")
    def list_events(request: Request, client_id: Optional[int] = None, u: dict = Depends(user_dep)):
        sql, args = EVENT_SQL + "WHERE e.account_id=? ", [u["account_id"]]
        if client_id is not None:
            sql += "AND e.client_id=? "
            args.append(client_id)
        rows = db.q(sql + "ORDER BY COALESCE(e.event_date, '') DESC, e.id DESC", args)
        return [event_out(r, request) for r in rows]

    @app.post("/api/events")
    def create_event(body: EventIn, request: Request, u: dict = Depends(can("events"))):
        get_client(u, body.client_id)
        plan = plan_of(u["plan"])
        with db.tx():  # the count and the insert are one write transaction, so parallel requests can't both pass
            if plan["events"] is not None and usage(u["account_id"])["events"] >= plan["events"]:
                raise err(403, f"The {plan['label']} plan allows {plan['events']} events. Upgrade to add more.",
                          "plan_limit")
            for _ in range(5):
                try:
                    model = config.settings.face_model
                    eid = db.ex("""INSERT INTO events(account_id, client_id, name, subtitle, event_date, slug, face_model,
                                   tolerance, created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                                (u["account_id"], body.client_id, body.name.strip(), body.subtitle.strip(),
                                 body.event_date or None, make_slug(body.name), model, PRESETS[model]["balanced"],
                                 int(time.time())))
                    break
                except sqlite3.IntegrityError:  # slug collision: vanishingly rare, just draw another
                    continue
            else:
                raise err(500, "Could not create the event. Try again.", "slug")
        get_storage().ensure(u["account_id"], eid)
        return event_out(get_event(u, eid), request)

    @app.get("/api/events/{eid}")
    def read_event(eid: int, request: Request, u: dict = Depends(user_dep)):
        return event_out(get_event(u, eid), request)

    @app.patch("/api/events/{eid}")
    def update_event(eid: int, body: EventPatch, request: Request, u: dict = Depends(can("events"))):
        ev = get_event(u, eid)
        fields = body.model_dump(exclude_unset=True)
        lo, hi = LIMITS.get(ev["face_model"], LIMITS["dlib"])
        if fields.get("tolerance") is not None and not lo <= fields["tolerance"] <= hi:
            raise err(422, f"Match strictness must be between {lo} and {hi} for this event.", "tolerance")
        for k, v in fields.items():  # only the dates may be cleared; null elsewhere would break NOT NULL columns
            if v is None and k not in ("event_date", "expires_at"):
                raise err(422, f"{k} can't be empty.", k)
        if "client_id" in fields:
            get_client(u, fields["client_id"])
        for k in ("event_date", "expires_at"):
            if k in fields and fields[k] == "":
                fields[k] = None
        if "name" in fields and not fields["name"].strip():
            raise err(422, "Event name is required.", "name")
        for k in ("allow_download", "client_enabled", "client_download"):
            if k in fields:
                fields[k] = int(bool(fields[k]))
        if fields.get("client_enabled") and not ev["client_token"]:  # first time on: make its secret link
            fields["client_token"] = new_token()
        for k, v in fields.items():  # keys come from the model above, never from the request
            db.run(f"UPDATE events SET {k}=? WHERE id=? AND account_id=?",
                   (v.strip() if isinstance(v, str) else v, eid, u["account_id"]))
        return event_out(get_event(u, eid), request)

    @app.post("/api/events/{eid}/guest-pin")
    def set_guest_pin(eid: int, body: PinIn, request: Request, u: dict = Depends(can("events"))):
        get_event(u, eid)
        pin = body.pin.strip()
        if pin and not PIN_RE.match(pin):
            raise err(422, "The PIN must be 4 to 8 digits.", "pin")
        db.run("UPDATE events SET guest_pin=? WHERE id=? AND account_id=?",
               (security.hash_password(pin) if pin else "", eid, u["account_id"]))
        return event_out(get_event(u, eid), request)

    @app.delete("/api/events/{eid}")
    def delete_event(eid: int, u: dict = Depends(can("events"))):
        get_event(u, eid)
        db.run("DELETE FROM events WHERE id=? AND account_id=?", (eid, u["account_id"]))
        get_storage().remove_event(u["account_id"], eid)
        return {"ok": True}

    @app.post("/api/events/{eid}/purge-faces")
    def purge_faces(eid: int, request: Request, u: dict = Depends(can("events"))):
        """Delete every stored face vector of the event (privacy). Guest search stops until it is re-indexed."""
        get_event(u, eid)
        db.run("DELETE FROM faces WHERE event_id=?", (eid,))
        db.run("UPDATE events SET faces_purged=1 WHERE id=?", (eid,))
        return event_out(get_event(u, eid), request)

    @app.post("/api/events/{eid}/reindex")
    def reindex(eid: int, request: Request, u: dict = Depends(can("events"))):
        """Process every photo of the event again (after a purge, or to apply new detection settings)."""
        ev = get_event(u, eid)
        db.run("UPDATE events SET faces_purged=0 WHERE id=?", (eid,))  # first: photos finished from now on keep their faces
        switch_model(eid, ev["face_model"], ev["tolerance"], config.settings.face_model)  # also moves to the current engine
        processor.poke()
        return event_out(get_event(u, eid), request)

    @app.get("/api/events/{eid}/qr.png")
    def event_qr(eid: int, request: Request, u: dict = Depends(user_dep)):
        import qrcode

        e = get_event(u, eid)
        buf = io.BytesIO()
        qrcode.make(f"{public_base(request)}/e/{e['slug']}", box_size=10, border=2).save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "private, max-age=300"})

    # ---------------------------------------------------------------- photos (dashboard side)
    @app.get("/api/events/{eid}/photos")
    def list_photos(eid: int, status: Optional[str] = None, vendor: Optional[str] = None, album: Optional[str] = None,
                    edited: Optional[str] = None, offset: int = 0, limit: int = 120, u: dict = Depends(user_dep)):
        """status: busy | ready | failed. vendor: "any" (sent through any vendor link) or one vendor link's id.
        album: "any" (in the album list), "client" (chosen by the client, alone or with the studio) or "studio" (likewise)."""
        get_event(u, eid)
        limit = max(1, min(limit, 500))
        sql, args = """SELECT p.public_id, p.orig_name, p.size, p.status, p.error, p.edited, p.edited_at, src.orig_name AS source,
                              src.public_id AS source_id, v.name AS vendor, a.picked_by AS album FROM photos p
                       LEFT JOIN photos src ON src.id=p.edited_of
                       LEFT JOIN vendor_links v ON v.id=p.vendor_id LEFT JOIN album_picks a ON a.photo_id=p.id AND a.event_id=p.event_id
                       WHERE p.event_id=? AND p.account_id=? """, [eid, u["account_id"]]
        if status == "busy":
            sql += "AND p.status IN ('uploading','uploaded','processing') "
        elif status in ("ready", "failed"):
            sql += "AND p.status=? "
            args.append(status)
        if vendor == "any":
            sql += "AND p.vendor_id IS NOT NULL "
        elif vendor and vendor.isdigit():
            sql += "AND p.vendor_id=? "
            args.append(int(vendor))
        if edited in ("0", "1"):
            sql += "AND p.edited=? "
            args.append(int(edited))
        if album == "any":
            sql += "AND a.photo_id IS NOT NULL "
        elif album in ("client", "studio"):
            sql += "AND a.picked_by IN (?, 'both') "
            args.append(album)
        rows = db.q(sql + "ORDER BY p.id DESC LIMIT ? OFFSET ?", args + [limit, max(0, offset)])
        return [{"id": r["public_id"], "name": r["orig_name"], "size": r["size"], "status": r["status"],
                 "error": r["error"], "vendor": r["vendor"], "album": r["album"], "edited": bool(r["edited"]),
                 "source": {"id": r["source_id"], "name": r["source"]} if r["source_id"] else None,
                 "v": r["edited_at"] or 0} for r in rows]

    @app.post("/api/events/{eid}/photos")
    def upload_photos(eid: int, files: List[UploadFile] = File(...), u: dict = Depends(can("upload"))):
        get_event(u, eid)
        cfg = config.settings
        if len(files) > cfg.max_files_per_request:
            raise err(413, f"Send at most {cfg.max_files_per_request} files per request.", "too_many")
        plan = plan_of(u["plan"])
        used = photo_usage(u["account_id"])
        results = [add_photo(u["account_id"], eid, plan, f.filename, f.file, used) for f in files]  # same path as cameras
        queued = sum(r["status"] == "queued" for r in results)
        if queued:
            processor.poke()
        return {"results": results, "queued": queued}

    def get_photo(u, eid, pid):
        r = db.q1("SELECT * FROM photos WHERE public_id=? AND event_id=? AND account_id=?", (pid, eid, u["account_id"]))
        if not r:
            raise err(404, "Photo not found.", "not_found")
        return r

    @app.get("/api/events/{eid}/photos/{pid}/{kind}")
    def photo_file(eid: int, pid: str, kind: str, u: dict = Depends(user_dep)):
        if kind not in ("thumb", "web", "orig"):
            raise err(404, "Not found.", "not_found")
        r = get_photo(u, eid, pid)
        path = get_storage().photo_path(r, kind)
        if not path.exists():
            raise err(404, "Not ready yet.", "not_ready")
        if kind == "orig":
            return FileResponse(path, filename=r["orig_name"], content_disposition_type="attachment")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/events/{eid}/photos/delete")
    def delete_photos(eid: int, body: IdsIn, u: dict = Depends(can("delete_photos"))):
        get_event(u, eid)
        store, n = get_storage(), 0
        for pid in body.ids:
            r = db.q1("SELECT * FROM photos WHERE public_id=? AND event_id=? AND account_id=?", (pid, eid, u["account_id"]))
            if r:
                db.run("DELETE FROM photos WHERE id=?", (r["id"],))  # faces cascade
                store.remove_photo(r)
                n += 1
        return {"deleted": n}

    @app.post("/api/events/{eid}/photos/retry")
    def retry_photos(eid: int, u: dict = Depends(can("upload"))):
        get_event(u, eid)
        n = db.run("UPDATE photos SET status='uploaded', error=NULL WHERE event_id=? AND account_id=? AND status='failed'",
                   (eid, u["account_id"]))
        processor.poke()
        return {"retried": n}

    # ---------------------------------------------------------------- guest side (no login)
    def guest_event(slug: str):
        r = db.q1("""SELECT e.*, a.company, a.brand_color, a.timezone, a.disabled_at, a.logo, a.logo_size, a.logo_badge,
                     a.logo_badge_light, a.theme FROM events e
                     JOIN accounts a ON a.id=e.account_id WHERE e.slug=?""", (slug,))
        if (not r or r["status"] != "published" or r["disabled_at"]
                or (r["expires_at"] and today_in(r["timezone"]) > r["expires_at"])):
            raise err(404, "This gallery isn't available.", "unavailable")
        return r

    def guest_pass(e) -> str:
        """Cookie value proving the guests' PIN was entered; changing the PIN locks every browser out again."""
        return hashlib.sha256(f"guest:{e['slug']}:{e['guest_pin']}".encode()).hexdigest()

    def guest_unlocked(request: Request, e) -> bool:
        return not e["guest_pin"] or hmac.compare_digest(request.cookies.get(f"pf_g{e['id']}", ""), guest_pass(e))

    def guest_open(slug: str, request: Request):
        """The published event, if this browser may see its photos (no PIN, or the PIN was entered)."""
        e = guest_event(slug)
        if not guest_unlocked(request, e):
            raise err(401, "Enter the PIN to find your photos.", "pin")
        return e

    def guest_photo(slug, pid, request: Request):
        e = guest_open(slug, request)
        r = db.q1("SELECT * FROM photos WHERE public_id=? AND event_id=? AND status='ready'", (pid, e["id"]))
        if not r:
            raise err(404, "Photo not found.", "not_found")
        return e, r

    def file_or_404(path, **kw):
        if not path.exists():  # deleted a moment ago (or the disk was tidied by hand)
            raise err(404, "Photo not found.", "not_found")
        return FileResponse(path, **kw)

    @app.get("/e/{slug}", response_class=HTMLResponse)
    def guest_page(slug: str, request: Request):
        try:
            e = guest_event(slug)
        except HTTPException:
            return HTMLResponse((STATIC / "unavailable.html").read_text(encoding="utf-8"), status_code=404)
        page = branding.render((STATIC / "guest.html").read_text(encoding="utf-8"), e, ink_for(e["brand_color"]), {
            "__TITLE__": e["name"], "__SUBTITLE__": e["subtitle"] or "Take a selfie and we'll find every photo you're in.",
            "__COMPANY__": e["company"]}, nonce=request.state.nonce)
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/e/{slug}/info")
    def guest_info(slug: str, request: Request):
        e = guest_event(slug)
        if not guest_unlocked(request, e):  # locked: the name only, nothing about the photos
            return {"name": e["name"], "has_pin": True, "unlocked": False}
        c = db.q1("""SELECT SUM(status='ready') ready, SUM(status IN ('uploading','uploaded','processing')) busy
                     FROM photos WHERE event_id=?""", (e["id"],))
        return {"name": e["name"], "has_pin": bool(e["guest_pin"]), "unlocked": True, "ready": c["ready"] or 0,
                "processing": c["busy"] or 0, "allow_download": bool(e["allow_download"]), "searchable": not e["faces_purged"]}

    @app.post("/e/{slug}/unlock")
    def guest_unlock(slug: str, body: UnlockIn, request: Request):
        e = guest_event(slug)
        ip = client_ip(request)
        # per visitor and per event, so a 6-digit PIN can't be walked through from many addresses either
        if not limiter.allow(("gpin", ip, slug), 10, 900) or not limiter.allow(("gpin-all", slug), 300, 3600):
            raise err(429, "Too many tries. Wait 15 minutes and try again.", "rate")
        if not e["guest_pin"] or not security.verify_password(body.pin.strip(), e["guest_pin"]):
            raise err(403, "That PIN isn't right.", "pin")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(f"pf_g{e['id']}", guest_pass(e), max_age=30 * 86400, httponly=True, samesite="lax",
                        secure=https(), path=f"/e/{slug}")
        return resp

    def match_selfie(e, file: UploadFile):
        """The event's photos the biggest face in an uploaded selfie appears in, best match first.

        Shared by the guests' search and the client gallery's filter. The image stays in memory and is never stored.
        Returns (rows of id, public_id, orig_name, width, height; whether the selfie had more than one face).
        """
        if e["faces_purged"]:
            raise err(410, "Photo search is switched off for this gallery.", "purged")
        limit = config.settings.max_selfie_mb * 1024 * 1024
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise err(413, "That image is too large.", "too_large")
        from faces import open_image

        try:
            im = open_image(io.BytesIO(data))  # kept in memory only, never written to disk
        except Exception:
            raise err(400, "We couldn't read that image.", "bad_image")
        with engine_lock:
            found = face_engine(e["face_model"]).faces(im, detect_side=1400, min_face=60, jitters=5)
        if not found:
            raise err(422, "No face found.", "no_face")
        found.sort(key=lambda f: f[0], reverse=True)  # the biggest face is the person holding the phone
        matches = vindex.search(e["id"], e["face_model"], found[0][1], e["tolerance"])
        rows = []
        if matches:
            order = {pid: i for i, (pid, _) in enumerate(matches)}
            ids = list(order)
            for i in range(0, len(ids), 500):
                part = ids[i:i + 500]
                rows += db.q(f"SELECT id, public_id, orig_name, width, height, edited FROM photos WHERE status='ready' "
                             f"AND event_id=? AND id IN ({','.join('?' * len(part))})", [e["id"]] + part)
            rows.sort(key=lambda r: order[r["id"]])
        return rows, len(found) > 1

    @app.post("/e/{slug}/search")
    def guest_search(slug: str, request: Request, file: UploadFile = File(...)):
        e = guest_open(slug, request)
        ip = client_ip(request)
        if not limiter.allow(("search", ip, slug), 20, 600) or not limiter.allow(("search-ip", ip), 80, 600):
            raise err(429, "Too many tries. Please wait a few minutes.", "rate")
        rows, multiple = match_selfie(e, file)
        photos = [{"id": r["public_id"], "name": r["orig_name"], "edited": bool(r["edited"])} for r in rows]
        db.run("INSERT INTO guest_searches(event_id, created_at, matches) VALUES(?,?,?)",
               (e["id"], int(time.time()), len(photos)))  # a count only: no selfie, no face data
        return {"count": len(photos), "multiple_faces": multiple, "photos": photos}

    @app.get("/e/{slug}/thumb/{pid}.jpg")
    def guest_thumb(slug: str, pid: str, request: Request):
        _, r = guest_photo(slug, pid, request)
        return file_or_404(get_storage().photo_path(r, "thumb"), headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/e/{slug}/web/{pid}.jpg")
    def guest_web(slug: str, pid: str, request: Request):
        _, r = guest_photo(slug, pid, request)
        return file_or_404(get_storage().photo_path(r, "web"), headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/e/{slug}/download/{pid}")
    def guest_download(slug: str, pid: str, request: Request):
        e, r = guest_photo(slug, pid, request)
        if not e["allow_download"]:
            raise err(403, "Downloads are turned off for this gallery.", "no_download")
        return file_or_404(get_storage().photo_path(r, "orig"), filename=r["orig_name"],
                           content_disposition_type="attachment")

    @app.post("/e/{slug}/zip")
    def guest_zip(slug: str, request: Request, ids: str = Form(...)):
        e = guest_open(slug, request)
        if not e["allow_download"]:
            raise err(403, "Downloads are turned off for this gallery.", "no_download")
        ip = client_ip(request)  # each zip copies up to 1000 originals to disk, so keep it rare per visitor
        if not limiter.allow(("zip", ip, slug), 10, 600) or not limiter.allow(("zip-ip", ip), 30, 600):
            raise err(429, "Too many downloads. Please wait a few minutes.", "rate")
        want = [p for p in ids.split(",") if p][:1000]
        rows = []
        for i in range(0, len(want), 500):
            part = want[i:i + 500]
            rows += db.q(f"SELECT * FROM photos WHERE status='ready' AND event_id=? AND public_id IN ({','.join('?' * len(part))})",
                         [e["id"]] + part)
        if not rows:
            raise err(400, "No photos selected.", "empty")
        store = get_storage()
        files = [(store.photo_path(r, "orig"), name) for r, name in unique_names(rows)]
        fname = re.sub(r"[^A-Za-z0-9]+", "-", e["name"]).strip("-").lower() or "photos"
        return StreamingResponse(zip_stream(files), media_type="application/zip",
                                 headers={"Content-Disposition": f'attachment; filename="{fname}-my-photos.zip"'})

    # ---------------------------------------------------------------- pages
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        page = (STATIC / "app.html").read_text(encoding="utf-8").replace("__VIEWER__", (STATIC / "_viewer.html").read_text(encoding="utf-8"))
        return HTMLResponse(branding.with_nonce(page, request.state.nonce), headers={"Cache-Control": "no-store"})

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        page = branding.with_nonce((STATIC / "admin.html").read_text(encoding="utf-8"), request.state.nonce)
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    mount_team(app, SimpleNamespace(user_dep=user_dep, can=can, err=err, public_base=email_base, client_ip=client_ip,
                                    limiter=limiter, set_session=set_session, email_re=EMAIL_RE, plan_of=plan_of,
                                    suspended=SUSPENDED))

    mount_client(app, SimpleNamespace(can=can, err=err, public_base=public_base, client_ip=client_ip, limiter=limiter,
                                      https=https, ink_for=ink_for, get_event=get_event, event_out=event_out,
                                      match_selfie=match_selfie))

    mount_videos(app, SimpleNamespace(user_dep=user_dep, can=can, err=err, get_event=get_event, plan_of=plan_of,
                                      client_open=app.state.client_open))
    mount_camera(app, SimpleNamespace(can=can, err=err, get_event=get_event))
    mount_inventory(app, SimpleNamespace(user_dep=user_dep, can=can, err=err))
    mount_bookings(app, SimpleNamespace(user_dep=user_dep, can=can, err=err))
    mount_tasks(app, SimpleNamespace(user_dep=user_dep, can=can, err=err))
    mount_editing(app, SimpleNamespace(user_dep=user_dep, can=can, err=err, get_event=get_event, plan_of=plan_of,
                                       poke=lambda: processor.poke()))
    mount_clips(app, SimpleNamespace(err=err, client_open=app.state.client_open, client_ip=client_ip, limiter=limiter))
    mount_scenes(app, SimpleNamespace(user_dep=user_dep, can=can, err=err, get_event=get_event, client_open=app.state.client_open,
                                      client_ip=client_ip, limiter=limiter))
    mount_album(app, SimpleNamespace(user_dep=user_dep, can=can, err=err, get_event=get_event, client_open=app.state.client_open,
                                     client_ip=client_ip, limiter=limiter))
    mount_vendors(app, SimpleNamespace(user_dep=user_dep, can=can, err=err, get_event=get_event, plan_of=plan_of,
                                       poke=lambda: processor.poke(), client_ip=client_ip, limiter=limiter,
                                       public_base=public_base, ink_for=ink_for))
    mount_estimates(app, SimpleNamespace(user_dep=user_dep, can=can, err=err))
    app.state.camera = camera

    from .admin import mount_admin  # here, not at the top: admin.py uses the helpers defined in this module

    mount_admin(app, user_dep)

    @app.get("/healthz")
    def healthz():
        db.q1("SELECT 1")
        return {"ok": True}

    return app
