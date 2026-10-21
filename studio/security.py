"""Passwords, sessions and rate limiting (stdlib only)."""
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from collections import defaultdict, deque

from . import config, db

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt_b64, h_b64 = stored.split("$")
        salt, want = base64.b64decode(salt_b64), base64.b64decode(h_b64)
        got = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(got, want)
    except Exception:
        return False


DUMMY_HASH = hash_password("not-a-real-password")  # verified against when the email is unknown, so timing doesn't leak it


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.run("DELETE FROM sessions WHERE expires_at < ?", (now,))
    db.ex("INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES(?,?,?,?)",
          (_digest(token), user_id, now + config.settings.session_days * 86400, now))
    return token


def lookup_session(token: str):
    r = db.q1(
        """SELECT u.id AS user_id, u.email, u.name, u.role, u.platform_admin, u.last_seen_at, u.pw_hash, u.google_sub,
                  a.id AS account_id, a.company, a.brand_color, a.plan, a.timezone, a.disabled_at, a.disabled_reason,
                  a.logo, a.logo_size, a.logo_badge, a.logo_badge_light, a.theme,
                  s.expires_at
           FROM sessions s JOIN users u ON u.id = s.user_id JOIN accounts a ON a.id = u.account_id
           WHERE s.token_hash = ?""", (_digest(token),))
    if not r or r["expires_at"] < time.time():
        return None
    now = int(time.time())
    if not r["last_seen_at"] or r["last_seen_at"] < now - 300:  # activity for the admin dashboard, at most every 5 min
        db.run("UPDATE users SET last_seen_at=? WHERE id=?", (now, r["user_id"]))
    return dict(r)


def destroy_session(token: str):
    db.run("DELETE FROM sessions WHERE token_hash = ?", (_digest(token),))


class RateLimiter:
    """Sliding window per key, in memory (one server process)."""

    def __init__(self):
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key, limit: int, window: float) -> bool:
        now = time.monotonic()
        with self.lock:
            if len(self.hits) > 20000:  # drop stale keys
                for k in [k for k, d in self.hits.items() if not d or d[-1] < now - 3600]:
                    del self.hits[k]
            dq = self.hits[key]
            while dq and dq[0] <= now - window:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True
