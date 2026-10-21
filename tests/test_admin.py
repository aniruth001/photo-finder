"""Product-owner dashboard: access control, company list and stats, suspend/enable, plan changes, audit log."""
import pytest

from conftest import Sess, jpeg_bytes, upload, wait_idle
from studio import db


@pytest.fixture()
def admin(make_user):
    s = make_user("owner@platform.test", "Platform HQ")
    db.run("UPDATE users SET platform_admin=1 WHERE email=?", ("owner@platform.test",))
    return s


def acct_id(sess):
    return sess.get("/api/auth/me").json()["account"]["id"]


def test_only_platform_admins_get_in(make_user, admin, anon):
    s = make_user("studio@x.test", "Lens Co")
    assert anon.get("/api/admin/overview").status_code == 401
    for url in ("/api/admin/overview", "/api/admin/accounts", "/api/admin/accounts.csv", "/api/admin/log",
                f"/api/admin/accounts/{acct_id(s)}"):
        assert s.get(url).status_code == 404, url              # looks like it doesn't exist
    assert s.post(f"/api/admin/accounts/{acct_id(s)}/plan", json={"plan": "studio"}).status_code == 404
    assert s.get("/api/auth/me").json()["account"]["plan"] == "free"
    assert admin.get("/api/admin/overview").status_code == 200
    assert admin.get("/api/auth/me").json()["user"]["platform_admin"] is True
    assert s.get("/api/auth/me").json()["user"]["platform_admin"] is False


def test_overview_and_company_list(make_user, admin):
    a = make_user("a@x.test", "Alpha Studio")
    make_user("b@x.test", "Beta Photos")
    cid = a.post("/api/clients", json={"name": "C"}).json()["id"]
    ev = a.post("/api/events", json={"client_id": cid, "name": "Wedding"}).json()
    upload(a, ev["id"], [("p.jpg", jpeg_bytes(seed=5))])
    wait_idle(a, ev["id"])
    admin.post(f"/api/admin/accounts/{acct_id(a)}/plan", json={"plan": "pro"})

    o = admin.get("/api/admin/overview").json()
    assert o["companies"]["total"] == 3 and o["companies"]["active"] == 3 and o["companies"]["new_30d"] == 3
    assert o["companies"]["active_30d"] == 3 and o["companies"]["paying"] == 1
    assert o["revenue_month"] == 1999 and o["photos"] == 1 and o["storage"] > 0 and o["events"] == 1
    assert {p["plan"]: p["accounts"] for p in o["plans"]} == {"free": 2, "pro": 1, "studio": 0}
    assert len(o["signups_by_month"]) == 12 and o["signups_by_month"][-1]["count"] == 3
    assert o["top_storage"][0]["company"] == "Alpha Studio"

    r = admin.get("/api/admin/accounts?sort=storage").json()
    assert r["total"] == 3 and r["items"][0]["company"] == "Alpha Studio"
    assert r["items"][0]["photos"] == 1 and r["items"][0]["plan_label"] == "Pro"
    assert [x["company"] for x in admin.get("/api/admin/accounts?q=beta").json()["items"]] == ["Beta Photos"]
    assert [x["company"] for x in admin.get("/api/admin/accounts?plan=pro").json()["items"]] == ["Alpha Studio"]
    assert [x["company"] for x in admin.get("/api/admin/accounts?sort=company&order=asc").json()["items"]] == \
        ["Alpha Studio", "Beta Photos", "Platform HQ"]

    d = admin.get(f"/api/admin/accounts/{acct_id(a)}").json()
    assert d["members"][0]["email"] == "a@x.test" and d["event_list"][0]["name"] == "Wedding"
    assert d["log"][0]["action"] == "plan" and d["log"][0]["detail"] == "Free -> Pro"


def test_suspend_and_enable_a_company(make_user, admin, anon, app_client):
    s = make_user("sus@x.test", "Sus Studio", password="correct horse")
    aid = acct_id(s)
    cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
    ev = s.post("/api/events", json={"client_id": cid, "name": "Party"}).json()
    s.patch(f"/api/events/{ev['id']}", json={"status": "published"})
    assert anon.get(f"/e/{ev['slug']}").status_code == 200

    r = admin.post(f"/api/admin/accounts/{aid}/disable", json={"reason": "Payment overdue"})
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    assert s.get("/api/auth/me").status_code == 401                       # sessions were removed
    login = anon.post("/api/auth/login", json={"email": "sus@x.test", "password": "correct horse"})
    assert login.status_code == 403 and login.json()["error"] == "suspended"
    assert anon.post("/api/auth/login", json={"email": "sus@x.test", "password": "wrong pass"}).status_code == 401
    assert anon.get(f"/e/{ev['slug']}").status_code == 404                # guest links go offline
    assert admin.post(f"/api/admin/accounts/{aid}/disable", json={}).status_code == 409
    assert admin.get("/api/admin/overview").json()["companies"]["suspended"] == 1

    # a session that somehow survives (e.g. created before the suspension) is refused too
    db.run("UPDATE accounts SET disabled_at=NULL WHERE id=?", (aid,))
    tok = anon.post("/api/auth/login", json={"email": "sus@x.test", "password": "correct horse"}).cookies.get("pf_session")
    db.run("UPDATE accounts SET disabled_at=1 WHERE id=?", (aid,))
    assert Sess(app_client, tok).get("/api/clients").status_code == 403

    assert admin.post(f"/api/admin/accounts/{aid}/enable").json()["status"] == "active"
    assert anon.post("/api/auth/login", json={"email": "sus@x.test", "password": "correct horse"}).status_code == 200
    assert anon.get(f"/e/{ev['slug']}").status_code == 200                # nothing was deleted
    actions = [l["action"] for l in admin.get("/api/admin/log").json()]
    assert actions[:2] == ["enable", "suspend"]


def test_admin_cannot_suspend_own_company_and_keeps_own_session(admin):
    me = acct_id(admin)
    assert admin.post(f"/api/admin/accounts/{me}/disable", json={}).status_code == 409
    admin.post(f"/api/admin/accounts/{me}/signout")
    assert admin.get("/api/auth/me").status_code == 200


def test_plan_change_validation_and_csv_export(make_user, admin):
    s = make_user("csv@x.test", "=HYPERLINK(\"http://evil\")")
    aid = acct_id(s)
    assert admin.post(f"/api/admin/accounts/{aid}/plan", json={"plan": "gold"}).status_code == 422
    assert admin.post(f"/api/admin/accounts/{aid}/plan", json={"plan": "studio"}).json()["plan"] == "studio"
    assert s.get("/api/auth/me").json()["limits"]["photos"] == 250_000    # the new limits apply straight away
    r = admin.get("/api/admin/accounts.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK" in r.text and "\n=HYPERLINK" not in r.text and ",=HYPERLINK" not in r.text
    assert admin.get(f"/api/admin/accounts/999999").status_code == 404


def test_admin_page_is_served(app_client):
    r = app_client.get("/admin")
    assert r.status_code == 200 and "Platform admin" in r.text
