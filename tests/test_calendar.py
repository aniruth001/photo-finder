"""Calendar bookings: details, Own client / Expost, payment status, month view, roles, other companies."""
from studio import db


def bk(**kw):
    return {"title": "Wedding: Arun & Anjali", "date": "2026-11-02", "source": "own", "client_name": "Arun",
            "description": "Candid + traditional, 2 photographers", "amount": 150000, "paid": 50000, **kw}


def test_add_edit_delete_and_payment_status(studio):
    s, cid, _ = studio
    b = s.post("/api/calendar", json=bk(client_id=cid, start_time="18:00", end_time="23:30", location="Chennai")).json()
    assert (b["source_label"], b["balance"], b["payment_status"], b["client_id"]) == ("Own client", 100000, "part", cid)
    assert s.post("/api/calendar", json=bk(amount=0, paid=0)).json()["payment_status"] == "not_finalised"
    assert s.post("/api/calendar", json=bk(paid=0)).json()["payment_status"] == "unpaid"
    up = s.patch(f"/api/calendar/{b['id']}", json=bk(source="expost", paid=150000, client_name="Lumen Studio")).json()
    assert (up["source_label"], up["payment_status"], up["balance"]) == ("Expost", "paid", 0)
    assert s.delete(f"/api/calendar/{b['id']}").json() == {"ok": True}
    assert s.patch(f"/api/calendar/{b['id']}", json=bk()).status_code == 404


def test_month_view_includes_ranges_events_and_totals(studio):
    s, cid, ev = studio                                                             # the studio's event is on 2026-11-20
    s.post("/api/calendar", json=bk(date="2026-10-30", end_date="2026-11-01", title="Destination wedding"))   # spans into Nov
    s.post("/api/calendar", json=bk(date="2026-11-15", source="expost", amount=40000, paid=40000))
    s.post("/api/calendar", json=bk(date="2026-12-05"))                             # another month
    m = s.get("/api/calendar?start=2026-11-01&end=2026-11-30").json()
    assert [b["title"] for b in m["bookings"]] == ["Destination wedding", "Wedding: Arun & Anjali"]
    assert [e["name"] for e in m["events"]] == ["Wedding"]
    assert m["totals"] == {"bookings": 2, "own": 1, "expost": 1, "amount": 190000, "paid": 90000, "balance": 100000}
    assert s.get("/api/calendar?start=2026-11-01&end=2027-03-01").status_code == 422   # at most two months at a time


def test_validation(studio):
    s, _, _ = studio
    for body, code in ((bk(title=" "), "title"), (bk(date="2026-02-30"), "date"), (bk(end_date="2026-11-01"), "end_date"),
                       (bk(end_date="2027-03-01"), "end_date"), (bk(start_time="25:00"), "time"),
                       (bk(start_time="20:00", end_time="18:00"), "time"), (bk(source="other"), "source"), (bk(client_id=999), "not_found")):
        r = s.post("/api/calendar", json=body)
        assert r.json()["error"] == code, (code, r.json())
    assert s.post("/api/calendar", json=bk(amount=-5)).status_code == 422
    one_day = s.post("/api/calendar", json=bk(end_date="2026-11-02")).json()
    assert one_day["end_date"] is None                                               # same day: not a range


def test_roles_and_other_companies(studio, make_user):
    s, _, _ = studio
    b = s.post("/api/calendar", json=bk()).json()
    other = make_user("x@other.test", "Other")
    assert other.get("/api/calendar?start=2026-11-01&end=2026-11-30").json()["bookings"] == []
    assert other.patch(f"/api/calendar/{b['id']}", json=bk()).status_code == 404
    assert other.delete(f"/api/calendar/{b['id']}").status_code == 404
    me = s.get("/api/auth/me").json()["user"]["email"]
    db.run("UPDATE users SET role='photographer' WHERE email=?", (me,))
    assert len(s.get("/api/calendar?start=2026-11-01&end=2026-11-30").json()["bookings"]) == 1   # everyone sees it
    assert s.post("/api/calendar", json=bk()).status_code == 403
    assert s.patch(f"/api/calendar/{b['id']}", json=bk()).status_code == 403
