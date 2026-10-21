"""Google sign-in: OAuth 2.0 authorization-code flow with PKCE (stdlib only).

The ID token comes straight from Google's token endpoint over HTTPS in exchange for a one-time code, so its
signature doesn't need checking (Google's OpenID Connect guide says so for this flow); its claims still are.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")


class GoogleError(Exception):
    """`str(e)` is a short code the dashboard turns into a message."""


def enabled() -> bool:
    s = config.settings
    return bool(s.google_client_id and s.google_client_secret)


def redirect_uri(base: str) -> str:
    """Must be listed exactly like this under "Authorized redirect URIs" in Google Cloud."""
    return f"{base}/api/auth/google/callback"


def new_flow():
    """(state, nonce, PKCE verifier, PKCE challenge)."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return secrets.token_urlsafe(24), secrets.token_urlsafe(24), verifier, challenge


def auth_url(state: str, nonce: str, challenge: str, redirect: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": config.settings.google_client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce,
        "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account",
    })


def exchange_code(code: str, verifier: str, redirect: str) -> dict:
    """POST the one-time code to Google; returns the token response (with id_token)."""
    data = urllib.parse.urlencode({
        "code": code, "client_id": config.settings.google_client_id,
        "client_secret": config.settings.google_client_secret, "redirect_uri": redirect,
        "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, ValueError, OSError) as e:
        raise GoogleError("exchange") from e


def claims(token_response: dict, nonce: str) -> dict:
    """Checked claims of the ID token: {sub, email, name}."""
    try:
        payload = token_response["id_token"].split(".")[1]
        c = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise GoogleError("token") from e
    aud = c.get("aud")
    if (c.get("iss") not in ISSUERS or config.settings.google_client_id not in (aud if isinstance(aud, list) else [aud])
            or float(c.get("exp", 0)) < time.time() or c.get("nonce") != nonce or not c.get("sub")):
        raise GoogleError("token")
    if not c.get("email") or c.get("email_verified") not in (True, "true"):
        raise GoogleError("unverified")
    return {"sub": str(c["sub"]), "email": str(c["email"]).strip().lower(), "name": str(c.get("name") or "")[:100]}


class Pending:
    """Short-lived server-side values keyed by a random token (one server process, like the rate limiter)."""

    def __init__(self, ttl: int = 600):
        self.ttl, self.items, self.lock = ttl, {}, threading.Lock()

    def put(self, value, key=None) -> str:
        key = key or secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            for k in [k for k, (t, _) in self.items.items() if t < now]:
                del self.items[k]
            self.items[key] = (now + self.ttl, value)
        return key

    def get(self, key):
        with self.lock:
            hit = self.items.get(key or "")
        return hit[1] if hit and hit[0] >= time.time() else None

    def pop(self, key):
        with self.lock:
            hit = self.items.pop(key or "", None)
        return hit[1] if hit and hit[0] >= time.time() else None
