"""Storage is allocated to each company by the platform admin; plans don't carry storage; a new company gets 1 GB."""
from conftest import jpeg_bytes, upload
from studio import config, db


def admin_of(s):
    db.run("UPDATE users SET platform_admin=1 WHERE account_id=(SELECT account_id FROM users WHERE email='admin@platform.test')")
    return s


def test_plans_have_no_storage():
    assert all("storage" not in p for p in config.PLANS.values())


def test_a_new_company_gets_1_gb_and_the_admin_changes_it(make_user, app_client):
    admin = admin_of(make_user("admin@platform.test", "Platform"))
    s = make_user("new@studio.test", "New Studio", storage_gb=0)          # just as it signed up
    me = s.get("/api/auth/me").json()
    assert me["limits"]["storage"] == config.NEW_COMPANY_STORAGE == config.GB   # 1 GB, free
    cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
    eid = s.post("/api/events", json={"client_id": cid, "name": "E"}).json()["id"]
    assert upload(s, eid, [("a.jpg", jpeg_bytes(seed=1))]).json()["results"][0]["status"] == "queued"

    aid = me["account"]["id"]
    a = admin.post(f"/api/admin/accounts/{aid}/storage", json={"gb": 0}).json()   # the admin can take it away...
    assert a["storage_quota"] == 0 and a["limits"]["storage"] == 0
    r = upload(s, eid, [("b.jpg", jpeg_bytes(seed=2))]).json()["results"][0]
    assert r["status"] == "rejected" and "No storage has been allocated" in r["error"]
    a = admin.post(f"/api/admin/accounts/{aid}/storage", json={"gb": 2.5}).json()   # ...or give more
    assert a["storage_quota"] == int(2.5 * config.GB) and s.get("/api/auth/me").json()["limits"]["storage"] == int(2.5 * config.GB)
    assert upload(s, eid, [("b.jpg", jpeg_bytes(seed=2))]).json()["results"][0]["status"] == "queued"
    log = admin.get("/api/admin/log").json()
    rows = log["items"] if isinstance(log, dict) else log
    assert [x["detail"] for x in rows if x["action"] == "storage"][:2] == ["0 GB -> 2.5 GB", "1 GB -> 0 GB"]

    db.run("UPDATE accounts SET storage_quota=10 WHERE id=?", (aid,))   # less than it uses: nothing deleted, no more uploads
    r = upload(s, eid, [("c.jpg", jpeg_bytes(seed=3))]).json()["results"][0]
    assert r["status"] == "rejected" and "is full" in r["error"]

    assert s.post(f"/api/admin/accounts/{aid}/storage", json={"gb": 100}).status_code == 404   # only the platform admin
    assert admin.post(f"/api/admin/accounts/{aid}/storage", json={"gb": -1}).status_code == 422


def test_existing_companies_keep_their_plans_storage(tmp_path):
    """Migration 28: companies that existed before keep what their plan gave them."""
    import sqlite3
    from studio.db import MIGRATIONS
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, plan TEXT)")
    c.executemany("INSERT INTO accounts(plan) VALUES(?)", [("free",), ("pro",), ("studio",)])
    c.executescript(MIGRATIONS[28])
    assert [r[0] // config.GB for r in c.execute("SELECT storage_quota FROM accounts ORDER BY id")] == [1, 100, 500]
