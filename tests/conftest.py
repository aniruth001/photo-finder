import io
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

from studio import config  # noqa: E402
from studio.server import create_app  # noqa: E402


class Sess:
    """One signed-in browser. Several can share one app, which is how we test tenant isolation."""

    def __init__(self, client, token=None):
        self.c, self.token = client, token

    def req(self, method, url, **kw):
        self.c.cookies.clear()
        if self.token:
            self.c.cookies.set("pf_session", self.token)
        headers = kw.pop("headers", {})
        if method not in ("GET", "HEAD"):
            headers.setdefault("X-PF", "1")
        return self.c.request(method, url, headers=headers, **kw)

    def get(self, url, **kw): return self.req("GET", url, **kw)
    def post(self, url, **kw): return self.req("POST", url, **kw)
    def patch(self, url, **kw): return self.req("PATCH", url, **kw)
    def put(self, url, **kw): return self.req("PUT", url, **kw)
    def delete(self, url, **kw): return self.req("DELETE", url, **kw)


@pytest.fixture(autouse=True)
def no_scene_model(monkeypatch):
    """Search by description stays off unless a test brings its own engine: the real CLIP model, where it's
    downloaded, would otherwise index every test's photos in the background (tests/test_scenes.py)."""
    from studio import scenes

    monkeypatch.setattr(scenes, "installed", lambda: False)
    scenes.set_engine(None)
    yield
    scenes.set_engine(None)


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "public_url", "")
    monkeypatch.setattr(config.settings, "allow_signup", True)
    monkeypatch.setattr(config.settings, "max_file_mb", 60)
    monkeypatch.setattr(config.settings, "max_files_per_request", 20)
    app = create_app(data_dir=tmp_path / "data", workers=1)
    with TestClient(app) as c:
        c.app_ref = app
        yield c


@pytest.fixture()
def make_user(app_client):
    def make(email="a@studio.test", company="Studio A", password="correct horse", storage_gb=10):
        r = app_client.post("/api/auth/signup", headers={"X-PF": "1"},
                            json={"company": company, "name": "Owner", "email": email, "password": password})
        assert r.status_code == 200, r.text
        if storage_gb:   # a new company has no storage until the platform admin allocates some: do that here
            from studio import db
            db.run("UPDATE accounts SET storage_quota=? WHERE id=(SELECT account_id FROM users WHERE email=?)",
                   (storage_gb * 1024 ** 3, email.lower()))
        return Sess(app_client, r.cookies.get("pf_session"))
    return make


@pytest.fixture()
def anon(app_client):
    return Sess(app_client)


def jpeg_bytes(color=(120, 60, 200), size=(320, 240), seed=0):
    """A small valid JPEG with no face in it. `seed` makes the bytes differ (so dedup doesn't trigger)."""
    im = Image.new("RGB", size, color)
    px = im.load()
    for i in range(seed % 50 + 1):
        px[i, i] = (i * 5 % 255, seed % 255, 7)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def wait_idle(sess, event_id, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        e = sess.get(f"/api/events/{event_id}").json()
        if e["processing"] == 0:
            return e
        time.sleep(0.4)
    raise AssertionError("photos were not processed in time")


def upload(sess, event_id, files):
    """files: list of (name, bytes)"""
    return sess.post(f"/api/events/{event_id}/photos",
                     files=[("files", (n, b, "image/jpeg")) for n, b in files])


@pytest.fixture()
def studio(make_user):
    """A signed-in company with one client and one event."""
    s = make_user()
    cid = s.post("/api/clients", json={"name": "Priya & Arjun"}).json()["id"]
    ev = s.post("/api/events", json={"client_id": cid, "name": "Wedding", "event_date": "2026-11-20"}).json()
    return s, cid, ev
