"""Full flow with real faces: upload -> process -> publish -> guest selfie -> only that person's photos.

Needs a folder of face photos. Point PF_TEST_FACES at one that contains obama.jpg, obama2.jpg, obama3.jpg and
biden.jpg (for example the tests/test_images folder of the `face_recognition` package). Skipped otherwise.
"""
import io
import os
from pathlib import Path

import pytest
from PIL import Image

from conftest import upload, wait_idle

FACES = Path(os.environ.get("PF_TEST_FACES", "/nonexistent"))
pytestmark = pytest.mark.skipif(not (FACES / "obama.jpg").exists(), reason="set PF_TEST_FACES to a folder with sample faces")


def raw(name):
    return (FACES / name).read_bytes()


def group_shot():
    a, b = (Image.open(FACES / n).convert("RGB") for n in ("obama.jpg", "biden.jpg"))
    a, b = (im.resize((round(im.width * 600 / im.height), 600)) for im in (a, b))
    g = Image.new("RGB", (a.width + b.width, 600))
    g.paste(a, (0, 0))
    g.paste(b, (a.width, 0))
    buf = io.BytesIO()
    g.save(buf, "JPEG")
    return buf.getvalue()


def search(anon, slug, name):
    r = anon.post(f"/e/{slug}/search", files={"file": (name, raw(name), "image/jpeg")})
    assert r.status_code == 200, r.text
    return r.json()


def test_selfie_finds_only_that_persons_photos(studio, anon, make_user):
    s, cid, ev = studio
    eid, slug = ev["id"], ev["slug"]
    upload(s, eid, [("obama2.jpg", raw("obama2.jpg")), ("obama3.jpg", raw("obama3.jpg")),
                    ("biden.jpg", raw("biden.jpg")), ("group.jpg", group_shot())])
    e = wait_idle(s, eid)
    assert e["ready"] == 4 and e["failed"] == 0

    assert anon.post(f"/e/{slug}/search", files={"file": ("o.jpg", raw("obama.jpg"), "image/jpeg")}).status_code == 404  # draft
    s.patch(f"/api/events/{eid}", json={"status": "published"})

    names = {p["name"] for p in search(anon, slug, "obama.jpg")["photos"]}
    assert names == {"obama2.jpg", "obama3.jpg", "group.jpg"}          # all of his photos, nobody else's
    names = {p["name"] for p in search(anon, slug, "biden.jpg")["photos"]}
    assert names == {"biden.jpg", "group.jpg"}

    # the guest can open exactly the photos that matched
    first = search(anon, slug, "obama.jpg")["photos"][0]["id"]
    assert anon.get(f"/e/{slug}/web/{first}.jpg").status_code == 200
    z = anon.post(f"/e/{slug}/zip", data={"ids": first})
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"

    st = s.get(f"/api/events/{eid}").json()
    assert st["searches"] == 3 and st["found"] == 3                    # counts only; no selfie is stored

    # another event of the same company does not see these faces
    other = s.post("/api/events", json={"client_id": cid, "name": "Other"}).json()
    upload(s, other["id"], [("biden.jpg", raw("biden.jpg"))])
    wait_idle(s, other["id"])
    s.patch(f"/api/events/{other['id']}", json={"status": "published"})
    assert search(anon, other["slug"], "obama.jpg")["count"] == 0
    assert search(anon, other["slug"], "biden.jpg")["count"] == 1


def test_search_follows_deletes_purge_and_reindex(studio, anon):
    s, _, ev = studio
    eid, slug = ev["id"], ev["slug"]
    upload(s, eid, [("obama2.jpg", raw("obama2.jpg")), ("obama3.jpg", raw("obama3.jpg"))])
    wait_idle(s, eid)
    s.patch(f"/api/events/{eid}", json={"status": "published"})
    assert search(anon, slug, "obama.jpg")["count"] == 2

    pid = next(p["id"] for p in s.get(f"/api/events/{eid}/photos").json() if p["name"] == "obama3.jpg")
    s.post(f"/api/events/{eid}/photos/delete", json={"ids": [pid]})
    assert {p["name"] for p in search(anon, slug, "obama.jpg")["photos"]} == {"obama2.jpg"}   # cache saw the delete

    # privacy: purging deletes every face vector and switches guest search off
    assert s.post(f"/api/events/{eid}/purge-faces").json()["faces_purged"] is True
    r = anon.post(f"/e/{slug}/search", files={"file": ("o.jpg", raw("obama.jpg"), "image/jpeg")})
    assert r.status_code == 410
    assert anon.get(f"/e/{slug}/info").json()["searchable"] is False

    # re-indexing brings it back
    s.post(f"/api/events/{eid}/reindex")
    wait_idle(s, eid)
    assert search(anon, slug, "obama.jpg")["count"] == 1
