"""Client gallery: every photo of an event on a private link, with an optional PIN."""
import io
import zipfile

import pytest

from conftest import Sess, jpeg_bytes, upload, wait_idle
from studio import config, db


@pytest.fixture()
def gallery(studio):
    """An event with 3 photos and its client gallery turned on; returns (studio session, event, client path)."""
    s, cid, ev = studio
    upload(s, ev["id"], [(f"IMG_{i}.jpg", jpeg_bytes(seed=400 + i)) for i in range(3)])
    wait_idle(s, ev["id"])
    e = s.patch(f"/api/events/{ev['id']}", json={"client_enabled": True}).json()
    path = e["client"]["url"].split("testserver", 1)[1]
    return s, e, path


def test_client_sees_every_photo_without_a_selfie(gallery, anon):
    s, e, path = gallery
    assert e["status"] == "draft"                                      # works before the guests' link is published
    assert anon.get(f"/e/{e['slug']}").status_code == 404
    page = anon.get(path)
    assert page.status_code == 200 and e["name"] in page.text
    info = anon.get(path + "/info").json()
    assert info["photos"] == 3 and info["unlocked"] is True and info["has_pin"] is False
    photos = anon.get(path + "/photos").json()
    assert sorted(p["name"] for p in photos) == ["IMG_0.jpg", "IMG_1.jpg", "IMG_2.jpg"]
    pid = photos[0]["id"]
    for kind in ("thumb", "web"):
        assert anon.get(f"{path}/{kind}/{pid}.jpg").status_code == 200
    assert anon.get(f"{path}/download/{pid}").headers["content-disposition"].startswith("attachment")
    assert s.get(f"/api/events/{e['id']}").json()["client"]["views"] == 1


def test_download_all_streams_a_valid_zip(gallery, anon):
    _, _, path = gallery
    r = anon.post(path + "/zip", data={"ids": ""})
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert sorted(z.namelist()) == ["IMG_0.jpg", "IMG_1.jpg", "IMG_2.jpg"] and z.testzip() is None
    one = anon.get(path + "/photos").json()[1]["id"]
    z = zipfile.ZipFile(io.BytesIO(anon.post(path + "/zip", data={"ids": one}).content))
    assert len(z.namelist()) == 1                                      # just the selected ones


def test_same_file_names_get_unique_names_in_the_zip(studio, anon):
    from studio.client_gallery import unique_names

    rows = [{"orig_name": "DSC_1.JPG", "public_id": "aaaaaaa1"}, {"orig_name": "dsc_1.jpg", "public_id": "bbbbbbb2"}]
    assert [n for _, n in unique_names(rows)] == ["DSC_1.JPG", "dsc_1-bbbbbb.jpg"]


def test_pin_protects_everything_and_changing_it_locks_people_out(gallery, anon, app_client):
    s, e, path = gallery
    assert s.post(f"/api/events/{e['id']}/client-pin", json={"pin": "12"}).status_code == 422
    assert s.post(f"/api/events/{e['id']}/client-pin", json={"pin": "482913"}).json()["client"]["has_pin"] is True
    info = anon.get(path + "/info").json()
    assert info["has_pin"] and not info["unlocked"] and info["photos"] is None      # nothing leaks before the PIN
    assert anon.get(path + "/photos").status_code == 401
    assert anon.post(path + "/zip", data={"ids": ""}).status_code == 401
    assert anon.post(path + "/unlock", json={"pin": "000000"}).status_code == 403

    c = app_client
    c.cookies.clear()
    r = c.post(path + "/unlock", json={"pin": "482913"})
    assert r.status_code == 200
    assert c.get(path + "/photos").status_code == 200                  # this browser is in
    s.post(f"/api/events/{e['id']}/client-pin", json={"pin": "111222"})
    s.c.cookies.clear()
    c.cookies.set(f"pf_c{e['id']}", r.cookies.get(f"pf_c{e['id']}"))
    assert c.get(path + "/photos").status_code == 401                  # old PIN's browsers are locked again
    assert s.post(f"/api/events/{e['id']}/client-pin", json={"pin": ""}).json()["client"]["has_pin"] is False


def test_pin_guessing_is_limited(gallery, anon):
    s, e, path = gallery
    s.post(f"/api/events/{e['id']}/client-pin", json={"pin": "482913"})
    codes = [anon.post(path + "/unlock", json={"pin": f"{i:06d}"}).status_code for i in range(11)]
    assert codes[:10] == [403] * 10 and codes[10] == 429
    assert anon.post(path + "/unlock", json={"pin": "482913"}).status_code == 429  # even the right one, for a while


def test_off_reset_downloads_and_suspension(gallery, anon):
    s, e, path = gallery
    pid = anon.get(path + "/photos").json()[0]["id"]
    s.patch(f"/api/events/{e['id']}", json={"client_download": False})
    assert anon.get(f"{path}/download/{pid}").status_code == 403
    assert anon.post(path + "/zip", data={"ids": ""}).status_code == 403
    assert anon.get(f"{path}/web/{pid}.jpg").status_code == 200        # viewing stays on
    new = s.post(f"/api/events/{e['id']}/client-link/reset").json()["client"]["url"].split("testserver", 1)[1]
    assert new != path and anon.get(path).status_code == 404 and anon.get(new + "/info").status_code == 200
    s.patch(f"/api/events/{e['id']}", json={"client_enabled": False})
    assert anon.get(new + "/info").status_code == 404
    s.patch(f"/api/events/{e['id']}", json={"client_enabled": True})   # back on: the same link again
    assert anon.get(new + "/info").status_code == 200
    db.run("UPDATE accounts SET disabled_at=1")
    assert anon.get(new + "/info").status_code == 404


def test_only_event_permission_can_change_it_and_other_events_photos_stay_out(gallery, anon, make_user, studio):
    s, e, path = gallery
    other = s.post("/api/events", json={"client_id": e["client_id"], "name": "Other"}).json()
    upload(s, other["id"], [("x.jpg", jpeg_bytes(seed=499))])
    wait_idle(s, other["id"])
    foreign = s.get(f"/api/events/{other['id']}/photos").json()[0]["id"]
    assert anon.get(f"{path}/web/{foreign}.jpg").status_code == 404    # this link shows this event only
    db.run("UPDATE users SET role='photographer'")
    assert s.patch(f"/api/events/{e['id']}", json={"client_enabled": False}).status_code == 403
    assert s.post(f"/api/events/{e['id']}/client-pin", json={"pin": "1234"}).status_code == 403
    b = make_user("b@x.test", "B")
    assert b.post(f"/api/events/{e['id']}/client-link/reset").status_code == 404


class _FakeEngine:
    """Says every image holds one face with the vector set in .vec (the real engine is tested elsewhere)."""
    vec = None

    def faces(self, im, **kw):
        return [(100.0, self.vec)]


def test_find_a_person_filters_the_gallery(gallery, anon, monkeypatch):
    import numpy as np
    import faces

    s, e, path = gallery
    eng = _FakeEngine()
    monkeypatch.setattr(faces, "make_engine", lambda *a, **k: eng)
    rows = db.q("SELECT id, public_id FROM photos WHERE event_id=? ORDER BY orig_name", (e["id"],))
    u = np.eye(128, dtype=np.float32)
    near, nearer = (0.8 * u[0] + 0.6 * u[1]), (0.98 * u[0] + 0.199 * u[1])   # 0.63 and 0.2 from u[0]
    for r, v in ((rows[0], near), (rows[1], nearer), (rows[2], u[5])):      # the third photo is someone else
        db.ex("INSERT INTO faces(event_id, photo_id, vec, model) VALUES(?,?,?,'sface')",
              (e["id"], r["id"], (v / np.linalg.norm(v)).astype(np.float32).tobytes()))
    eng.vec = u[0]
    before = s.get(f"/api/events/{e['id']}").json()["searches"]
    r = anon.post(path + "/search", files={"file": ("me.jpg", jpeg_bytes(seed=1), "image/jpeg")})
    assert r.status_code == 200
    got = r.json()
    assert [p["id"] for p in got["photos"]] == [rows[1]["public_id"], rows[0]["public_id"]]   # best match first
    assert got["count"] == 2 and {"w", "h", "name"} <= set(got["photos"][0])
    assert s.get(f"/api/events/{e['id']}").json()["searches"] == before   # the client's filter isn't a guest search
    # the filtered set downloads as one zip, like the selection does
    ids = ",".join(p["id"] for p in got["photos"])
    assert len(zipfile.ZipFile(io.BytesIO(anon.post(path + "/zip", data={"ids": ids}).content)).namelist()) == 2


def test_find_a_person_rules(gallery, anon, app_client):
    s, e, path = gallery
    selfie = {"file": ("me.jpg", jpeg_bytes(seed=2), "image/jpeg")}
    assert anon.post(path + "/search", files=selfie).json()["error"] == "no_face"      # a plain JPEG has no face
    assert anon.post(path + "/search", files={"file": ("x.jpg", b"not an image", "image/jpeg")}).status_code == 400
    s.post(f"/api/events/{e['id']}/client-pin", json={"pin": "482913"})
    app_client.cookies.clear()
    assert anon.post(path + "/search", files=selfie).status_code == 401                  # the PIN comes first
    s.post(f"/api/events/{e['id']}/client-pin", json={"pin": ""})
    s.post(f"/api/events/{e['id']}/purge-faces")
    assert anon.get(path + "/info").json()["searchable"] is False
    assert anon.post(path + "/search", files=selfie).status_code == 410
    assert anon.get(path + "/photos").status_code == 200 and len(anon.get(path + "/photos").json()) == 3  # browsing stays
