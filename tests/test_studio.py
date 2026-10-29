"""API tests: accounts, tenant isolation, plan limits, uploads, guest access rules."""
import re

import pytest

from conftest import jpeg_bytes, upload, wait_idle
from studio import config, db


# ------------------------------------------------------------------------------------------ accounts
def test_signup_login_logout(app_client, make_user, anon):
    s = make_user("owner@x.test", "Lens Co")
    me = s.get("/api/auth/me").json()
    assert me["account"]["company"] == "Lens Co" and me["account"]["plan"] == "free"
    assert me["limits"]["events"] == 2 and me["usage"] == {"events": 0, "photos": 0, "storage": 0, "users": 1}
    assert me["user"]["role"] == "owner" and "delete_account" in me["user"]["permissions"]

    assert s.post("/api/auth/logout").status_code == 200
    assert s.get("/api/auth/me").status_code == 401  # the session is gone server-side, not just in the cookie

    bad = anon.post("/api/auth/login", json={"email": "owner@x.test", "password": "wrong password"})
    assert bad.status_code == 401
    ok = anon.post("/api/auth/login", json={"email": "OWNER@x.test", "password": "correct horse"})
    assert ok.status_code == 200 and "pf_session" in ok.cookies


def test_signup_validation(app_client, make_user, anon):
    make_user("dup@x.test")
    assert anon.post("/api/auth/signup", json={"company": "B", "email": "dup@x.test", "password": "longenough1"}).status_code == 409
    assert anon.post("/api/auth/signup", json={"company": "B", "email": "not-an-email", "password": "longenough1"}).status_code == 422
    assert anon.post("/api/auth/signup", json={"company": "B", "email": "b@x.test", "password": "short"}).status_code == 422


def test_signup_can_be_closed(app_client, monkeypatch, anon):
    monkeypatch.setattr(config.settings, "allow_signup", False)
    r = anon.post("/api/auth/signup", json={"company": "B", "email": "b@x.test", "password": "longenough1"})
    assert r.status_code == 403


def test_login_rate_limit(app_client, make_user, anon):
    make_user("r@x.test")
    codes = [anon.post("/api/auth/login", json={"email": "r@x.test", "password": "nope nope"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_requires_login_and_csrf_header(app_client, make_user, anon):
    assert anon.get("/api/clients").status_code == 401
    s = make_user()
    # a state-changing request without the custom header is refused even with a valid session cookie
    s.c.cookies.set("pf_session", s.token)
    r = s.c.post("/api/clients", json={"name": "x"})
    assert r.status_code == 403
    assert s.post("/api/clients", json={"name": "x"}).status_code == 200


def test_change_password_signs_out_other_devices(app_client, make_user, anon):
    s = make_user("p@x.test")
    other = anon.post("/api/auth/login", json={"email": "p@x.test", "password": "correct horse"})
    from conftest import Sess
    s2 = Sess(app_client, other.cookies.get("pf_session"))
    assert s2.get("/api/auth/me").status_code == 200
    assert s.post("/api/account/password", json={"current": "bad", "new": "brand new pass"}).status_code == 403
    assert s.post("/api/account/password", json={"current": "correct horse", "new": "brand new pass"}).status_code == 200
    assert s.get("/api/auth/me").status_code == 200       # this device stays signed in
    assert s2.get("/api/auth/me").status_code == 401      # the other one is signed out


def test_brand_color_validation(make_user):
    s = make_user()
    assert s.patch("/api/account", json={"brand_color": "red"}).status_code == 422
    r = s.patch("/api/account", json={"brand_color": "#112233", "company": "New Name"})
    assert r.status_code == 200 and r.json()["account"]["brand_color"] == "#112233"


# ------------------------------------------------------------------------------------------ clients + events
def test_client_and_event_lifecycle(studio):
    s, cid, ev = studio
    assert re.fullmatch(r"wedding-[a-z0-9]{8}", ev["slug"]) and ev["status"] == "draft"
    assert s.get("/api/clients").json()[0]["event_count"] == 1
    # a client with events can't be deleted
    assert s.delete(f"/api/clients/{cid}").status_code == 409
    assert ev["face_model"] == "sface" and ev["tolerance"] == 0.95          # new events use the accurate engine
    assert [t[0] for t in ev["tolerance_presets"]] == [0.92, 0.95, 1.0]
    r = s.patch(f"/api/events/{ev['id']}", json={"name": "Wedding Day", "tolerance": 0.92, "event_date": ""})
    assert r.status_code == 200 and r.json()["name"] == "Wedding Day" and r.json()["event_date"] is None
    assert s.patch(f"/api/events/{ev['id']}", json={"tolerance": 0.5}).status_code == 422   # a dlib-scale value
    assert s.patch(f"/api/events/{ev['id']}", json={"tolerance": 1.4}).status_code == 422
    assert s.delete(f"/api/events/{ev['id']}").status_code == 200
    assert s.delete(f"/api/clients/{cid}").status_code == 200


def test_event_plan_limit(make_user):
    s = make_user()
    cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
    for i in range(2):
        assert s.post("/api/events", json={"client_id": cid, "name": f"E{i}"}).status_code == 200
    r = s.post("/api/events", json={"client_id": cid, "name": "E3"})
    assert r.status_code == 403 and r.json()["error"] == "plan_limit"


def test_event_names_with_any_script_get_a_slug(studio):
    s, cid, _ = studio
    e = s.post("/api/events", json={"client_id": cid, "name": "शादी समारोह"}).json()
    assert re.fullmatch(r"event-[a-z0-9]{8}", e["slug"])


# ------------------------------------------------------------------------------------------ tenant isolation
def test_one_company_cannot_touch_anothers_data(make_user):
    a = make_user("a@x.test", "A")
    b = make_user("b@x.test", "B")
    cid = a.post("/api/clients", json={"name": "A's client"}).json()["id"]
    ev = a.post("/api/events", json={"client_id": cid, "name": "A's event"}).json()
    eid = ev["id"]
    assert upload(a, eid, [("p.jpg", jpeg_bytes())]).json()["queued"] == 1
    wait_idle(a, eid)
    pid = a.get(f"/api/events/{eid}/photos").json()[0]["id"]

    assert b.get("/api/clients").json() == [] and b.get("/api/events").json() == []
    for method, url, kw in [
        ("GET", f"/api/events/{eid}", {}), ("PATCH", f"/api/events/{eid}", {"json": {"name": "hacked"}}),
        ("DELETE", f"/api/events/{eid}", {}), ("GET", f"/api/events/{eid}/photos", {}),
        ("GET", f"/api/events/{eid}/photos/{pid}/orig", {}), ("GET", f"/api/events/{eid}/photos/{pid}/thumb", {}),
        ("GET", f"/api/events/{eid}/qr.png", {}), ("POST", f"/api/events/{eid}/purge-faces", {}),
        ("POST", f"/api/events/{eid}/reindex", {}), ("POST", f"/api/events/{eid}/photos/retry", {}),
        ("POST", f"/api/events/{eid}/photos/delete", {"json": {"ids": [pid]}}),
        ("POST", f"/api/events/{eid}/photos", {"files": [("files", ("x.jpg", jpeg_bytes(seed=9), "image/jpeg"))]}),
        ("PATCH", f"/api/clients/{cid}", {"json": {"name": "hacked"}}), ("DELETE", f"/api/clients/{cid}", {}),
    ]:
        r = b.req(method, url, **kw)
        assert r.status_code == 404, f"{method} {url} -> {r.status_code}"
    # B can't attach a new event to A's client, nor move its own event under A's client
    assert b.post("/api/events", json={"client_id": cid, "name": "sneaky"}).status_code == 404
    bc = b.post("/api/clients", json={"name": "B client"}).json()["id"]
    be = b.post("/api/events", json={"client_id": bc, "name": "B event"}).json()
    assert b.patch(f"/api/events/{be['id']}", json={"client_id": cid}).status_code == 404
    # nothing of A's changed
    assert a.get(f"/api/events/{eid}").json()["name"] == "A's event"
    assert len(a.get(f"/api/events/{eid}/photos").json()) == 1


# ------------------------------------------------------------------------------------------ uploads
def test_upload_validation_dedup_and_processing(studio, monkeypatch):
    s, _, ev = studio
    eid = ev["id"]
    good = jpeg_bytes(seed=1)
    r = upload(s, eid, [("a.jpg", good), ("notes.txt", b"hello"), ("empty.jpg", b""), ("a-copy.jpg", good)]).json()
    by = {x["name"]: x for x in r["results"]}
    assert by["a.jpg"]["status"] == "queued" and by["a-copy.jpg"]["status"] == "duplicate"
    assert by["notes.txt"]["status"] == "rejected" and "Unsupported" in by["notes.txt"]["error"]
    assert by["empty.jpg"]["status"] == "rejected"
    e = wait_idle(s, eid)
    assert e["photos"] == 1 and e["ready"] == 1 and e["failed"] == 0

    # a file that isn't really an image fails processing with a readable message, and Retry re-queues it
    upload(s, eid, [("broken.jpg", b"definitely not a jpeg" * 20)])
    e = wait_idle(s, eid)
    assert e["failed"] == 1
    failed = [p for p in s.get(f"/api/events/{eid}/photos?status=failed").json()]
    assert "couldn't read" in failed[0]["error"].lower()
    assert s.post(f"/api/events/{eid}/photos/retry").json()["retried"] == 1
    wait_idle(s, eid)

    # thumbnails and web copies exist and the dashboard can fetch them
    pid = s.get(f"/api/events/{eid}/photos?status=ready").json()[0]["id"]
    for kind in ("thumb", "web", "orig"):
        assert s.get(f"/api/events/{eid}/photos/{pid}/{kind}").status_code == 200
    assert s.post(f"/api/events/{eid}/photos/delete", json={"ids": [pid]}).json()["deleted"] == 1
    assert s.get(f"/api/events/{eid}/photos/{pid}/thumb").status_code == 404


def test_upload_size_and_request_limits(studio, monkeypatch):
    s, _, ev = studio
    monkeypatch.setattr(config.settings, "max_file_mb", 1)
    big = jpeg_bytes(size=(1600, 1200), seed=3) + b"\0" * (1024 * 1024)
    r = upload(s, ev["id"], [("big.jpg", big)]).json()
    assert r["results"][0]["status"] == "rejected" and "1 MB" in r["results"][0]["error"]
    monkeypatch.setattr(config.settings, "max_files_per_request", 2)
    assert upload(s, ev["id"], [(f"{i}.jpg", jpeg_bytes(seed=i)) for i in range(3)]).status_code == 413


def test_plan_photo_and_storage_limits(studio, monkeypatch):
    s, _, ev = studio
    monkeypatch.setitem(config.PLANS["free"], "photos", 2)
    r = upload(s, ev["id"], [(f"{i}.jpg", jpeg_bytes(seed=10 + i)) for i in range(3)]).json()
    assert [x["status"] for x in r["results"]] == ["queued", "queued", "rejected"]
    assert "limit" in r["results"][2]["error"].lower()
    monkeypatch.setitem(config.PLANS["free"], "photos", 100)
    db.run("UPDATE accounts SET storage_quota=10")   # 10 bytes, as allocated by the platform admin
    r = upload(s, ev["id"], [("z.jpg", jpeg_bytes(seed=40))]).json()
    assert r["results"][0]["status"] == "rejected" and "storage" in r["results"][0]["error"].lower()


# ------------------------------------------------------------------------------------------ guest access
def test_guest_pages_follow_publish_state_and_escape_html(studio, anon):
    s, _, ev = studio
    slug, eid = ev["slug"], ev["id"]
    assert anon.get(f"/e/{slug}").status_code == 404            # draft: nothing is public
    assert anon.get(f"/e/{slug}/info").status_code == 404
    s.patch(f"/api/events/{eid}", json={"name": "<script>alert(1)</script> & Co", "status": "published"})
    page = anon.get(f"/e/{slug}")
    assert page.status_code == 200
    assert "<script>alert(1)</script>" not in page.text and "&lt;script&gt;" in page.text
    assert anon.get(f"/e/{slug}/info").json()["ready"] == 0

    s.patch(f"/api/events/{eid}", json={"expires_at": "2000-01-01"})
    assert anon.get(f"/e/{slug}").status_code == 404           # expired links stop working
    s.patch(f"/api/events/{eid}", json={"expires_at": "", "status": "draft"})
    assert anon.get(f"/e/{slug}/info").status_code == 404      # un-publishing takes it offline again
    assert anon.get("/e/does-not-exist").status_code == 404


def test_guest_cannot_fetch_other_events_or_unpublished_photos(studio, anon):
    s, cid, ev = studio
    ev2 = s.post("/api/events", json={"client_id": cid, "name": "Reception"}).json()
    upload(s, ev["id"], [("one.jpg", jpeg_bytes(seed=21))])
    wait_idle(s, ev["id"])
    pid = s.get(f"/api/events/{ev['id']}/photos").json()[0]["id"]
    s.patch(f"/api/events/{ev2['id']}", json={"status": "published"})
    # the event the photo belongs to is still a draft
    assert anon.get(f"/e/{ev['slug']}/thumb/{pid}.jpg").status_code == 404
    # a published event can't serve a photo that belongs to a different event
    assert anon.get(f"/e/{ev2['slug']}/thumb/{pid}.jpg").status_code == 404
    assert anon.get(f"/e/{ev2['slug']}/download/{pid}").status_code == 404
    s.patch(f"/api/events/{ev['id']}", json={"status": "published"})
    assert anon.get(f"/e/{ev['slug']}/thumb/{pid}.jpg").status_code == 200
    assert anon.get(f"/e/{ev['slug']}/download/{pid}").status_code == 200
    s.patch(f"/api/events/{ev['id']}", json={"allow_download": False})
    assert anon.get(f"/e/{ev['slug']}/download/{pid}").status_code == 403
    assert anon.post(f"/e/{ev['slug']}/zip", data={"ids": pid}).status_code == 403
    assert anon.get(f"/e/{ev['slug']}/web/{pid}.jpg").status_code == 200   # viewing stays on


def test_guest_search_rejects_garbage_and_no_face(studio, anon):
    s, _, ev = studio
    s.patch(f"/api/events/{ev['id']}", json={"status": "published"})
    r = anon.post(f"/e/{ev['slug']}/search", files={"file": ("s.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 400
    r = anon.post(f"/e/{ev['slug']}/search", files={"file": ("s.jpg", jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 422 and r.json()["error"] == "no_face"


def test_deleting_event_and_account_removes_files(app_client, make_user, tmp_path):
    s = make_user("del@x.test")
    cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
    ev = s.post("/api/events", json={"client_id": cid, "name": "E"}).json()
    upload(s, ev["id"], [("a.jpg", jpeg_bytes(seed=31))])
    wait_idle(s, ev["id"])
    acct = tmp_path / "data" / "files" / "accounts"
    assert any(acct.rglob("*.jpg"))
    s.delete(f"/api/events/{ev['id']}")
    assert not any(acct.rglob("*.jpg"))
    ev2 = s.post("/api/events", json={"client_id": cid, "name": "E2"}).json()
    upload(s, ev2["id"], [("b.jpg", jpeg_bytes(seed=32))])
    wait_idle(s, ev2["id"])
    assert s.post("/api/account/delete", json={"password": "wrong wrong"}).status_code == 403
    assert s.post("/api/account/delete", json={"password": "correct horse"}).status_code == 200
    assert not (acct / "1").exists() and s.get("/api/auth/me").status_code == 401


# ------------------------------------------------------------------------------------------ robustness
def test_photos_left_mid_processing_are_picked_up_after_a_restart(tmp_path, monkeypatch):
    """A crash or redeploy while photos were processing must not strand them."""
    from starlette.testclient import TestClient
    from conftest import Sess
    from studio import db
    from studio.server import create_app

    monkeypatch.setattr(config.settings, "public_url", "")
    data = tmp_path / "data"
    with TestClient(create_app(data_dir=data, workers=1)) as c:
        r = c.post("/api/auth/signup", headers={"X-PF": "1"}, json={"company": "S", "email": "s@x.test", "password": "longenough1"})
        s = Sess(c, r.cookies.get("pf_session"))
        db.run("UPDATE accounts SET storage_quota=?", (config.GB,))   # allocated by the platform admin
        cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
        eid = s.post("/api/events", json={"client_id": cid, "name": "E"}).json()["id"]
        upload(s, eid, [("a.jpg", jpeg_bytes(seed=51)), ("b.jpg", jpeg_bytes(seed=52))])
        wait_idle(s, eid)
        # simulate dying half way: one photo was processing, one never started
        db.run("UPDATE photos SET status='processing' WHERE orig_name='a.jpg'")
        db.run("UPDATE photos SET status='uploaded' WHERE orig_name='b.jpg'")
        token = s.token
    with TestClient(create_app(data_dir=data, workers=1)) as c2:   # the "restarted" server
        s2 = Sess(c2, token)
        e = wait_idle(s2, eid)
        assert e["ready"] == 2 and e["failed"] == 0


def test_heic_uploads_from_iphones_are_processed(studio):
    pillow_heif = pytest.importorskip("pillow_heif")
    import io
    from PIL import Image

    pillow_heif.register_heif_opener()
    buf = io.BytesIO()
    Image.open(io.BytesIO(jpeg_bytes(seed=61))).save(buf, "HEIF")
    s, _, ev = studio
    r = s.post(f"/api/events/{ev['id']}/photos", files=[("files", ("IMG_0001.HEIC", buf.getvalue(), "image/heic"))])
    assert r.json()["results"][0]["status"] == "queued"
    assert wait_idle(s, ev["id"])["ready"] == 1
    pid = s.get(f"/api/events/{ev['id']}/photos").json()[0]["id"]
    assert s.get(f"/api/events/{ev['id']}/photos/{pid}/thumb").headers["content-type"] == "image/jpeg"  # browsers can show it


# ------------------------------------------------------------------------------------------ fixes
def _parallel(n, fn):
    import threading
    out, ths = [None] * n, []
    for i in range(n):
        ths.append(threading.Thread(target=lambda i=i: out.__setitem__(i, fn(i))))
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return out


def test_plan_limits_hold_under_parallel_requests(app_client, make_user, monkeypatch):
    s = make_user()
    hdr = {"Cookie": f"pf_session={s.token}", "X-PF": "1"}  # per request, not the shared cookie jar
    cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
    codes = _parallel(6, lambda i: app_client.post("/api/events", headers=hdr,
                                                   json={"client_id": cid, "name": f"E{i}"}).status_code)
    assert sorted(codes) == [200, 200, 403, 403, 403, 403]
    eid = s.get("/api/events").json()[0]["id"]

    monkeypatch.setitem(config.PLANS["free"], "photos", 3)
    res = _parallel(8, lambda i: app_client.post(
        f"/api/events/{eid}/photos", headers=hdr,
        files=[("files", (f"{i}.jpg", jpeg_bytes(seed=100 + i), "image/jpeg"))]).json()["results"][0]["status"])
    assert res.count("queued") == 3 and res.count("rejected") == 5
    assert s.get("/api/auth/me").json()["usage"]["photos"] == 3


def test_event_patch_rejects_nulls(studio):
    s, _, ev = studio
    for field in ("name", "tolerance", "status", "client_id", "subtitle", "allow_download"):
        r = s.patch(f"/api/events/{ev['id']}", json={field: None})
        assert r.status_code == 422, (field, r.status_code, r.text)
    r = s.patch(f"/api/events/{ev['id']}", json={"event_date": None, "expires_at": None})
    assert r.status_code == 200 and r.json()["event_date"] is None


def test_guest_zip_is_rate_limited(studio, anon):
    s, _, ev = studio
    upload(s, ev["id"], [("z.jpg", jpeg_bytes(seed=71))])
    wait_idle(s, ev["id"])
    pid = s.get(f"/api/events/{ev['id']}/photos").json()[0]["id"]
    s.patch(f"/api/events/{ev['id']}", json={"status": "published"})
    codes = [anon.post(f"/e/{ev['slug']}/zip", data={"ids": pid}).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


def test_link_expiry_follows_the_studio_time_zone(studio, anon):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    s, _, ev = studio
    assert s.patch("/api/account", json={"timezone": "Mars/Olympus"}).status_code == 422
    # UTC-12 and UTC+14 are always on different calendar days
    behind = datetime.now(ZoneInfo("Etc/GMT+12")).date().isoformat()
    s.patch(f"/api/events/{ev['id']}", json={"status": "published", "expires_at": behind})
    assert s.patch("/api/account", json={"timezone": "Etc/GMT+12"}).json()["account"]["timezone"] == "Etc/GMT+12"
    assert anon.get(f"/e/{ev['slug']}/info").status_code == 200      # still the last day there
    s.patch("/api/account", json={"timezone": "Pacific/Kiritimati"})
    assert anon.get(f"/e/{ev['slug']}/info").status_code == 404      # already the next day there


def test_signup_stores_browser_time_zone(anon, app_client):
    r = anon.post("/api/auth/signup", json={"company": "T", "email": "tz@x.test", "password": "longenough1",
                                            "timezone": "Asia/Kolkata"})
    assert r.json()["account"]["timezone"] == "Asia/Kolkata"
    r = anon.post("/api/auth/signup", json={"company": "T", "email": "tz2@x.test", "password": "longenough1",
                                            "timezone": "Not/AZone"})
    assert r.status_code == 200 and r.json()["account"]["timezone"] == ""   # bad zone is ignored, not fatal


def test_old_databases_are_migrated(tmp_path, monkeypatch):
    import sqlite3
    from studio import db

    data = tmp_path / "old"
    data.mkdir()
    c = sqlite3.connect(data / "studio.db")
    c.executescript(db.SCHEMA)  # a version-1 database, from before the timezone column
    c.execute("PRAGMA user_version=1")
    c.execute("INSERT INTO accounts(company, created_at) VALUES('Old', 0)")
    c.commit()
    c.close()
    monkeypatch.setattr(config.settings, "data_dir", data)
    db.init()
    assert db.q1("PRAGMA user_version")[0] == max(db.MIGRATIONS)
    assert db.q1("SELECT timezone FROM accounts")["timezone"] == ""


def test_studio_server_does_not_load_dlib():
    import subprocess
    import sys
    from pathlib import Path

    code = "import sys; import studio.server, studio.__main__, netutil; print('dlib' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).resolve().parent.parent)
    assert out.stdout.strip() == "False", out.stderr
