"""Event videos: uploaded by the studio, played in the client gallery only, never shown to guests."""
import os

from studio import config, db

VIDEO = b"\x00\x00\x00\x18ftypmp42" + os.urandom(200_000)
WEBM = b"\x1a\x45\xdf\xa3" + os.urandom(50_000)


def up(s, eid, files):
    return s.post(f"/api/events/{eid}/videos", files=[("files", (n, b, "video/mp4")) for n, b in files])


def client_link(s, ev):
    s.patch(f"/api/events/{ev['id']}", json={"client_enabled": True})
    return s.get(f"/api/events/{ev['id']}").json()["client"]["url"].rsplit("/", 1)[1]


def test_upload_list_play_delete(studio):
    s, _, ev = studio
    r = up(s, ev["id"], [("First dance.mp4", VIDEO), ("notes.txt", b"x"), ("again.mp4", VIDEO)]).json()
    assert [x["status"] for x in r["results"]] == ["queued", "rejected", "duplicate"] and r["added"] == 1
    vids = s.get(f"/api/events/{ev['id']}/videos").json()
    assert [(v["name"], v["size"]) for v in vids] == [("First dance.mp4", len(VIDEO))]
    assert s.get(f"/api/events/{ev['id']}").json()["videos"] == 1
    play = s.c.get(f"/api/events/{ev['id']}/videos/{vids[0]['id']}", headers={"Range": "bytes=0-99"}, cookies={"pf_session": s.token})
    assert play.status_code == 206 and len(play.content) == 100 and play.headers["content-type"] == "video/mp4"  # seeking works
    assert s.get(f"/api/events/{ev['id']}/videos/{vids[0]['id']}?download=1").headers["content-disposition"].startswith("attachment")
    assert s.post(f"/api/events/{ev['id']}/videos/delete", json={"ids": [vids[0]["id"]]}).json() == {"deleted": 1}
    assert s.get(f"/api/events/{ev['id']}/videos").json() == []


def test_only_the_client_gallery_shows_videos(studio, anon):
    s, _, ev = studio
    s.patch(f"/api/events/{ev['id']}", json={"status": "published"})
    vid = up(s, ev["id"], [("film.mp4", VIDEO)]).json()["results"][0]["id"]
    token = client_link(s, ev)
    assert anon.get(f"/c/{token}/info").json()["videos"] == 1
    assert [v["id"] for v in anon.get(f"/c/{token}/videos").json()] == [vid]
    assert anon.get(f"/c/{token}/video/{vid}").content == VIDEO
    assert anon.get(f"/c/{token}/video/{vid}/download").headers["content-disposition"].startswith("attachment")
    # the guests' page has no way to them
    assert "videos" not in anon.get(f"/e/{ev['slug']}/info").json()
    for url in (f"/e/{ev['slug']}/videos", f"/e/{ev['slug']}/video/{vid}", f"/e/{ev['slug']}/web/{vid}.jpg",
                f"/e/{ev['slug']}/download/{vid}"):
        assert anon.get(url).status_code in (404, 405), url
    # downloads off: watch yes, download no; PIN: nothing without it
    s.patch(f"/api/events/{ev['id']}", json={"client_download": False})
    assert anon.get(f"/c/{token}/video/{vid}").status_code == 200
    assert anon.get(f"/c/{token}/video/{vid}/download").status_code == 403
    s.post(f"/api/events/{ev['id']}/client-pin", json={"pin": "4321"})
    assert anon.get(f"/c/{token}/videos").status_code == 401 and anon.get(f"/c/{token}/video/{vid}").status_code == 401
    assert anon.get(f"/c/{token}/info").json()["videos"] is None


def test_videos_count_towards_storage_and_have_a_size_limit(studio, monkeypatch):
    s, _, ev = studio
    up(s, ev["id"], [("a.mp4", VIDEO)])
    assert s.get("/api/auth/me").json()["usage"]["storage"] == len(VIDEO)
    db.run("UPDATE accounts SET storage_quota=?", (len(VIDEO) + 1000,))   # what the platform admin allocated
    r = up(s, ev["id"], [("b.mp4", VIDEO + b"\x01")]).json()["results"][0]
    assert r["status"] == "rejected" and "storage" in r["error"] and "is full" in r["error"]
    monkeypatch.setattr(config.settings, "max_video_mb", 0)
    assert "up to" in up(s, ev["id"], [("c.mov", VIDEO + b"\x02")]).json()["results"][0]["error"]


def test_roles_and_other_companies(studio, make_user):
    s, _, ev = studio
    vid = up(s, ev["id"], [("a.webm", WEBM)]).json()["results"][0]["id"]
    other = make_user("x@other.test", "Other")
    assert other.get(f"/api/events/{ev['id']}/videos").status_code == 404
    assert other.get(f"/api/events/{ev['id']}/videos/{vid}").status_code == 404
    me = s.get("/api/auth/me").json()["user"]["email"]
    db.run("UPDATE users SET role='viewer' WHERE email=?", (me,))
    assert s.get(f"/api/events/{ev['id']}/videos").status_code == 200
    assert up(s, ev["id"], [("b.mp4", b"\x03" + VIDEO)]).status_code == 403
    assert s.post(f"/api/events/{ev['id']}/videos/delete", json={"ids": [vid]}).status_code == 403
    db.run("UPDATE users SET role='owner' WHERE email=?", (me,))
    assert s.delete(f"/api/events/{ev['id']}").status_code == 200                    # deleting the event removes them
    assert db.q1("SELECT COUNT(*) n FROM videos")["n"] == 0


def test_only_real_videos_are_stored(studio):
    """A file named like a video must start like one: a playlist or a web page named .mp4 never gets stored."""
    s, _, ev = studio
    fakes = [("playlist.mp4", b"#EXTM3U\n#EXTINF:3,\n/etc/passwd\n"), ("concat.mov", b"ffconcat version 1.0\nfile 'x.mp4'\n"),
             ("page.mp4", b"<html><script>alert(1)</script></html>" + os.urandom(100)), ("mp4.webm", VIDEO)]
    for name, data in fakes:
        r = up(s, ev["id"], [(name, data)]).json()["results"][0]
        assert r["status"] == "rejected" and "isn't a real video" in r["error"], name
    assert s.get(f"/api/events/{ev['id']}/videos").json() == []
    assert up(s, ev["id"], [("ok.webm", WEBM), ("ok.mov", b"\x00\x00\x00\x14ftypqt  " + os.urandom(1000))]).json()["added"] == 2
