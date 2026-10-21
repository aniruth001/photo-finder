"""Team members: invitations by email, roles and what each role may do, and the platform admin's view."""
import re

import pytest

from conftest import Sess, jpeg_bytes, upload, wait_idle
from studio import config, db, mailer


@pytest.fixture()
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html="": sent.append(
        {"to": to, "subject": subject, "text": text, "html": html}) or True)
    return sent


def link_token(mail):
    return re.search(r"#/join\?token=([\w-]+)", mail["text"]).group(1)


@pytest.fixture()
def team(make_user, app_client, anon, outbox, monkeypatch):
    """Owner of "Lens Co" plus a helper that invites and signs in a new employee with a role."""
    monkeypatch.setitem(config.PLANS["free"], "users", 10)
    owner = make_user("owner@lens.test", "Lens Co")

    def hire(email, role, by=None):
        r = (by or owner).post("/api/team/invites", json={"email": email, "role": role})
        assert r.status_code == 200, r.text
        tok = link_token(outbox[-1])
        r = anon.post("/api/invite/accept", json={"token": tok, "name": email.split("@")[0], "password": "employee pass"})
        assert r.status_code == 200, r.text
        return Sess(app_client, r.cookies.get("pf_session"))

    return owner, hire


def test_invite_and_join_the_company(team, anon, outbox, monkeypatch):
    owner, _ = team
    monkeypatch.setattr(config.settings, "public_url", "https://photos.example.com")
    r = owner.post("/api/team/invites", json={"email": "Ravi@Lens.test", "role": "photographer"})
    assert r.status_code == 200 and outbox[-1]["to"] == "ravi@lens.test"
    assert "Lens Co" in outbox[-1]["subject"] and "https://photos.example.com/#/join?token=" in outbox[-1]["text"]
    tok = link_token(outbox[-1])
    info = anon.post("/api/invite/check", json={"token": tok}).json()
    assert info["company"] == "Lens Co" and info["email"] == "ravi@lens.test" and info["role_label"] == "Photographer"

    r = anon.post("/api/invite/accept", json={"token": tok, "name": "Ravi", "password": "ravi's pass"})
    assert r.status_code == 200
    me = Sess(anon.c, r.cookies.get("pf_session")).get("/api/auth/me").json()
    assert me["account"]["company"] == "Lens Co" and me["user"]["role"] == "photographer"   # joined, no new company
    assert db.q1("SELECT COUNT(*) n FROM accounts")["n"] == 1
    assert anon.post("/api/invite/accept", json={"token": tok, "name": "x", "password": "another one"}).status_code == 410
    assert anon.post("/api/auth/login", json={"email": "ravi@lens.test", "password": "ravi's pass"}).status_code == 200

    t = owner.get("/api/team").json()
    assert [(m["email"], m["role"]) for m in t["members"]] == [("owner@lens.test", "owner"), ("ravi@lens.test", "photographer")]
    assert t["invites"] == [] and t["seats"]["used"] == 2


def test_email_registered_anywhere_cannot_be_invited(team, make_user, outbox):
    owner, hire = team
    make_user("taken@other.test", "Other Studio")
    r = owner.post("/api/team/invites", json={"email": "taken@other.test", "role": "viewer"})
    assert r.status_code == 409 and "already has a Photo Finder account" in r.json()["message"]
    assert owner.post("/api/team/invites", json={"email": "owner@lens.test", "role": "viewer"}).status_code == 409
    # registered after the invitation went out: accepting is refused too
    owner.post("/api/team/invites", json={"email": "late@x.test", "role": "viewer"})
    tok = link_token(outbox[-1])
    make_user("late@x.test", "Late Studio")
    r = owner.c.post("/api/invite/accept", json={"token": tok, "name": "L", "password": "longenough"})
    assert r.status_code == 409
    assert owner.post("/api/team/invites", json={"email": "bad email", "role": "viewer"}).status_code == 422


@pytest.mark.parametrize("role, allowed", [
    ("admin", {"client", "event", "upload", "delete_photo", "company", "team"}),
    ("manager", {"client", "event", "upload", "delete_photo"}),
    ("photographer", {"upload"}),
    ("viewer", set()),
])
def test_what_each_role_can_do(team, role, allowed):
    owner, hire = team
    cid = owner.post("/api/clients", json={"name": "C"}).json()["id"]
    eid = owner.post("/api/events", json={"client_id": cid, "name": "E"}).json()["id"]
    upload(owner, eid, [("o.jpg", jpeg_bytes(seed=201))])
    wait_idle(owner, eid)
    pid = owner.get(f"/api/events/{eid}/photos").json()[0]["id"]
    s = hire(f"{role}@lens.test", role)
    # everyone in the company can see its work
    assert s.get("/api/events").status_code == 200 and s.get(f"/api/events/{eid}/photos").status_code == 200
    assert s.get(f"/api/events/{eid}/qr.png").status_code == 200
    checks = {
        "client": lambda: s.post("/api/clients", json={"name": "New"}),
        "event": lambda: s.patch(f"/api/events/{eid}", json={"name": "Renamed"}),
        "upload": lambda: upload(s, eid, [(f"{role}.jpg", jpeg_bytes(seed=hash(role) % 1000))]),
        "delete_photo": lambda: s.post(f"/api/events/{eid}/photos/delete", json={"ids": [pid]}),
        "company": lambda: s.patch("/api/account", json={"brand_color": "#123456"}),
        "team": lambda: s.post("/api/team/invites", json={"email": f"x-{role}@lens.test", "role": "viewer"}),
    }
    for what, call in checks.items():
        code = call().status_code
        assert (code == 200) == (what in allowed), f"{role} {what} -> {code}"
    assert s.post("/api/account/delete", json={"password": "employee pass"}).status_code == 403   # owner only
    assert s.patch("/api/account", json={"name": "My own name"}).status_code == 200            # own name: anyone


def test_who_can_manage_whom(team):
    owner, hire = team
    admin = hire("admin@lens.test", "admin")
    t = admin.get("/api/team").json()
    assert t["assignable"] == ["manager", "photographer", "viewer"]          # an admin can't make admins
    assert admin.post("/api/team/invites", json={"email": "a2@lens.test", "role": "admin"}).status_code == 403
    viewer = hire("v@lens.test", "viewer", by=admin)
    ids = {m["email"]: m["id"] for m in owner.get("/api/team").json()["members"]}
    assert admin.patch(f"/api/team/members/{ids['owner@lens.test']}", json={"role": "viewer"}).status_code == 403
    assert admin.delete(f"/api/team/members/{ids['owner@lens.test']}").status_code == 403
    assert admin.patch(f"/api/team/members/{ids['admin@lens.test']}", json={"role": "viewer"}).status_code == 409  # self
    assert admin.patch(f"/api/team/members/{ids['v@lens.test']}", json={"role": "manager"}).status_code == 200
    assert viewer.get("/api/auth/me").json()["user"]["role"] == "manager"        # takes effect straight away
    assert owner.patch(f"/api/team/members/{ids['admin@lens.test']}", json={"role": "owner"}).status_code == 403
    assert owner.delete(f"/api/team/members/{ids['v@lens.test']}").status_code == 200
    assert viewer.get("/api/auth/me").status_code == 401                         # removed: signed out everywhere


def test_another_company_cannot_touch_my_team(team, make_user):
    owner, hire = team
    hire("emp@lens.test", "viewer")
    other = make_user("boss@other.test", "Other")
    lens = owner.get("/api/team").json()
    emp = next(m for m in lens["members"] if m["email"] == "emp@lens.test")
    assert other.patch(f"/api/team/members/{emp['id']}", json={"role": "admin"}).status_code == 404
    assert other.delete(f"/api/team/members/{emp['id']}").status_code == 404
    assert [m["email"] for m in other.get("/api/team").json()["members"]] == ["boss@other.test"]


def test_seat_limit_resend_and_cancel(team, outbox, anon, monkeypatch):
    owner, _ = team
    monkeypatch.setitem(config.PLANS["free"], "users", 2)
    assert owner.post("/api/team/invites", json={"email": "one@lens.test", "role": "viewer"}).status_code == 200
    r = owner.post("/api/team/invites", json={"email": "two@lens.test", "role": "viewer"})
    assert r.status_code == 403 and r.json()["error"] == "plan_limit"        # the pending invite holds a seat
    first = link_token(outbox[-1])
    inv = owner.get("/api/team").json()["invites"][0]
    assert owner.post(f"/api/team/invites/{inv['id']}/resend").status_code == 200
    assert anon.post("/api/invite/check", json={"token": first}).status_code == 410     # old link replaced
    assert owner.delete(f"/api/team/invites/{inv['id']}").status_code == 200
    assert anon.post("/api/invite/check", json={"token": link_token(outbox[-1])}).status_code == 410
    assert owner.post("/api/team/invites", json={"email": "two@lens.test", "role": "viewer"}).status_code == 200


def test_suspended_company_invites_cannot_be_used(team, outbox, anon):
    owner, _ = team
    owner.post("/api/team/invites", json={"email": "s@lens.test", "role": "viewer"})
    db.run("UPDATE accounts SET disabled_at=1")
    assert anon.post("/api/invite/accept", json={"token": link_token(outbox[-1]), "name": "S",
                                                 "password": "longenough"}).status_code == 403


def test_platform_admin_sees_each_companys_team(team, make_user):
    owner, hire = team
    hire("e1@lens.test", "manager")
    owner.post("/api/team/invites", json={"email": "e2@lens.test", "role": "viewer"})
    pa = make_user("pa@platform.test", "Platform")
    db.run("UPDATE users SET platform_admin=1 WHERE email='pa@platform.test'")
    lens = next(a for a in pa.get("/api/admin/accounts").json()["items"] if a["company"] == "Lens Co")
    assert lens["users"] == 2 and lens["invites"] == 1
    d = pa.get(f"/api/admin/accounts/{lens['id']}").json()
    assert {(m["email"], m["role_label"]) for m in d["members"]} == {("owner@lens.test", "Owner"), ("e1@lens.test", "Manager")}
    assert [(i["email"], i["role_label"]) for i in d["pending_invites"]] == [("e2@lens.test", "Viewer")]
    assert "invites" in pa.get("/api/admin/accounts.csv").text.splitlines()[0]
    assert pa.get("/api/admin/overview").json()["users"] == 3                  # people, not pending invites


def test_join_with_google_must_use_the_invited_address(team, outbox, app_client, monkeypatch):
    import base64, hashlib, json, time
    from urllib.parse import parse_qs, urlparse
    from studio import google

    owner, _ = team
    owner.post("/api/team/invites", json={"email": "g@lens.test", "role": "manager"})
    tok = link_token(outbox[-1])
    monkeypatch.setattr(config.settings, "google_client_id", "cid")
    monkeypatch.setattr(config.settings, "google_client_secret", "sec")
    who = {"email": "someone.else@gmail.com"}

    def flow():
        c = app_client
        c.cookies.clear()
        q = parse_qs(urlparse(c.get(f"/api/auth/google/start?invite={tok}", follow_redirects=False).headers["location"]).query)
        claims = {"iss": "accounts.google.com", "aud": "cid", "exp": time.time() + 60, "nonce": q["nonce"][0],
                  "sub": "g-1", "email_verified": True, "name": "G", **who}
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
        monkeypatch.setattr(google, "exchange_code", lambda *a: {"id_token": f"x.{body}.y"})
        return c.get("/api/auth/google/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)

    assert flow().headers["location"].endswith("#/join?google=wrong_email")
    who["email"] = "g@lens.test"
    r = flow()
    assert r.headers["location"].endswith("#/") and r.cookies.get("pf_session")
    me = Sess(app_client, r.cookies.get("pf_session")).get("/api/auth/me").json()
    assert me["account"]["company"] == "Lens Co" and me["user"]["role"] == "manager" and me["user"]["google"] is True
