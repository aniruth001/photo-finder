"""Camera upload: cameras send photos straight to an event over FTP (or FTPS).

Camera upload is off for every event until the platform admin allows it for that event (admin dashboard, the
company's Events table; events.ftp_allowed). Each allowed event can have one camera login. It can only add photos to that event: it can't download, delete or see
anything (its folder is an empty inbox; every file is moved into the event the moment it arrives). Photos go
through studio/ingest.add_photo, exactly like a dashboard upload: duplicates, plan limits, processing.

The server runs inside Studio when PF_FTP_PORT (or `run --ftp-port`) is set. Cameras need the server's public
address and, behind NAT or a firewall, the passive port range opened as well.
"""
import logging
import secrets
import shutil
import socket
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends
from pydantic import BaseModel

from . import config, db, security
from .ingest import add_photo

ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i: these are typed on a camera's dial
STALE = 600                                    # unfinished or unrenamed files are swept after 10 minutes


class CameraPatch(BaseModel):
    enabled: bool


def _code(n: int) -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(n))


def inbox(login_id: int) -> Path:
    return config.settings.data_dir / "ftp-inbox" / str(login_id)


def server_info() -> dict:
    s = config.settings
    host = s.ftp_host or (s.public_url.split("://", 1)[-1].split("/")[0].split(":")[0] if s.public_url else "")
    tls = "required" if s.ftp_cert and s.ftp_require_tls else "available" if s.ftp_cert else "none"
    return {"available": bool(s.ftp_port), "host": host, "port": s.ftp_port, "tls": tls}


# Failed camera logins in the last 15 minutes, counted three ways. Several cameras usually share one venue Wi-Fi,
# so one camera typing a wrong password must not lock out the others: the tight limit is per camera login on one
# address. Looser limits stop someone trying one login from many addresses, or many logins from one address.
PER_LOGIN_HERE, PER_LOGIN, PER_ADDRESS = 10, 60, 300


class _Failures:
    """Sliding-window counters of failed logins, by key."""

    def __init__(self, window=900):
        self.window, self.hits, self.lock = window, defaultdict(deque), threading.Lock()

    def count(self, key) -> int:
        now = time.monotonic()
        with self.lock:
            d = self.hits.get(key)
            while d and d[0] < now - self.window:
                d.popleft()
            if d is not None and not d:
                del self.hits[key]
            return len(d) if d else 0

    def blocked(self, key, limit) -> bool:
        return self.count(key) >= limit

    def add(self, *keys):
        now = time.monotonic()
        with self.lock:
            for k in keys:
                self.hits[k].append(now)

    def refusal(self, ip, user):
        """Why this login attempt is refused before checking the password, or None."""
        if self.blocked(("here", ip, user), PER_LOGIN_HERE):
            return "Too many wrong passwords for this camera login. Check it in the camera, then try again in 15 minutes."
        if self.blocked(("login", user), PER_LOGIN) or self.blocked(("address", ip), PER_ADDRESS):
            return "Too many failed logins. Try again later."
        return None

    def failed(self, ip, user):
        self.add(("here", ip, user), ("login", user), ("address", ip))


def _receive(login_id: int, path: str, poke, plan_of):
    """A file has arrived in a login's inbox: add it to the event, whatever happens remove it from the inbox."""
    p = Path(path)
    if p.suffix.lower() not in config.ALLOWED_EXTS:
        return  # maybe a temporary name the camera renames next (see ftp_RNTO); swept later if not
    try:
        r = db.q1("""SELECT c.event_id, e.account_id, e.ftp_allowed, a.plan, a.disabled_at, c.enabled FROM camera_logins c
                     JOIN events e ON e.id=c.event_id JOIN accounts a ON a.id=e.account_id WHERE c.id=?""", (login_id,))
        if not r or not r["enabled"] or not r["ftp_allowed"] or r["disabled_at"]:
            return
        with open(p, "rb") as f:
            res = add_photo(r["account_id"], r["event_id"], plan_of(r["plan"]), p.name, f)
        now = int(time.time())
        if res["status"] == "queued":
            db.run("UPDATE camera_logins SET last_upload_at=?, uploads=uploads+1, last_note=? WHERE id=?",
                   (now, f"Received {p.name}", login_id))
            poke()
        else:
            db.run("UPDATE camera_logins SET last_note=? WHERE id=?",
                   (f"{p.name}: {res['error'] or res['status']}", login_id))
    finally:
        p.unlink(missing_ok=True)


def _sweep(login_id: int):
    d = inbox(login_id)
    cutoff = time.time() - STALE
    for f in sorted(d.rglob("*"), reverse=True) if d.exists() else ():  # deepest first, so emptied folders go too
        try:
            if f.stat().st_mtime >= cutoff:
                continue
            if f.is_file():
                f.unlink()
            elif f.is_dir() and not any(f.iterdir()):
                f.rmdir()
        except OSError:
            pass


class CameraServer:
    """The FTP(S) server. One thread per connection; logins are checked against camera_logins."""

    def __init__(self, poke, plan_of):
        self.poke, self.plan_of, self.server, self.thread = poke, plan_of, None, None
        self.failures = _Failures()

    def start(self):
        from pyftpdlib.authorizers import AuthenticationFailed
        from pyftpdlib.handlers import DTPHandler, FTPHandler, TLS_DTPHandler, TLS_FTPHandler
        from pyftpdlib.servers import ThreadedFTPServer

        s, outer = config.settings, self
        logging.getLogger("pyftpdlib").setLevel(logging.WARNING)  # like the web server: warnings only, no per-session lines

        class Authorizer:
            """Read-nothing, delete-nothing: change folder, make folders, list (always empty), store, rename."""
            perms = "elmwf"

            def validate_authentication(self, username, password, handler):
                ip, user = handler.remote_ip, (username or "").strip().lower()[:64]
                why = outer.failures.refusal(ip, user)
                if why:
                    raise AuthenticationFailed(why)
                r = db.q1("""SELECT c.id, c.pw_hash, c.enabled, e.id AS event_id, e.ftp_allowed, a.disabled_at FROM camera_logins c
                             JOIN events e ON e.id=c.event_id JOIN accounts a ON a.id=e.account_id
                             WHERE c.username=?""", (user,))
                ok = security.verify_password(password or "", r["pw_hash"] if r else security.DUMMY_HASH)
                if not (r and ok):
                    outer.failures.failed(ip, user)
                    raise AuthenticationFailed("Wrong camera username or password.")
                if not r["enabled"] or not r["ftp_allowed"] or r["disabled_at"]:
                    raise AuthenticationFailed("Camera upload is switched off for this event.")
                handler.login_id = r["id"]
                inbox(r["id"]).mkdir(parents=True, exist_ok=True)
                db.run("UPDATE camera_logins SET last_login_at=? WHERE id=?", (int(time.time()), r["id"]))

            def get_home_dir(self, username):
                r = db.q1("SELECT id FROM camera_logins WHERE username=?", ((username or "").strip().lower(),))
                return str(inbox(r["id"]))

            def has_user(self, username):
                return bool(db.q1("SELECT 1 FROM camera_logins WHERE username=?", ((username or "").strip().lower(),)))

            def has_perm(self, username, perm, path=None):
                return perm in self.perms

            def get_perms(self, username):
                return self.perms

            def get_msg_login(self, username):
                return "Logged in. Photos you send go to this event."

            def get_msg_quit(self, username):
                return "Goodbye."

            def impersonate_user(self, username, password):
                pass

            def terminate_impersonation(self, username):
                pass

        class LimitedDTP(DTPHandler):
            """Stops a transfer the moment it passes the per-file size limit (before it fills the disk)."""

            def handle_read(self):
                super().handle_read()
                if self.receive and self.tot_bytes_received > config.settings.max_file_mb * 1024 * 1024:
                    # counted as incomplete, so the partial file is removed; the camera gets a clear answer
                    self._resp = (f"552 File is larger than {config.settings.max_file_mb} MB.", logging.info)
                    self.close()

            handle_read_event = handle_read  # pyftpdlib aliases this to the original method; point it at ours

        class TLSLimitedDTP(TLS_DTPHandler, LimitedDTP):
            """FTPS: the TLS layer (handshake, clean shutdown) calls super().handle_read_event(), which now lands
            on LimitedDTP: the order is TLS_DTPHandler, SSLConnection, LimitedDTP, DTPHandler."""

        def handler_class(base, dtp):
            class Camera(base):
                dtp_handler = dtp
                banner = "Photo Finder Studio camera upload ready."
                max_login_attempts = 3
                auth_failed_timeout = 2
                login_id = None
                session_dir = None

                def on_login(self, username):
                    # Each connection gets its own folder inside the login's inbox. Several cameras can share one
                    # event's login, and two of them sending DSC_0001.JPG at the same moment must not collide.
                    d = inbox(self.login_id) / f"c-{secrets.token_hex(6)}"
                    d.mkdir(parents=True, exist_ok=True)
                    self.session_dir, self.fs.root, self.fs.cwd = d, str(d), "/"

                def on_disconnect(self):
                    if self.session_dir:  # anything left is unfinished or never renamed to a photo name
                        shutil.rmtree(self.session_dir, ignore_errors=True)

                def on_file_received(self, file):
                    _receive(self.login_id, file, outer.poke, outer.plan_of)

                def on_incomplete_file_received(self, file):
                    Path(file).unlink(missing_ok=True)

                def ftp_RNTO(self, path):  # some cameras upload as .tmp, then rename to .JPG
                    done = super().ftp_RNTO(path)
                    if done:
                        _receive(self.login_id, done[1], outer.poke, outer.plan_of)
                    return done

                def on_logout(self, username):
                    if self.login_id:
                        _sweep(self.login_id)
            return Camera

        if s.ftp_cert:
            handler = handler_class(TLS_FTPHandler, TLSLimitedDTP)
            handler.certfile, handler.keyfile = s.ftp_cert, s.ftp_key or None
            handler.tls_control_required = handler.tls_data_required = bool(s.ftp_require_tls)
        else:
            handler = handler_class(FTPHandler, LimitedDTP)
        handler.authorizer = Authorizer()
        if s.ftp_passive:
            lo, _, hi = s.ftp_passive.partition("-")
            handler.passive_ports = range(int(lo), int(hi or lo) + 1)
        host = server_info()["host"]
        if host:  # tell cameras the public address for passive data connections (behind NAT)
            try:
                handler.masquerade_address = socket.gethostbyname(host)
            except OSError:
                pass
        self.server = ThreadedFTPServer((s.ftp_bind, s.ftp_port), handler)
        self.server.max_cons, self.server.max_cons_per_ip = s.ftp_max_connections, s.ftp_max_per_ip
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"handle_exit": False},
                                       name="camera-ftp", daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.close_all()
            self.server = None


def mount_camera(app, h):
    """h: helpers from server.create_app (can, err, get_event)."""
    err = h.err

    def allowed(eid) -> bool:
        return bool(db.q1("SELECT ftp_allowed FROM events WHERE id=?", (eid,))["ftp_allowed"])

    def need_allowed(eid):
        if not allowed(eid):
            raise err(403, "Camera upload isn't enabled for this event. Ask the platform administrator.", "not_allowed")

    def status(eid):
        r = db.q1("SELECT * FROM camera_logins WHERE event_id=?", (eid,))
        login = None if not r else {
            "username": r["username"], "enabled": bool(r["enabled"]), "created_at": r["created_at"],
            "last_login_at": r["last_login_at"], "last_upload_at": r["last_upload_at"], "uploads": r["uploads"],
            "last_note": r["last_note"]}
        return {"server": server_info(), "allowed": allowed(eid), "login": login, "now": int(time.time())}

    @app.get("/api/events/{eid}/camera")
    def read(eid: int, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        return status(eid)

    @app.post("/api/events/{eid}/camera")
    def create_or_rotate(eid: int, u: dict = Depends(h.can("events"))):
        """A new password (and, the first time, a username). The password is only ever shown in this answer."""
        h.get_event(u, eid)
        need_allowed(eid)
        if not config.settings.ftp_port:
            raise err(409, "Camera upload isn't switched on for this server.", "unavailable")
        password, now = _code(10), int(time.time())
        r = db.q1("SELECT id FROM camera_logins WHERE event_id=?", (eid,))
        if r:
            db.run("UPDATE camera_logins SET pw_hash=?, enabled=1, created_at=? WHERE id=?",
                   (security.hash_password(password), now, r["id"]))
        else:
            for _ in range(5):
                try:
                    db.ex("""INSERT INTO camera_logins(event_id, username, pw_hash, enabled, created_at)
                             VALUES(?,?,?,1,?)""", (eid, "cam-" + _code(8), security.hash_password(password), now))
                    break
                except sqlite3.IntegrityError:  # a username collision: draw another
                    continue
        return {**status(eid), "password": password}

    @app.patch("/api/events/{eid}/camera")
    def switch(eid: int, body: CameraPatch, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        if body.enabled:
            need_allowed(eid)
        if not db.run("UPDATE camera_logins SET enabled=? WHERE event_id=?", (int(body.enabled), eid)):
            raise err(404, "This event has no camera login yet.", "not_found")
        return status(eid)

    @app.delete("/api/events/{eid}/camera")
    def remove(eid: int, u: dict = Depends(h.can("events"))):
        h.get_event(u, eid)
        db.run("DELETE FROM camera_logins WHERE event_id=?", (eid,))
        return status(eid)
