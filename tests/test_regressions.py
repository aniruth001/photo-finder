"""Bugs found in the October 2026 review, each pinned by a test."""
import io
import sqlite3
import zipfile

from conftest import jpeg_bytes, upload, wait_idle
from studio import db
from studio.storage import get_storage


def test_company_name_of_only_spaces_is_refused(app_client, studio):
    r = app_client.post("/api/auth/signup", headers={"X-PF": "1"}, json={"company": "   ", "email": "b@x.test", "password": "longenough1"})
    assert r.status_code == 422 and r.json()["error"] == "company"
    s, _, _ = studio
    assert s.patch("/api/account", json={"company": "  "}).status_code == 422
    assert s.get("/api/auth/me").json()["account"]["company"].strip()


def test_validation_errors_have_a_message(studio):
    s, _, _ = studio
    r = s.post("/api/team/invites", json={"email": "a@b.test", "role": "viewer", "name": "x" * 101})
    assert r.status_code == 422 and r.json() == {"error": "invalid", "message": "Check name: should have at most 100 characters."}
    r = s.post("/api/estimates", json={"company_name": "A", "client_name": "B", "estimate_date": "2026-10-21", "tax_pct": 150})
    assert r.status_code == 422 and "tax pct" in r.json()["message"]


def test_an_expired_invite_sent_again_needs_a_free_seat(studio):
    s, _, _ = studio                                                                 # free plan: 2 seats, owner + 1
    assert s.post("/api/team/invites", json={"email": "m1@x.test", "role": "viewer"}).status_code == 200
    db.run("UPDATE invites SET expires_at=1")                                         # expired: its seat is free again
    assert s.post("/api/team/invites", json={"email": "m2@x.test", "role": "viewer"}).status_code == 200
    assert s.post("/api/team/invites", json={"email": "m1@x.test", "role": "viewer"}).json()["error"] == "plan_limit"
    iid = next(i["id"] for i in s.get("/api/team").json()["invites"] if i["email"] == "m1@x.test")
    assert s.post(f"/api/team/invites/{iid}/resend").json()["error"] == "plan_limit"
    assert s.get("/api/team").json()["seats"] == {"used": 2, "limit": 2}
    # a live invite sent again is fine: it already holds its seat
    m2 = next(i["id"] for i in s.get("/api/team").json()["invites"] if i["email"] == "m2@x.test")
    assert s.post(f"/api/team/invites/{m2}/resend").status_code == 200
    assert s.post("/api/team/invites", json={"email": "m2@x.test", "role": "manager"}).status_code == 200


def test_billing_numbers_are_never_reused(studio):
    s, _, _ = studio
    body = {"company_name": "A", "client_name": "B", "estimate_date": "2026-10-21", "items": [{"name": "x", "amount": 1}], "kind": "invoice"}
    s.post("/api/estimates", json=body)
    newest = s.post("/api/estimates", json=body).json()
    assert newest["label"] == "INV-0002"
    s.delete(f"/api/estimates/{newest['id']}")
    assert s.get("/api/estimates/defaults").json()["labels"]["invoice"] == "INV-0003"
    assert s.post("/api/estimates", json=body).json()["label"] == "INV-0003"
    assert s.post("/api/estimates", json={**body, "kind": "estimate"}).json()["label"] == "EST-0001"


def test_numbering_carries_on_from_documents_made_before_the_fix():
    from studio.db import MIGRATIONS, SCHEMA

    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    for n in range(2, 15):
        c.executescript(f"BEGIN; {MIGRATIONS[n]}; COMMIT;")
    c.execute("INSERT INTO accounts(company, plan, created_at) VALUES('A','free',0)")
    for kind, n in (("estimate", 4), ("estimate", 9), ("invoice", 2)):
        c.execute("INSERT INTO estimates(account_id, kind, number, company_name, client_name, estimate_date, created_at, updated_at)"
                  " VALUES(1,?,?,'A','B','2026-10-21',0,0)", (kind, n))
    c.executescript(f"BEGIN; {MIGRATIONS[15]}; COMMIT;")
    assert sorted(c.execute("SELECT kind, last FROM doc_numbers").fetchall()) == [("estimate", 9), ("invoice", 2)]


def test_missing_originals_do_not_break_downloads(studio, anon):
    s, _, ev = studio
    s.patch(f"/api/events/{ev['id']}", json={"status": "published"})
    upload(s, ev["id"], [("a.jpg", jpeg_bytes(seed=971)), ("b.jpg", jpeg_bytes(seed=972)), ("A.JPG", jpeg_bytes(seed=973))])
    wait_idle(s, ev["id"])
    ph = s.get(f"/api/events/{ev['id']}/photos").json()
    gone = next(p for p in ph if p["name"] == "b.jpg")
    get_storage().photo_path(db.q1("SELECT * FROM photos WHERE public_id=?", (gone["id"],)), "orig").unlink()
    r = anon.c.post(f"/e/{ev['slug']}/zip", data={"ids": ",".join(p["id"] for p in ph)})
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 2 and len({n.lower() for n in names}) == 2                 # skipped, and a.jpg / A.JPG both kept
    assert anon.c.get(f"/e/{ev['slug']}/download/{gone['id']}").status_code == 404


def test_deleted_face_data_stays_deleted_until_reindexed(studio):
    s, _, ev = studio
    upload(s, ev["id"], [("a.jpg", jpeg_bytes(seed=981))])
    wait_idle(s, ev["id"])
    s.post(f"/api/events/{ev['id']}/purge-faces")
    # a photo processed after the purge (uploaded later, or in progress during it) stores no face vectors
    import numpy as np
    from studio.processing import Processor

    row = db.q1("SELECT p.id, p.account_id, p.event_id, p.public_id, p.ext, e.face_model FROM photos p JOIN events e ON e.id=p.event_id WHERE p.event_id=?", (ev["id"],))
    db.run("UPDATE photos SET status='processing' WHERE id=?", (row["id"],))

    class Done:
        def result(self):
            return 10, 10, [np.ones(128, np.float32).tobytes()]
    Processor()._finish(row, Done())
    assert db.q1("SELECT status FROM photos WHERE id=?", (row["id"],))["status"] == "ready"
    assert db.q1("SELECT COUNT(*) n FROM faces WHERE event_id=?", (ev["id"],))["n"] == 0
    # re-indexing switches the flag off first, so the photos it queues keep their faces
    s.post(f"/api/events/{ev['id']}/reindex")
    assert s.get(f"/api/events/{ev['id']}").json()["faces_purged"] is False
    db.run("UPDATE photos SET status='processing' WHERE id=?", (row["id"],))
    Processor()._finish(row, Done())
    assert db.q1("SELECT COUNT(*) n FROM faces WHERE event_id=?", (ev["id"],))["n"] == 1


def test_a_failed_photo_can_be_uploaded_again(studio):
    s, _, ev = studio
    data = jpeg_bytes(seed=991)
    upload(s, ev["id"], [("cut.jpg", data)])
    wait_idle(s, ev["id"])
    row = db.q1("SELECT * FROM photos WHERE event_id=?", (ev["id"],))
    # what a restart in the middle of the upload leaves: marked failed, the original never finished
    store = get_storage()
    store.photo_path(row, "orig").rename(store.path(row["account_id"], row["event_id"], "orig", row["public_id"], row["ext"] + ".part"))
    db.run("UPDATE photos SET status='failed', error='Upload was interrupted. Upload it again.' WHERE id=?", (row["id"],))
    r = upload(s, ev["id"], [("cut.jpg", data)]).json()["results"][0]
    assert r["status"] == "queued", r                                                # not "already uploaded"
    e = wait_idle(s, ev["id"])
    assert (e["photos"], e["ready"], e["failed"]) == (1, 1, 0)
    assert not list(store.event_dir(row["account_id"], row["event_id"]).glob("*/*.part"))
    assert upload(s, ev["id"], [("cut.jpg", data)]).json()["results"][0]["status"] == "duplicate"   # a ready one still is


def test_half_written_uploads_are_removed_at_start(studio):
    from studio.ingest import remove_leftovers

    s, _, ev = studio
    acct = s.get("/api/auth/me").json()["account"]["id"]
    get_storage().ensure(acct, ev["id"])
    part = get_storage().event_dir(acct, ev["id"]) / "orig" / "abc.jpg.part"
    part.write_bytes(b"half")
    assert remove_leftovers() == 1 and not part.exists()


def test_visitor_address_behind_a_proxy(monkeypatch):
    from types import SimpleNamespace
    from studio import config
    from studio.server import client_ip

    def req(peer, xff=None):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": xff} if xff else {})

    monkeypatch.setattr(config.settings, "trust_proxy", "auto")
    assert client_ip(req("127.0.0.1", "49.36.1.10")) == "49.36.1.10"              # nginx on this machine
    assert client_ip(req("172.18.0.3", "49.36.1.10")) == "49.36.1.10"             # nginx in Docker
    assert client_ip(req("127.0.0.1", "8.8.4.4, 49.36.1.10")) == "49.36.1.10"     # a typed-in first hop is ignored
    assert client_ip(req("127.0.0.1", "49.36.1.10, 10.0.0.2")) == "49.36.1.10"    # our own proxies are skipped
    assert client_ip(req("103.21.1.5", "8.8.4.4")) == "103.21.1.5"              # direct from the internet: no proxy
    assert client_ip(req("192.168.1.20")) == "192.168.1.20"                         # a phone on the studio's Wi-Fi
    monkeypatch.setattr(config.settings, "trust_proxy", "0")
    assert client_ip(req("127.0.0.1", "49.36.1.10")) == "127.0.0.1"
    monkeypatch.setattr(config.settings, "trust_proxy", "1")
    assert client_ip(req("103.21.1.5", "49.36.1.10")) == "49.36.1.10"


def test_rate_limits_are_per_visitor_behind_a_proxy(app_client, monkeypatch):
    from studio import config

    monkeypatch.setattr(config.settings, "trust_proxy", "1")                         # the test client isn't on loopback
    def login(ip):
        return app_client.post("/api/auth/login", headers={"X-PF": "1", "X-Forwarded-For": ip},
                               json={"email": "nobody@x.test", "password": "wrong-password"}).status_code
    assert [login("49.36.1.11") for _ in range(10)] == [401] * 10 and login("49.36.1.11") == 429
    assert login("49.36.1.12") == 401                                                # another visitor isn't blocked


def test_event_names_are_shown_as_typed_on_guest_and_client_pages(studio, anon):
    s, _, ev = studio
    s.patch(f"/api/events/{ev['id']}", json={"name": "Party __MARK__ __BRAND__ <b>", "status": "published", "client_enabled": True})
    page = anon.get(f"/e/{ev['slug']}").text
    assert "Party __MARK__ __BRAND__ &lt;b&gt;" in page
    token = s.get(f"/api/events/{ev['id']}").json()["client"]["url"].rsplit("/", 1)[1]
    assert "Party __MARK__ __BRAND__ &lt;b&gt;" in anon.get(f"/c/{token}").text
    assert "__THEME__" not in page and "__ACCENT_DARK__" not in page                 # the real placeholders are filled



def test_windows_connection_reset_is_cleaned_up_quietly():
    """Python < 3.11 on Windows: shutdown() on a socket the phone already reset raised WinError 10054, printed a
    traceback and skipped closing the socket. Now the socket is closed and the server forgets the connection."""
    import socket
    import sys
    import pytest
    if sys.platform != "win32" or sys.version_info >= (3, 11):
        pytest.skip("only Windows on Python 3.10 has the bug")
    from asyncio import proactor_events as pe
    from studio.server import fix_proactor_resets
    fix_proactor_resets()
    events = []

    class Sock:
        def shutdown(self, how):
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")

        def close(self):
            events.append("closed")

    class Proto:
        def connection_lost(self, exc):
            events.append("lost")

    class Server:
        def _detach(self):
            events.append("detached")

    t = pe._ProactorBasePipeTransport.__new__(pe._ProactorBasePipeTransport)
    t._sock, t._protocol, t._server = Sock(), Proto(), Server()
    t._call_connection_lost(None)                                                   # no exception
    assert events == ["lost", "closed", "detached"] and t._sock is None and t._server is None
    t._call_connection_lost(None)                                                   # twice: nothing more
    assert events == ["lost", "closed", "detached"]
