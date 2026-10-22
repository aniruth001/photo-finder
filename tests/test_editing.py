"""Photo editing: save as copy, update a copy (never an original); edited copies are shown everywhere, tagged."""
import io
import zipfile

from PIL import Image

from conftest import jpeg_bytes, upload, wait_idle
from studio import db


def setup(s, ev):
    upload(s, ev["id"], [(f"IMG_{i}.jpg", jpeg_bytes(seed=900 + i)) for i in range(2)])
    wait_idle(s, ev["id"])
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    ids = {p["name"]: p["id"] for p in s.get(f"/api/events/{ev['id']}/photos").json()}
    return e, e["client"]["url"].split("testserver", 1)[1], f"/e/{e['slug']}", ids


def save_copy(s, eid, pid, data):
    return s.post(f"/api/events/{eid}/photos/{pid}/edits", files={"file": ("edited.jpg", data, "image/jpeg")})


def test_copy_then_update_never_the_original(studio, anon):
    s, _, ev = studio
    e, cpath, gpath, ids = setup(s, ev)
    orig = ids["IMG_0.jpg"]
    src = s.get(f"/api/events/{ev['id']}/photos/{orig}/editor/source")
    assert src.status_code == 200 and src.content[:3] == b"\xff\xd8\xff" and Image.open(io.BytesIO(src.content)).size == (320, 240)
    info = s.get(f"/api/events/{ev['id']}/photos/{orig}/editor/info").json()
    assert info["edited"] is False and info["event"]["name"] == "Wedding"

    assert s.put(f"/api/events/{ev['id']}/photos/{orig}/edit", files={"file": ("e.jpg", jpeg_bytes(seed=1), "image/jpeg")}).json()["error"] == "original"
    c = save_copy(s, ev["id"], orig, jpeg_bytes((10, 200, 10), seed=2)).json()
    assert c["edited"] and c["name"] == "IMG_0 (edited).jpg" and c["source"] == {"id": orig, "name": "IMG_0.jpg"}
    wait_idle(s, ev["id"])
    v1 = s.get(f"/api/events/{ev['id']}/photos/{c['id']}/editor/info").json()["v"]

    new = jpeg_bytes((200, 10, 10), seed=3)
    u = s.put(f"/api/events/{ev['id']}/photos/{c['id']}/edit", files={"file": ("e.jpg", new, "image/jpeg")}).json()
    assert u["id"] == c["id"] and u["size"] == len(new) and u["v"] >= v1
    wait_idle(s, ev["id"])
    assert s.get(f"/api/events/{ev['id']}/photos/{c['id']}/orig").content == new          # the copy changed...
    assert s.get(f"/api/events/{ev['id']}/photos/{orig}/orig").content == jpeg_bytes(seed=900)   # ...the original didn't

    cc = save_copy(s, ev["id"], c["id"], jpeg_bytes(seed=4)).json()                     # a copy of a copy
    assert cc["name"] == "IMG_0 (edited).jpg" and cc["source"]["id"] == orig
    listed = {p["id"]: p for p in s.get(f"/api/events/{ev['id']}/photos").json()}
    assert listed[c["id"]]["edited"] and listed[c["id"]]["source"]["name"] == "IMG_0.jpg" and not listed[orig]["edited"]
    assert sorted(p["id"] for p in s.get(f"/api/events/{ev['id']}/photos?edited=1").json()) == sorted([c["id"], cc["id"]])
    assert s.get(f"/api/events/{ev['id']}").json()["edited"] == 2


def test_clients_and_guests_see_edited_copies_tagged(studio, anon):
    s, _, ev = studio
    e, cpath, gpath, ids = setup(s, ev)
    c = save_copy(s, ev["id"], ids["IMG_1.jpg"], jpeg_bytes((10, 10, 200), seed=5)).json()
    wait_idle(s, ev["id"])

    listed = {p["name"]: p["edited"] for p in anon.get(cpath + "/photos").json()}
    assert listed == {"IMG_0.jpg": False, "IMG_1.jpg": False, "IMG_1 (edited).jpg": True}   # in the gallery, tagged
    assert anon.get(cpath + "/info").json()["photos"] == 3
    for kind in ("thumb", "web"):
        assert anon.get(f"{cpath}/{kind}/{c['id']}.jpg").status_code == 200
        assert anon.get(f"{gpath}/{kind}/{c['id']}.jpg").status_code == 200
    assert anon.get(f"{cpath}/download/{c['id']}").status_code == 200
    z = zipfile.ZipFile(io.BytesIO(anon.post(cpath + "/zip", data={"ids": ""}).content))
    assert sorted(z.namelist()) == ["IMG_0.jpg", "IMG_1 (edited).jpg", "IMG_1.jpg"]
    assert anon.get(gpath + "/info").json()["ready"] == 3

    al = anon.post(cpath + "/album", json={"ids": [c["id"]]}).json()                     # the client puts it in the album...
    assert al["added"] == 1 and [p["edited"] for p in al["photos"]] == [True]
    st = s.post(f"/api/events/{ev['id']}/album/add", json={"ids": [c["id"], ids["IMG_0.jpg"]]})   # ...and so can the studio
    assert st.status_code == 200, st.text
    by = {p["id"]: (p["by"], p["edited"]) for p in s.get(f"/api/events/{ev['id']}/album").json()["photos"]}
    assert by == {c["id"]: ("both", True), ids["IMG_0.jpg"]: ("studio", False)}

    s.post(f"/api/events/{ev['id']}/photos/delete", json={"ids": [ids["IMG_1.jpg"]]})     # the original goes...
    left = s.get(f"/api/events/{ev['id']}/photos/{c['id']}/editor/info").json()
    assert left["edited"] is True and left["source"] is None                              # ...the copy stays
    assert sorted(p["name"] for p in anon.get(cpath + "/photos").json()) == ["IMG_0.jpg", "IMG_1 (edited).jpg"]


def test_what_gets_refused(studio, make_user, app_client, anon):
    s, _, ev = studio
    e, cpath, gpath, ids = setup(s, ev)
    orig = ids["IMG_0.jpg"]
    assert save_copy(s, ev["id"], orig, b"\x89PNG\r\n\x1a\n" + b"x" * 100).json()["error"] == "not_jpeg"
    assert save_copy(s, ev["id"], orig, b"\xff\xd8\xff" + b"garbage" * 50).json()["error"] == "bad_image"
    assert save_copy(s, ev["id"], orig, jpeg_bytes(seed=900)).json()["error"] == "duplicate"   # identical to a photo there
    from test_inventory import member
    vw, _ = member(s, app_client, "vw@studio.test", "viewer")
    assert vw.get(f"/api/events/{ev['id']}/photos/{orig}/editor/source").status_code == 200       # viewers can look...
    assert save_copy(vw, ev["id"], orig, jpeg_bytes(seed=6)).status_code == 403                # ...not save
    ph, _ = member(s, app_client, "ph@studio.test", "photographer")
    assert save_copy(ph, ev["id"], orig, jpeg_bytes(seed=7)).status_code == 200                # photographers save
    other = make_user(email="b@studio.test", company="Studio B")
    assert other.get(f"/api/events/{ev['id']}/photos/{orig}/editor/source").status_code == 404
    assert save_copy(other, ev["id"], orig, jpeg_bytes(seed=8)).status_code == 404
    page = anon.get("/editor")
    assert page.status_code == 200 and "Photo editor" in page.text and "fonts.googleapis" not in page.text
