"""Album selection: the client picks photos in the client gallery; the studio sees them on the event page."""
import io
import zipfile

from conftest import jpeg_bytes, upload, wait_idle


def setup(s, ev, n=4):
    upload(s, ev["id"], [(f"IMG_{i}.jpg", jpeg_bytes(seed=600 + i)) for i in range(n)])
    wait_idle(s, ev["id"])
    s.patch(f"/api/events/{ev['id']}", json={"client_enabled": True})
    token = s.get(f"/api/events/{ev['id']}").json()["client"]["url"].rsplit("/", 1)[1]
    ids = [p["id"] for p in sorted(s.get(f"/api/events/{ev['id']}/photos?limit=50").json(), key=lambda p: p["name"])]
    return token, ids


def test_client_sends_photos_and_the_studio_sees_them(studio, anon):
    s, _, ev = studio
    token, ids = setup(s, ev)
    r = anon.post(f"/c/{token}/album", json={"ids": [ids[0], ids[2], "nope"], "note": "Please make IMG_0 the cover"}).json()
    assert (r["added"], r["count"], r["note"]) == (2, 2, "Please make IMG_0 the cover") and r["sent_at"]
    r = anon.post(f"/c/{token}/album", json={"ids": [ids[2], ids[3]]}).json()        # adds; the note stays
    assert (r["count"], r["note"]) == (3, "Please make IMG_0 the cover")
    a = s.get(f"/api/events/{ev['id']}/album").json()
    assert [p["name"] for p in a["photos"]] == ["IMG_0.jpg", "IMG_2.jpg", "IMG_3.jpg"]
    assert s.get(f"/api/events/{ev['id']}").json()["album"] == 3
    z = zipfile.ZipFile(io.BytesIO(s.get(f"/api/events/{ev['id']}/album.zip").content))
    assert sorted(z.namelist()) == ["IMG_0.jpg", "IMG_2.jpg", "IMG_3.jpg"]
    assert anon.post(f"/c/{token}/album/remove", json={"ids": [ids[3]]}).json()["count"] == 2
    assert s.post(f"/api/events/{ev['id']}/album/remove", json={"ids": [ids[2]]}).json()["count"] == 1
    assert anon.post(f"/c/{token}/album", json={"ids": []}).status_code == 400


def test_lock_deleted_photos_and_pin(studio, anon):
    s, _, ev = studio
    token, ids = setup(s, ev, 3)
    anon.post(f"/c/{token}/album", json={"ids": ids})
    s.post(f"/api/events/{ev['id']}/photos/delete", json={"ids": [ids[1]]})
    assert anon.get(f"/c/{token}/album").json()["count"] == 2                    # a deleted photo leaves the list
    assert s.post(f"/api/events/{ev['id']}/album/lock", json={"locked": True}).json()["locked"] is True
    assert anon.post(f"/c/{token}/album", json={"ids": ids}).json()["error"] == "locked"
    assert anon.post(f"/c/{token}/album/remove", json={"ids": ids}).status_code == 409
    assert anon.get(f"/c/{token}/album").json()["locked"] is True
    s.post(f"/api/events/{ev['id']}/album/lock", json={"locked": False})
    s.post(f"/api/events/{ev['id']}/client-pin", json={"pin": "2468"})
    assert anon.get(f"/c/{token}/album").status_code == 401
    assert anon.post(f"/c/{token}/album", json={"ids": ids}).status_code == 401


def test_other_companies_and_roles(studio, make_user):
    s, _, ev = studio
    other = make_user("y@other.test", "Other")
    assert other.get(f"/api/events/{ev['id']}/album").status_code == 404
    assert other.get(f"/api/events/{ev['id']}/album.zip").status_code == 404
    assert other.post(f"/api/events/{ev['id']}/album/lock", json={"locked": True}).status_code == 404
    assert s.get(f"/api/events/{ev['id']}/album.zip").status_code == 400            # nothing chosen yet


def test_studio_adds_photos_and_everyone_sees_who_chose_what(studio, anon, app_client):
    s, _, ev = studio
    token, ids = setup(s, ev, 4)
    anon.post(f"/c/{token}/album", json={"ids": [ids[0], ids[1]]})                      # the client: 0 and 1
    r = s.post(f"/api/events/{ev['id']}/album/add", json={"ids": [ids[1], ids[2], "nope"]}).json()   # the studio: 1 and 2
    assert r["added"] == 2 and r["by"] == {"client": 1, "studio": 1, "both": 1}
    who = {p["name"]: (p["by"], p["picker"]) for p in r["photos"]}
    assert who == {"IMG_0.jpg": ("client", ""), "IMG_1.jpg": ("both", "Owner"), "IMG_2.jpg": ("studio", "Owner")}
    anon.post(f"/c/{token}/album", json={"ids": [ids[2]]})                               # the client now chose 2 as well
    assert {p["name"]: p["by"] for p in anon.get(f"/c/{token}/album").json()["photos"]}["IMG_2.jpg"] == "both"
    s.post(f"/api/events/{ev['id']}/album/add", json={"ids": [ids[0]]})                  # adding again changes nothing twice
    assert s.get(f"/api/events/{ev['id']}/album").json()["by"] == {"client": 0, "studio": 0, "both": 3}

    listed = {p["name"]: p["album"] for p in s.get(f"/api/events/{ev['id']}/photos?limit=50").json()}
    assert listed == {"IMG_0.jpg": "both", "IMG_1.jpg": "both", "IMG_2.jpg": "both", "IMG_3.jpg": None}
    s.post(f"/api/events/{ev['id']}/album/add", json={"ids": [ids[3]]})
    names = lambda q: sorted(p["name"] for p in s.get(f"/api/events/{ev['id']}/photos?limit=50&album={q}").json())
    assert names("studio") == ["IMG_0.jpg", "IMG_1.jpg", "IMG_2.jpg", "IMG_3.jpg"] and names("client") == ["IMG_0.jpg", "IMG_1.jpg", "IMG_2.jpg"]
    assert names("any") == names("studio")

    s.post(f"/api/events/{ev['id']}/album/lock", json={"locked": True})                 # locked: the client can't, the studio can
    assert anon.post(f"/c/{token}/album/remove", json={"ids": [ids[3]]}).status_code == 409
    assert s.post(f"/api/events/{ev['id']}/album/remove", json={"ids": [ids[3]]}).json()["count"] == 3
    assert s.post(f"/api/events/{ev['id']}/album/add", json={"ids": [ids[3]]}).json()["count"] == 4
    assert s.post(f"/api/events/{ev['id']}/album/add", json={"ids": []}).status_code == 400

    from test_inventory import member
    ph, _ = member(s, app_client, "ph@studio.test", "photographer")
    assert ph.post(f"/api/events/{ev['id']}/album/add", json={"ids": [ids[0]]}).status_code == 403   # roles that manage events
