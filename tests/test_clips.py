"""Video clips in the client gallery: the cut is right, nothing is kept on the server, and the gallery's rules apply."""
import re
import subprocess

import pytest

from studio import clips, config

pytestmark = pytest.mark.skipif(not clips.available(), reason="needs ffmpeg (pip install imageio-ffmpeg)")


def duration(data: bytes, tmp_path) -> float:
    f = tmp_path / "out.mp4"
    f.write_bytes(data)
    info = subprocess.run([clips.ffmpeg(), "-hide_banner", "-i", str(f)], capture_output=True, text=True).stderr
    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", info).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


@pytest.fixture()
def video(studio, tmp_path_factory):
    """An event with a 6-second video (picture and sound) and its client gallery on: (session, event, gallery path, video id)."""
    s, _, ev = studio
    src = tmp_path_factory.mktemp("v") / "Ceremony.mp4"
    subprocess.run([clips.ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-shortest", str(src)], check=True)
    r = s.post(f"/api/events/{ev['id']}/videos", files=[("files", ("Ceremony.mp4", src.read_bytes(), "video/mp4"))]).json()
    assert r["added"] == 1
    e = s.patch(f"/api/events/{ev['id']}", json={"client_enabled": True}).json()
    path = e["client"]["url"].split("testserver", 1)[1]
    return s, e, path, r["results"][0]["id"]


def files_on_disk():
    return sorted(str(p) for p in config.settings.data_dir.rglob("*") if p.is_file() and "studio.db" not in p.name)


def test_the_clip_is_cut_streamed_and_never_stored(video, anon, tmp_path):
    s, e, path, vid = video
    assert anon.get(path + "/videos").json()[0]["clip"] is True
    before = files_on_disk()
    r = anon.get(f"{path}/video/{vid}/clip", params={"start": 1.5, "end": 4})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.headers["content-disposition"] == ("attachment; filename=\"Ceremony clip 00m01s-00m04s.mp4\"; "
                                                "filename*=UTF-8''Ceremony%20clip%2000m01s-00m04s.mp4")
    assert r.content[4:8] == b"ftyp"                                   # a real MP4
    assert abs(duration(r.content, tmp_path) - 2.5) < 0.15             # exactly the part asked for
    assert files_on_disk() == before                                   # no copy left on the server
    assert clips.busy.acquire(blocking=False)                          # and its slot is free again
    clips.busy.release()


def test_rules(video, anon, monkeypatch):
    s, e, path, vid = video
    url = f"{path}/video/{vid}/clip"
    assert anon.get(url, params={"start": 3, "end": 2}).json()["error"] == "range"
    assert anon.get(url, params={"start": 1, "end": 1.2}).json()["error"] == "range"
    assert anon.get(url, params={"start": 0, "end": clips.MAX_SECONDS + 1}).json()["error"] == "too_long"
    assert anon.get(f"{path}/video/nope/clip", params={"start": 0, "end": 1}).status_code == 404
    for _ in range(clips.BUSY_MAX):                                     # the server is busy making others
        clips.busy.acquire()
    try:
        assert anon.get(url, params={"start": 0, "end": 1}).json()["error"] == "busy"
    finally:
        for _ in range(clips.BUSY_MAX):
            clips.busy.release()

    s.patch(f"/api/events/{e['id']}", json={"client_download": False})   # downloads off: no clips either
    assert anon.get(path + "/videos").json()[0]["clip"] is False
    assert anon.get(url, params={"start": 0, "end": 1}).json()["error"] == "no_download"
    s.patch(f"/api/events/{e['id']}", json={"client_download": True})

    s.post(f"/api/events/{e['id']}/client-pin", json={"pin": "482913"})  # the gallery's PIN covers it
    assert anon.get(url, params={"start": 0, "end": 1}).status_code == 401
    s.post(f"/api/events/{e['id']}/client-pin", json={"pin": ""})

    monkeypatch.setattr(clips, "available", lambda: False)                # no ffmpeg on this server
    assert anon.get(path + "/videos").json()[0]["clip"] is False
    assert anon.get(url, params={"start": 0, "end": 1}).json()["error"] == "not_installed"


def test_clips_are_rate_limited(video, anon):
    _, _, path, vid = video
    codes = [anon.get(f"{path}/video/{vid}/clip", params={"start": 0, "end": 0.6}).status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


def test_ffmpeg_reads_only_the_declared_format_and_local_files(tmp_path):
    """A playlist dressed as a video can't make ffmpeg open other files (it is told the format, not left to guess)."""
    (tmp_path / "other.ts").write_bytes(b"\x47" * 188 * 50)
    (tmp_path / "evil.mp4").write_text("ffconcat version 1.0\nfile 'other.ts'\n")
    cmd = clips.command(tmp_path / "evil.mp4", 0, 2)
    assert cmd[cmd.index("-f") + 1] == "mov" and cmd[cmd.index("-protocol_whitelist") + 1] == "file,pipe"
    assert clips.command(tmp_path / "x.webm", 0, 1)[cmd.index("-f") + 1] == "matroska"
    out = subprocess.run(cmd, capture_output=True, timeout=60)
    assert len(out.stdout) == 0 and out.returncode != 0          # not opened as a playlist: nothing comes out


def test_any_language_name_one_clip_per_visitor(video, anon, monkeypatch):
    s, e, path, vid = video
    from studio import db
    db.run("UPDATE videos SET orig_name=? WHERE public_id=?", ("வரவேற்பு \"நடனம்\".mp4", vid))   # Tamil, with quotes
    r = anon.get(f"{path}/video/{vid}/clip", params={"start": 0, "end": 1})
    assert r.status_code == 200 and r.content[4:8] == b"ftyp"
    cd = r.headers["content-disposition"]
    assert cd.startswith('attachment; filename="') and "filename*=UTF-8''%E0%AE" in cd and '\\"' not in cd
    with clips.making_lock:
        clips.making.add("testclient")                                   # this visitor already has one being made
    try:
        assert anon.get(f"{path}/video/{vid}/clip", params={"start": 0, "end": 1}).json()["error"] == "busy"
    finally:
        clips.making.discard("testclient")
    assert anon.get(f"{path}/video/{vid}/clip", params={"start": 0, "end": 1}).status_code == 200
    assert not clips.making                                               # and it lets go when done
