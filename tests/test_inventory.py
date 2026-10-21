"""Equipment inventory: items, check-out and return, who may do what, overdue, history, tenant isolation."""
from studio import db


def item(**kw):
    return {"name": "Sony A7 IV", "category": "Camera body", "brand": "Sony", "serial": "S01234", "quantity": 1,
            "purchase_date": "2025-03-01", "purchase_cost": 215000, "notes": "", **kw}


def member(s, app_client, email, role):
    """Add a team member with `role` directly (the invitation flow has its own tests)."""
    acct = s.get("/api/auth/me").json()["account"]["id"]
    from studio import security
    db.ex("INSERT INTO users(account_id, email, name, pw_hash, role, created_at) VALUES(?,?,?,?,?,0)",
          (acct, email, email.split("@")[0].title(), security.hash_password("longenough1"), role))
    r = app_client.post("/api/auth/login", headers={"X-PF": "1"}, json={"email": email, "password": "longenough1"})
    from conftest import Sess
    return Sess(app_client, r.cookies.get("pf_session")), db.q1("SELECT id FROM users WHERE email=?", (email,))["id"]


def test_add_edit_checkout_return_history(studio, app_client):
    s, _, ev = studio
    cam = s.post("/api/inventory", json=item()).json()
    assert cam["status"] == "available" and cam["purchase_cost"] == 215000
    s.post("/api/inventory", json=item(name="SD cards 128 GB", category="Memory card", quantity=10, purchase_cost=1500, serial=""))
    ph, ph_id = member(s, app_client, "ravi@studio.test", "photographer")
    out = s.post(f"/api/inventory/{cam['id']}/checkout", json={"user_id": ph_id, "event_id": ev["id"], "due_back": "2099-01-01", "note": "for the wedding"}).json()
    assert out["status"] == "out" and out["holder"]["name"] == "Ravi" and out["event"]["name"] == "Wedding" and not out["overdue"]
    assert s.post(f"/api/inventory/{cam['id']}/checkout", json={}).json()["error"] == "unavailable"   # nobody takes it twice
    inv = s.get("/api/inventory").json()
    assert inv["summary"] == {"items": 2, "units": 11, "available": 1, "out": 1, "overdue": 0, "maintenance": 0, "value": 215000 + 15000}
    assert ph.post(f"/api/inventory/{cam['id']}/return", json={"note": "all fine"}).json()["status"] == "available"   # the holder returns it
    s.patch(f"/api/inventory/{cam['id']}", json=item(serial="S09999"))
    h = s.get(f"/api/inventory/{cam['id']}/history").json()
    assert [x["action"] for x in h] == ["edited", "returned", "checked_out", "added"]
    assert h[0]["detail"] == "serial" and h[1]["detail"] == "all fine" and h[2]["person"] == "Ravi" and h[2]["event"] == "Wedding"


def test_overdue_maintenance_and_retired(studio):
    s, _, _ = studio
    i = s.post("/api/inventory", json=item()).json()
    assert s.post(f"/api/inventory/{i['id']}/checkout", json={"due_back": "2020-01-01"}).json()["error"] == "due_back"
    s.post(f"/api/inventory/{i['id']}/checkout", json={"due_back": "2099-12-31"})
    db.run("UPDATE inventory SET due_back='2020-01-01'")                            # time passes
    got = s.get("/api/inventory").json()
    assert got["items"][0]["overdue"] and got["summary"]["overdue"] == 1
    assert s.post(f"/api/inventory/{i['id']}/status", json={"status": "maintenance"}).json()["error"] == "is_out"
    s.post(f"/api/inventory/{i['id']}/return", json={})
    assert s.post(f"/api/inventory/{i['id']}/status", json={"status": "maintenance", "note": "sensor clean"}).json()["status"] == "maintenance"
    assert s.post(f"/api/inventory/{i['id']}/checkout", json={}).json()["message"] == "It's in maintenance."
    s.post(f"/api/inventory/{i['id']}/status", json={"status": "retired"})
    assert s.get("/api/inventory").json()["summary"]["items"] == 0                 # retired items don't count
    assert s.post(f"/api/inventory/{i['id']}/status", json={"status": "out"}).status_code == 422


def test_who_may_do_what(studio, app_client):
    s, _, _ = studio
    a = s.post("/api/inventory", json=item()).json()
    b = s.post("/api/inventory", json=item(name="Godox AD200", category="Flash & lighting")).json()
    ph, ph_id = member(s, app_client, "ph@studio.test", "photographer")
    vw, _ = member(s, app_client, "vw@studio.test", "viewer")
    mg, _ = member(s, app_client, "mg@studio.test", "manager")
    owner_id = db.q1("SELECT id FROM users WHERE role='owner'")["id"]
    assert ph.post("/api/inventory", json=item()).status_code == 403                # photographers don't add or edit
    assert ph.patch(f"/api/inventory/{a['id']}", json=item()).status_code == 403
    assert ph.post(f"/api/inventory/{a['id']}/checkout", json={"user_id": owner_id}).status_code == 403   # only to themselves
    assert ph.post(f"/api/inventory/{a['id']}/checkout", json={}).json()["holder"]["id"] == ph_id
    s.post(f"/api/inventory/{b['id']}/checkout", json={})                            # the owner has b
    assert ph.post(f"/api/inventory/{b['id']}/return", json={}).status_code == 403  # not theirs
    assert vw.get("/api/inventory").status_code == 200 and not vw.get("/api/inventory").json()["can_use"]
    assert vw.post(f"/api/inventory/{b['id']}/return", json={}).status_code == 403
    assert vw.get("/api/inventory.csv").status_code == 200 and "Purchase cost" not in vw.get("/api/inventory.csv").text
    seen = vw.get("/api/inventory").json()
    assert seen["summary"]["value"] is None and all(i["purchase_cost"] is None for i in seen["items"])   # costs: managers only
    assert "Purchase cost" in s.get("/api/inventory.csv").text
    assert mg.post(f"/api/inventory/{b['id']}/return", json={}).status_code == 200  # managers can check anything in
    assert mg.post("/api/inventory", json=item(name="Rode mic", category="Audio")).status_code == 200
    assert mg.delete(f"/api/inventory/{b['id']}").json() == {"ok": True}


def test_other_companies_and_validation(studio, make_user):
    s, _, ev = studio
    a = s.post("/api/inventory", json=item()).json()
    other = make_user("x@other.test", "Other")
    assert other.get("/api/inventory").json()["items"] == []
    for call in (lambda: other.patch(f"/api/inventory/{a['id']}", json=item()), lambda: other.get(f"/api/inventory/{a['id']}/history"),
                 lambda: other.post(f"/api/inventory/{a['id']}/checkout", json={}), lambda: other.delete(f"/api/inventory/{a['id']}")):
        assert call().status_code == 404
    o_ev = other.post("/api/events", json={"client_id": other.post("/api/clients", json={"name": "C"}).json()["id"], "name": "E"}).json()
    assert s.post(f"/api/inventory/{a['id']}/checkout", json={"event_id": o_ev["id"]}).status_code == 404   # not their event
    assert s.post("/api/inventory", json=item(name="  ")).json()["error"] == "name"
    assert s.post("/api/inventory", json=item(category="Spaceship")).json()["error"] == "category"
    assert s.post("/api/inventory", json=item(purchase_date="2025-02-30")).json()["error"] == "purchase_date"


def test_removed_people_and_deleted_events_keep_the_history(studio, app_client):
    s, _, ev = studio
    a = s.post("/api/inventory", json=item()).json()
    ph, ph_id = member(s, app_client, "gone@studio.test", "photographer")
    s.post(f"/api/inventory/{a['id']}/checkout", json={"user_id": ph_id, "event_id": ev["id"]})
    s.delete(f"/api/team/members/{ph_id}")
    s.delete(f"/api/events/{ev['id']}")
    got = s.get("/api/inventory").json()["items"][0]
    assert got["status"] == "out" and got["holder"] is None and got["event"] is None   # still out: someone must bring it back
    assert s.post(f"/api/inventory/{a['id']}/return", json={"note": "collected"}).json()["status"] == "available"
    h = s.get(f"/api/inventory/{a['id']}/history").json()
    assert h[1]["person"] == "Gone" and h[1]["event"] == "Wedding"


def test_csv_export(studio):
    s, _, _ = studio
    s.post("/api/inventory", json=item(name="=HYPERLINK(1)", notes="x"))
    csv = s.get("/api/inventory.csv").text
    assert csv.startswith("﻿Name,Category") and "'=HYPERLINK(1)" in csv and "Available" in csv
