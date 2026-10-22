"""Forgot password: the email, the one-time link, and everything that must not work."""
import re
import time

import pytest

from conftest import Sess
from studio import config, db, mailer


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html="": sent.append(
        {"to": to, "subject": subject, "text": text, "html": html}) or True)
    return sent


def token_of(mail):
    return re.search(r"#/reset\?token=([\w-]+)", mail["text"]).group(1)


def test_reset_flow(make_user, anon, app_client, outbox, monkeypatch):
    monkeypatch.setattr(config.settings, "public_url", "https://photos.example.com")
    old = make_user("forgot@x.test", "Lens Co")                       # a signed-in device
    r = anon.post("/api/auth/forgot", json={"email": "Forgot@X.test"})
    assert r.status_code == 200 and "If an account exists" in r.json()["message"]
    assert len(outbox) == 1 and outbox[0]["to"] == "forgot@x.test" and "Lens Co" in outbox[0]["text"]
    assert "https://photos.example.com/#/reset?token=" in outbox[0]["text"] and "Choose a new password" in outbox[0]["html"]
    tok = token_of(outbox[0])
    assert db.q1("SELECT COUNT(*) n FROM password_resets WHERE token_hash=?", (tok,))["n"] == 0   # only the hash is kept

    assert anon.post("/api/auth/reset/check", json={"token": tok}).json() == {"email": "f***@x.test"}
    assert anon.post("/api/auth/reset", json={"token": tok, "password": "short"}).status_code == 422
    r = anon.post("/api/auth/reset", json={"token": tok, "password": "a brand new one"})
    assert r.status_code == 200 and r.cookies.get("pf_session")             # signed in straight away
    assert Sess(app_client, r.cookies.get("pf_session")).get("/api/auth/me").status_code == 200
    assert old.get("/api/auth/me").status_code == 401                      # every other device is signed out
    assert anon.post("/api/auth/login", json={"email": "forgot@x.test", "password": "correct horse"}).status_code == 401
    assert anon.post("/api/auth/login", json={"email": "forgot@x.test", "password": "a brand new one"}).status_code == 200
    # the link works once
    assert anon.post("/api/auth/reset", json={"token": tok, "password": "another one!"}).status_code == 410
    assert anon.post("/api/auth/reset/check", json={"token": tok}).status_code == 410


def test_unknown_email_gets_the_same_answer_and_no_mail(make_user, anon, outbox):
    make_user("real@x.test")
    known = anon.post("/api/auth/forgot", json={"email": "real@x.test"}).json()["message"]
    unknown = anon.post("/api/auth/forgot", json={"email": "nobody@x.test"}).json()["message"]
    assert known.replace("real@x.test", "E") == unknown.replace("nobody@x.test", "E")
    assert [m["to"] for m in outbox] == ["real@x.test"]
    assert anon.post("/api/auth/forgot", json={"email": "not an email"}).status_code == 422


def test_links_expire_and_a_new_request_replaces_the_old_link(make_user, anon, outbox):
    make_user("exp@x.test")
    anon.post("/api/auth/forgot", json={"email": "exp@x.test"})
    anon.post("/api/auth/forgot", json={"email": "exp@x.test"})
    first, second = token_of(outbox[0]), token_of(outbox[1])
    assert anon.post("/api/auth/reset/check", json={"token": first}).status_code == 410   # replaced
    db.run("UPDATE password_resets SET expires_at=?", (int(time.time()) - 1,))
    assert anon.post("/api/auth/reset", json={"token": second, "password": "a brand new one"}).status_code == 410
    assert anon.post("/api/auth/reset", json={"token": "made-up", "password": "a brand new one"}).status_code == 410


def test_inbox_flooding_and_guessing_are_limited(make_user, anon, outbox):
    make_user("flood@x.test")
    codes = [anon.post("/api/auth/forgot", json={"email": "flood@x.test"}).status_code for _ in range(5)]
    assert codes == [200] * 5 and len(outbox) == 3                         # quietly capped at 3 emails an hour
    codes = [anon.post("/api/auth/forgot", json={"email": f"x{i}@x.test"}).status_code for i in range(6)]
    assert codes[-1] == 429                                                 # 10 an hour per network
    codes = [anon.post("/api/auth/reset", json={"token": f"guess{i}", "password": "whatever123"}).status_code
             for i in range(21)]
    assert codes[:20] == [410] * 20 and codes[20] == 429


def test_suspended_accounts_get_no_link(make_user, anon, outbox):
    make_user("sus@x.test")
    anon.post("/api/auth/forgot", json={"email": "sus@x.test"})
    db.run("UPDATE accounts SET disabled_at=1")
    assert anon.post("/api/auth/reset", json={"token": token_of(outbox[0]), "password": "a brand new one"}).status_code == 403
    anon.post("/api/auth/forgot", json={"email": "sus@x.test"})
    assert len(outbox) == 1


def test_console_fallback_without_smtp(capsys, monkeypatch):
    monkeypatch.setattr(config.settings, "smtp_host", "")
    assert mailer.send("a@x.test", "Hello", "the body") is False
    out = capsys.readouterr().out
    assert "To: a@x.test" in out and "the body" in out


def test_a_forged_host_header_never_reaches_the_email(make_user, anon, outbox, monkeypatch):
    """With no public URL set, the link uses the address the server answers on, not the request's Host header:
    otherwise anyone could ask for a victim's reset and receive the token on their own site."""
    make_user(email="victim@studio.test")
    monkeypatch.setattr(config.settings, "public_url", "")
    anon.post("/api/auth/forgot", json={"email": "victim@studio.test"}, headers={"Host": "evil.example"})
    assert len(outbox) == 1
    assert "evil.example" not in outbox[0]["text"] + outbox[0]["html"]
    assert "http://testserver/#/reset?token=" in outbox[0]["text"]
