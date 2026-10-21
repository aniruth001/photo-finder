"""Tasks board: stages and drag-and-drop order, vendors and what is owed, who may do what, history, tenant isolation."""
from studio import db
from test_inventory import member


def task(**kw):
    return {"title": "Edit highlights", "description": "", "stage": "new", "status": "on_track", "starred": False,
            "deadline": "2099-01-10", "tags": ["Post-prod"], "assignees": [], "checklist": [], **kw}


def test_create_move_and_order(studio):
    s, cid, ev = studio
    a = s.post("/api/tasks", json=task(event_id=ev["id"], tags=["Post-prod", " post-prod ", "Onsite"],
                                       checklist=[{"text": "Colour", "done": True}, {"text": "Export", "done": False}, {"text": " "}])).json()
    assert a["client"]["id"] == cid and a["event"]["name"] == "Wedding"           # the event brings its client
    assert a["tags"] == ["Post-prod", "Onsite"] and a["checked"] == 1 and len(a["checklist"]) == 2
    b = s.post("/api/tasks", json=task(title="Album", stage="planned")).json()
    c = s.post("/api/tasks", json=task(title="Deliver", stage="planned")).json()
    assert (b["position"], c["position"]) == (1, 2)
    moved = s.post(f"/api/tasks/{a['id']}/move", json={"stage": "planned", "order": [c["id"], a["id"], b["id"]]}).json()
    assert moved["stage"] == "planned"
    col = [t["title"] for t in sorted((t for t in s.get("/api/tasks").json()["tasks"] if t["stage"] == "planned"), key=lambda t: t["position"])]
    assert col == ["Deliver", "Edit highlights", "Album"]
    s.post(f"/api/tasks/{a['id']}/quick", json={"status": "blocked", "starred": True})
    s.post(f"/api/tasks/{a['id']}/comments", json={"text": "Waiting for the music"})
    log = s.get(f"/api/tasks/{a['id']}/log").json()
    assert [x["kind"] for x in log] == ["comment", "status", "moved", "created"]
    assert log[2]["text"] == "New → Planned" and log[1]["text"] == "Blocked"
    got = next(t for t in s.get("/api/tasks").json()["tasks"] if t["id"] == a["id"])
    assert got["starred"] and got["status"] == "blocked" and got["comments"] == 1
    assert s.post("/api/tasks", json=task(title=" ")).json()["error"] == "title"
    assert s.post("/api/tasks", json=task(stage="later")).json()["error"] == "stage"
    assert s.post("/api/tasks", json=task(deadline="2099-02-30")).json()["error"] == "deadline"


def test_vendor_money_and_overdue(studio, app_client):
    s, _, _ = studio
    t = s.post("/api/tasks", json=task(vendor_name="Ravi Edits", vendor_phone="+91 98450 00000",
                                       vendor_work="Edit the 4-minute teaser", vendor_amount=12000, vendor_paid=5000)).json()
    assert t["vendor"] == {"name": "Ravi Edits", "phone": "+91 98450 00000", "work": "Edit the 4-minute teaser",
                           "amount": 12000, "paid": 5000, "balance": 7000}
    s.post("/api/tasks", json=task(title="Prints", vendor_name="", vendor_amount=999))   # no vendor: no money
    db.run("UPDATE tasks SET deadline='2020-01-01'")                                       # time passes
    b = s.get("/api/tasks").json()
    assert b["summary"] == {"open": 2, "overdue": 2, "mine": 0, "vendors": 1, "vendor_balance": 7000}
    assert b["vendors"] == [{"name": "Ravi Edits", "phone": "+91 98450 00000"}]
    s.post(f"/api/tasks/{t['id']}/move", json={"stage": "done", "order": []})
    b = s.get("/api/tasks").json()
    assert b["summary"]["overdue"] == 1 and b["summary"]["vendor_balance"] == 7000      # done isn't late, but still owed
    edited = s.patch(f"/api/tasks/{t['id']}", json=task(stage="done", vendor_name="Ravi Edits", vendor_amount=12000, vendor_paid=12000)).json()
    assert edited["vendor"]["balance"] == 0
    assert s.get(f"/api/tasks/{t['id']}/log").json()[0]["text"] == "deadline, vendor's number, vendor's work, vendor payment"
    ph, _ = member(s, app_client, "ph@studio.test", "photographer")
    seen = ph.get("/api/tasks").json()
    assert "amount" not in seen["tasks"][0]["vendor"] and seen["summary"]["vendor_balance"] is None and seen["vendors"] == []


def test_who_may_do_what(studio, app_client):
    s, _, _ = studio
    ph, ph_id = member(s, app_client, "ph@studio.test", "photographer")
    vw, vw_id = member(s, app_client, "vw@studio.test", "viewer")
    mine = s.post("/api/tasks", json=task(assignees=[ph_id])).json()
    other = s.post("/api/tasks", json=task(title="Not theirs")).json()
    assert mine["assignees"] == [{"id": ph_id, "name": "Ph"}]
    assert ph.post("/api/tasks", json=task()).status_code == 403
    assert ph.patch(f"/api/tasks/{mine['id']}", json=task()).status_code == 403
    assert ph.delete(f"/api/tasks/{mine['id']}").status_code == 403
    assert ph.post(f"/api/tasks/{mine['id']}/move", json={"stage": "progress"}).json()["stage"] == "progress"
    assert ph.post(f"/api/tasks/{mine['id']}/quick", json={"checklist": [{"text": "Cull", "done": True}]}).json()["checked"] == 1
    assert ph.post(f"/api/tasks/{other['id']}/move", json={"stage": "done"}).status_code == 403
    assert ph.post(f"/api/tasks/{other['id']}/comments", json={"text": "Can I help?"}).status_code == 200
    assert ph.get("/api/tasks").json()["summary"]["mine"] == 1
    s.patch(f"/api/tasks/{other['id']}", json=task(title="Not theirs", assignees=[vw_id]))
    assert vw.post(f"/api/tasks/{other['id']}/move", json={"stage": "done"}).status_code == 403   # viewers only look
    assert vw.post(f"/api/tasks/{other['id']}/comments", json={"text": "hi"}).status_code == 403
    assert s.post("/api/tasks", json=task(assignees=[999999])).status_code == 404
    s.delete(f"/api/tasks/{other['id']}")
    assert [t["id"] for t in s.get("/api/tasks").json()["tasks"]] == [mine["id"]]


def test_companies_never_see_each_others_tasks(studio, make_user):
    s, cid, ev = studio
    t = s.post("/api/tasks", json=task()).json()
    other = make_user(email="b@studio.test", company="Studio B")
    assert other.get("/api/tasks").json()["tasks"] == []
    assert other.get(f"/api/tasks/{t['id']}/log").status_code == 404
    assert other.post(f"/api/tasks/{t['id']}/move", json={"stage": "done"}).status_code == 404
    assert other.post("/api/tasks", json=task(event_id=ev["id"])).status_code == 404
    assert other.post("/api/tasks", json=task(client_id=cid)).status_code == 404
    mine = other.post("/api/tasks", json=task(title="B's")).json()
    other.post(f"/api/tasks/{mine['id']}/move", json={"stage": "new", "order": [t["id"], mine["id"]]})   # A's id is ignored
    assert db.q1("SELECT position FROM tasks WHERE id=?", (t["id"],))["position"] == 1
    assert db.q1("SELECT position FROM tasks WHERE id=?", (mine["id"],))["position"] == 1
