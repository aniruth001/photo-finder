"""Google sign-in: sign-up with a company name, sign-in, linking, and every way the flow is refused.

Google is faked: exchange_code returns the token response Google would send for the one-time code.
"""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

from studio import config, db, google

CLIENT = "test-client.apps.googleusercontent.com"


def id_token(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


@pytest.fixture()
def g(app_client, monkeypatch):
    """Google turned on, with a fake Google account; returns a helper that runs the whole browser flow."""
    monkeypatch.setattr(config.settings, "google_client_id", CLIENT)
    monkeypatch.setattr(config.settings, "google_client_secret", "secret")
    who = {"sub": "g-111", "email": "Maya@Gmail.com", "name": "Maya Rao", "email_verified": True}
    seen = {}

    def fake_exchange(code, verifier, redirect):
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        assert challenge == seen["challenge"] and redirect.endswith("/api/auth/google/callback")  # PKCE holds
        claims = {"iss": "https://accounts.google.com", "aud": CLIENT, "exp": time.time() + 600,
                  "nonce": seen["nonce"], **who}
        return {"id_token": id_token(**claims), "access_token": "x"}

    monkeypatch.setattr(google, "exchange_code", fake_exchange)
    c = app_client

    def flow(state=None, **params):
        c.cookies.clear()
        r = c.get("/api/auth/google/start", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(google.AUTH_URL)
        q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
        assert q["client_id"] == CLIENT and q["code_challenge_method"] == "S256" and "openid" in q["scope"]
        seen.update(nonce=q["nonce"], challenge=q["code_challenge"])
        args = {"code": "one-time", "state": state if state is not None else q["state"], **params}
        return c.get("/api/auth/google/callback", params=args, follow_redirects=False)

    flow.who, flow.client = who, c
    return flow


def hash_of(r):
    return r.headers["location"].split("#", 1)[1]


def me(c):
    return c.get("/api/auth/me").json()


def test_sign_up_then_sign_in_with_google(g):
    c = g.client
    r = g()
    assert hash_of(r) == "/google-finish" and "pf_session" not in r.cookies   # no account until a company is named
    assert c.get("/api/auth/google/pending").json() == {"email": "maya@gmail.com", "name": "Maya Rao"}
    assert c.post("/api/auth/google/complete", json={"company": "Maya Clicks"}).status_code == 403   # needs X-PF
    r = c.post("/api/auth/google/complete", headers={"X-PF": "1"}, json={"company": "Maya Clicks", "timezone": "Asia/Kolkata"})
    assert r.status_code == 200
    m = r.json()
    assert m["account"]["company"] == "Maya Clicks" and m["user"]["email"] == "maya@gmail.com"
    assert m["user"]["google"] is True and m["user"]["has_password"] is False and m["account"]["timezone"] == "Asia/Kolkata"
    # the pending sign-up is used up
    assert c.get("/api/auth/google/pending").status_code == 410

    r = g()                                                   # next time: straight in
    assert hash_of(r) == "/" and r.cookies.get("pf_session")
    assert me(c)["account"]["company"] == "Maya Clicks"
    assert db.q1("SELECT COUNT(*) n FROM accounts")["n"] == 1
    # a Google-only account has no password to log in with
    assert c.post("/api/auth/login", json={"email": "maya@gmail.com", "password": ""}).status_code == 401


def test_flow_is_bound_to_the_browser_and_single_use(g):
    c = g.client
    assert hash_of(g(state="forged")) == "/login?google=state"
    r = g()
    state = parse_qs(urlparse(c.get("/api/auth/google/start", follow_redirects=False).headers["location"]).query)["state"][0]
    c.cookies.clear()                                         # someone else's browser finishing my flow
    r = c.get("/api/auth/google/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert hash_of(r) == "/login?google=state"
    assert hash_of(g(error="access_denied")) == "/login?google=cancelled"


@pytest.mark.parametrize("change, code", [
    ({"email_verified": False}, "unverified"),
    ({"aud": "someone-elses-client"}, "token"),
    ({"iss": "https://evil.example"}, "token"),
    ({"exp": 1}, "token"),
])
def test_bad_id_tokens_are_refused(g, change, code):
    g.who.update(change)
    assert hash_of(g()) == f"/login?google={code}"
    assert db.q1("SELECT COUNT(*) n FROM users")["n"] == 0


def test_existing_password_account_is_linked_by_verified_email(g, make_user):
    make_user("maya@gmail.com", "Old Studio")
    r = g()
    assert hash_of(r) == "/" and me(g.client)["account"]["company"] == "Old Studio"
    assert me(g.client)["user"]["google"] is True and me(g.client)["user"]["has_password"] is True
    g.who["sub"] = "g-999"                                    # a different Google account claiming the same email
    assert hash_of(g()) == "/login?google=other_google"


def test_suspended_and_closed_sign_ups(g, make_user, monkeypatch):
    make_user("maya@gmail.com", "Sus")
    db.run("UPDATE accounts SET disabled_at=1")
    assert hash_of(g()) == "/login?google=suspended"
    g.who.update(sub="g-new", email="new@gmail.com")
    monkeypatch.setattr(config.settings, "allow_signup", False)
    assert hash_of(g()) == "/login?google=closed"


def test_google_is_off_until_configured(app_client):
    assert app_client.get("/api/auth/providers").json()["google"] is False
    assert app_client.get("/api/auth/google/start", follow_redirects=False).status_code == 404


def test_google_only_user_can_set_a_password_and_delete_the_account(g):
    c = g.client
    g()
    c.post("/api/auth/google/complete", headers={"X-PF": "1"}, json={"company": "Maya Clicks"})
    h = {"X-PF": "1"}
    assert c.post("/api/account/password", headers=h, json={"new": "a fresh password"}).status_code == 200
    assert me(c)["user"]["has_password"] is True
    assert c.post("/api/auth/login", json={"email": "maya@gmail.com", "password": "a fresh password"}).status_code == 200

    db.run("UPDATE users SET pw_hash=''")                     # back to Google-only
    assert c.post("/api/account/delete", headers=h, json={"confirm": "wrong"}).status_code == 403
    assert c.post("/api/account/delete", headers=h, json={"confirm": "Maya Clicks"}).status_code == 200
    assert db.q1("SELECT COUNT(*) n FROM accounts")["n"] == 0
