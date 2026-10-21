"""Vendor uploads: an upload link per vendor; their photos and videos land in the event, tagged with their name."""
import os

from studio import db
from conftest import jpeg_bytes, wait_idle

VIDEO = b"\x00\x00\x00\x18ftypmp42" + os.urandom(50_000)
PF = {"X-PF": "1"}


def link(s, ev, name="Ravi (second shooter)"):
    v = s.post(f"/api/events/{ev['id']}/vendors", json={"name": name, "phone": "+91 90000 00000"}).json()
    return v, v["url"].rsplit("/", 1)[1]


def send(anon, token, kind, files):
    mime = "video/mp4" if kind == "videos" else "image/jpeg"
    return anon.c.post(f"/v/{token}/{kind}", headers=PF, files=[("files", (n, b, mime)) for n, b in files])


def test_vendor_sends_photos_and_videos_into_the_event(studio, anon):
    s, _, ev = studio
    v, token = link(s, ev)
    assert anon.get(f"/v/{token}").status_code == 200 and "Ravi (second shooter)" in anon.get(f"/v/{token}").text
    r = send(anon, token, "photos", [("DSC_1.jpg", jpeg_bytes(seed=701)), ("DSC_2.jpg", jpeg_bytes(seed=702)), ("x.txt", b"x")]).json()
    assert r["queued"] == 2 and [x["status"] for x in r["results"]] == ["queued", "queued", "rejected"]
    assert "id" not in r["results"][0]                                              # no way back to the photo itself
    assert send(anon, token, "photos", [("again.jpg", jpeg_bytes(seed=701))]).json()["results"][0]["status"] == "duplicate"
    assert send(anon, token, "videos", [("Drone.mp4", VIDEO)]).json()["added"] == 1
    wait_idle(s, ev["id"])
    photos = s.get(f"/api/events/{ev['id']}/photos").json()
    assert {(p["name"], p["vendor"], p["status"]) for p in photos} == {("DSC_1.jpg", v["name"], "ready"), ("DSC_2.jpg", v["name"], "ready")}
    assert s.get(f"/api/events/{ev['id']}/videos").json()[0]["vendor"] == v["name"]
    assert [p["name"] for p in s.get(f"/api/events/{ev['id']}/photos?vendor={v['id']}").json()] == ["DSC_2.jpg", "DSC_1.jpg"]
    assert s.get(f"/api/events/{ev['id']}/photos?vendor=any").json() != []
    info = anon.get(f"/v/{token}/info").json()
    assert (info["photos"], info["videos"], info["vendor"]) == (2, 1, v["name"])
    assert {x["name"] for x in info["sent"]} == {"DSC_1.jpg", "DSC_2.jpg", "Drone.mp4"}
    lst = s.get(f"/api/events/{ev['id']}/vendors").json()
    assert (lst[0]["photos"], lst[0]["ready"], lst[0]["videos"]) == (2, 2, 1) and lst[0]["last_upload_at"]


def test_the_studios_own_photos_are_not_tagged_or_shown(studio, anon):
    s, _, ev = studio
    s.post(f"/api/events/{ev['id']}/photos", files=[("files", ("own.jpg", jpeg_bytes(seed=711), "image/jpeg"))])
    _, token = link(s, ev)
    wait_idle(s, ev["id"])
    assert s.get(f"/api/events/{ev['id']}/photos").json()[0]["vendor"] is None
    assert s.get(f"/api/events/{ev['id']}/photos?vendor=any").json() == []
    assert anon.get(f"/v/{token}/info").json()["sent"] == []                         # the vendor sees only their own


def test_off_reset_delete_and_csrf(studio, anon):
    s, _, ev = studio
    v, token = link(s, ev)
    assert anon.c.post(f"/v/{token}/photos", files=[("files", ("a.jpg", jpeg_bytes(seed=721), "image/jpeg"))]).status_code == 403  # no X-PF
    assert s.patch(f"/api/events/{ev['id']}/vendors/{v['id']}", json={"enabled": False}).json()["enabled"] is False
    assert anon.get(f"/v/{token}").status_code == 404 and anon.get(f"/v/{token}/info").status_code == 404
    assert send(anon, token, "photos", [("a.jpg", jpeg_bytes(seed=721))]).status_code == 404
    s.patch(f"/api/events/{ev['id']}/vendors/{v['id']}", json={"enabled": True})
    send(anon, token, "photos", [("a.jpg", jpeg_bytes(seed=722))])
    new = s.post(f"/api/events/{ev['id']}/vendors/{v['id']}/reset").json()["url"].rsplit("/", 1)[1]
    assert new != token and anon.get(f"/v/{token}/info").status_code == 404 and anon.get(f"/v/{new}/info").status_code == 200
    wait_idle(s, ev["id"])
    assert s.delete(f"/api/events/{ev['id']}/vendors/{v['id']}").json() == {"ok": True}
    assert [p["vendor"] for p in s.get(f"/api/events/{ev['id']}/photos").json()] == [None]   # the photo stays
    assert anon.get(f"/v/{new}/info").status_code == 404


def test_validation_roles_and_other_companies(studio, make_user, anon):
    s, _, ev = studio
    assert s.post(f"/api/events/{ev['id']}/vendors", json={"name": "  "}).json()["error"] == "name"
    v, token = link(s, ev)
    other = make_user("z@other.test", "Other")
    assert other.get(f"/api/events/{ev['id']}/vendors").status_code == 404
    assert other.patch(f"/api/events/{ev['id']}/vendors/{v['id']}", json={"enabled": False}).status_code == 404
    assert other.delete(f"/api/events/{ev['id']}/vendors/{v['id']}").status_code == 404
    me = s.get("/api/auth/me").json()["user"]["email"]
    db.run("UPDATE users SET role='photographer' WHERE email=?", (me,))
    assert s.post(f"/api/events/{ev['id']}/vendors", json={"name": "X"}).status_code == 403
    db.run("UPDATE accounts SET disabled_at=1")                                      # a suspended studio: links stop
    assert anon.get(f"/v/{token}/info").status_code == 404
