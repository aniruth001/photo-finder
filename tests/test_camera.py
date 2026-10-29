"""Camera upload: a real FTP client sends photos to an event, and can do nothing else."""
import ftplib
import io
import socket
import time

import pytest
from starlette.testclient import TestClient

from conftest import Sess, jpeg_bytes, wait_idle
from studio import config, db
from studio.camera import _Failures
from studio.server import create_app


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def cam(tmp_path, monkeypatch):
    """A Studio with camera upload on; yields (client, studio session, event, port)."""
    port = free_port()
    for k, v in {"public_url": "", "ftp_port": port, "ftp_bind": "127.0.0.1", "ftp_host": "", "ftp_cert": "",
                 "ftp_key": "", "ftp_require_tls": False, "ftp_passive": "", "ftp_max_per_ip": 40}.items():
        monkeypatch.setattr(config.settings, k, v)
    with TestClient(create_app(data_dir=tmp_path / "data", workers=1)) as c:
        r = c.post("/api/auth/signup", headers={"X-PF": "1"}, json={"company": "Lens", "email": "o@lens.test", "password": "longenough1"})
        s = Sess(c, r.cookies.get("pf_session"))
        db.run("UPDATE accounts SET storage_quota=?", (config.GB,))   # allocated by the platform admin
        cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
        ev = s.post("/api/events", json={"client_id": cid, "name": "Wedding"}).json()
        db.run("UPDATE events SET ftp_allowed=1 WHERE id=?", (ev["id"],))     # what the platform admin's switch does
        yield c, s, ev, port


def connect(port, user, password, tls=False):
    f = ftplib.FTP_TLS() if tls else ftplib.FTP()
    f.connect("127.0.0.1", port, timeout=10)
    f.login(user, password)
    if tls:
        f.prot_p()
    return f


def send(f, name, data):
    f.storbinary(f"STOR {name}", io.BytesIO(data))


def wait_status(s, eid, uploads, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        st = s.get(f"/api/events/{eid}/camera").json()
        if st["login"]["uploads"] >= uploads:
            return st
        time.sleep(0.2)
    raise AssertionError(f"camera uploads never reached {uploads}: {st}")


def test_camera_sends_photos_into_the_event(cam):
    c, s, ev, port = cam
    st = s.get(f"/api/events/{ev['id']}/camera").json()
    assert st["server"]["available"] and st["server"]["port"] == port and st["login"] is None
    made = s.post(f"/api/events/{ev['id']}/camera").json()
    user, pw = made["login"]["username"], made["password"]
    assert user.startswith("cam-") and len(pw) == 10
    assert "password" not in s.get(f"/api/events/{ev['id']}/camera").json()       # shown once only

    f = connect(port, user, pw)
    f.mkd("DCIM"); f.cwd("DCIM")                                                   # cameras often make folders
    send(f, "DSC_0001.JPG", jpeg_bytes(seed=601))
    send(f, "DSC_0002.JPG", jpeg_bytes(seed=602))
    send(f, "DSC_0002-again.JPG", jpeg_bytes(seed=602))                           # same photo twice
    f.quit()
    st = wait_status(s, ev["id"], 2)
    assert st["login"]["last_upload_at"] and "already uploaded" in st["login"]["last_note"].lower()
    e = wait_idle(s, ev["id"])
    assert e["photos"] == 2 and e["ready"] == 2                                    # processed like any upload
    names = {p["name"] for p in s.get(f"/api/events/{ev['id']}/photos").json()}
    assert names == {"DSC_0001.JPG", "DSC_0002.JPG"}


def test_a_camera_login_can_only_add_photos(cam):
    c, s, ev, port = cam
    made = s.post(f"/api/events/{ev['id']}/camera").json()
    f = connect(port, made["login"]["username"], made["password"])
    send(f, "one.jpg", jpeg_bytes(seed=611))
    wait_status(s, ev["id"], 1)
    assert f.nlst() == []                                                          # nothing to see
    with pytest.raises(ftplib.error_perm):
        f.retrbinary("RETR one.jpg", lambda b: None)                               # nothing to download
    with pytest.raises(ftplib.error_perm):
        f.delete("one.jpg")
    f.cwd("/"); f.cwd(".."); assert f.pwd() == "/"                                  # locked in its own inbox
    send(f, "notes.txt", b"hello")
    f.quit()
    time.sleep(0.5)
    assert len(s.get(f"/api/events/{ev['id']}/photos").json()) == 1                # the text file was not added


def test_temporary_name_then_rename(cam):
    c, s, ev, port = cam
    made = s.post(f"/api/events/{ev['id']}/camera").json()
    f = connect(port, made["login"]["username"], made["password"])
    send(f, "IMG_0001.JPG.tmp", jpeg_bytes(seed=621))
    f.rename("IMG_0001.JPG.tmp", "IMG_0001.JPG")
    f.quit()
    wait_status(s, ev["id"], 1)
    assert [p["name"] for p in s.get(f"/api/events/{ev['id']}/photos").json()] == ["IMG_0001.JPG"]


def test_wrong_rotated_switched_off_and_removed_logins_are_refused(cam):
    c, s, ev, port = cam
    made = s.post(f"/api/events/{ev['id']}/camera").json()
    user, pw = made["login"]["username"], made["password"]
    with pytest.raises(ftplib.error_perm):
        connect(port, user, "wrongpass1")
    new = s.post(f"/api/events/{ev['id']}/camera").json()                         # a new password
    assert new["login"]["username"] == user and new["password"] != pw
    with pytest.raises(ftplib.error_perm):
        connect(port, user, pw)                                                    # the old one stopped working
    connect(port, user, new["password"]).quit()
    s.patch(f"/api/events/{ev['id']}/camera", json={"enabled": False})
    with pytest.raises(ftplib.error_perm):
        connect(port, user, new["password"])
    s.patch(f"/api/events/{ev['id']}/camera", json={"enabled": True})
    db.run("UPDATE accounts SET disabled_at=1")
    with pytest.raises(ftplib.error_perm):
        connect(port, user, new["password"])                                       # suspended company
    db.run("UPDATE accounts SET disabled_at=NULL")
    s.delete(f"/api/events/{ev['id']}/camera")
    with pytest.raises(ftplib.error_perm):
        connect(port, user, new["password"])


def test_size_and_plan_limits_apply(cam, monkeypatch):
    c, s, ev, port = cam
    made = s.post(f"/api/events/{ev['id']}/camera").json()
    monkeypatch.setattr(config.settings, "max_file_mb", 1)
    f = connect(port, made["login"]["username"], made["password"])
    with pytest.raises((ftplib.error_temp, ftplib.error_perm, ConnectionError, EOFError, OSError)):
        send(f, "huge.jpg", jpeg_bytes(seed=631) + b"\0" * (3 * 1024 * 1024))     # stopped mid-transfer
    try:
        f.quit()
    except Exception:
        pass
    monkeypatch.setitem(config.PLANS["free"], "photos", 1)
    f = connect(port, made["login"]["username"], made["password"])
    send(f, "a.jpg", jpeg_bytes(seed=632))
    send(f, "b.jpg", jpeg_bytes(seed=633))
    f.quit()
    st = wait_status(s, ev["id"], 1)
    time.sleep(0.5)
    st = s.get(f"/api/events/{ev['id']}/camera").json()
    assert st["login"]["uploads"] == 1 and "limit" in st["login"]["last_note"].lower()
    assert len(s.get(f"/api/events/{ev['id']}/photos").json()) == 1


def test_ftps(tmp_path, monkeypatch):
    from netutil import ensure_cert

    crt, key = ensure_cert(tmp_path / "certs", "127.0.0.1")
    port = free_port()
    for k, v in {"public_url": "", "ftp_port": port, "ftp_bind": "127.0.0.1", "ftp_host": "", "ftp_passive": "",
                 "ftp_cert": str(crt), "ftp_key": str(key), "ftp_require_tls": True}.items():
        monkeypatch.setattr(config.settings, k, v)
    with TestClient(create_app(data_dir=tmp_path / "data", workers=1)) as c:
        r = c.post("/api/auth/signup", headers={"X-PF": "1"}, json={"company": "L", "email": "t@l.test", "password": "longenough1"})
        s = Sess(c, r.cookies.get("pf_session"))
        db.run("UPDATE accounts SET storage_quota=?", (config.GB,))   # allocated by the platform admin
        cid = s.post("/api/clients", json={"name": "C"}).json()["id"]
        eid = s.post("/api/events", json={"client_id": cid, "name": "E"}).json()["id"]
        db.run("UPDATE events SET ftp_allowed=1 WHERE id=?", (eid,))
        made = s.post(f"/api/events/{eid}/camera").json()
        assert made["server"]["tls"] == "required"
        with pytest.raises(ftplib.error_perm):
            connect(port, made["login"]["username"], made["password"])            # plain FTP refused
        f = connect(port, made["login"]["username"], made["password"], tls=True)
        send(f, "secure.jpg", jpeg_bytes(seed=641))
        f.quit()
        wait_status(s, eid, 1)
        monkeypatch.setattr(config.settings, "max_file_mb", 1)                   # the size limit holds over TLS too
        f = connect(port, made["login"]["username"], made["password"], tls=True)
        with pytest.raises((ftplib.error_temp, ftplib.error_perm, ConnectionError, EOFError, OSError)):
            send(f, "huge.jpg", jpeg_bytes(seed=642) + b"\0" * (3 * 1024 * 1024))
        time.sleep(0.5)
        assert s.get(f"/api/events/{eid}/camera").json()["login"]["uploads"] == 1


def test_who_may_manage_camera_logins(cam):
    c, s, ev, port = cam
    other = Sess(c, c.post("/api/auth/signup", headers={"X-PF": "1"},
                           json={"company": "B", "email": "b@b.test", "password": "longenough1"}).cookies.get("pf_session"))
    assert other.post(f"/api/events/{ev['id']}/camera").status_code == 404
    db.run("UPDATE users SET role='photographer' WHERE email='o@lens.test'")
    assert s.get(f"/api/events/{ev['id']}/camera").status_code == 403
    assert s.post(f"/api/events/{ev['id']}/camera").status_code == 403


def test_camera_upload_off_without_a_port(studio):
    s, _, ev = studio
    db.run("UPDATE events SET ftp_allowed=1 WHERE id=?", (ev["id"],))
    assert s.get(f"/api/events/{ev['id']}/camera").json()["server"]["available"] is False
    assert s.post(f"/api/events/{ev['id']}/camera").status_code == 409


def test_failed_logins_block_one_camera_login_not_the_whole_venue():
    from studio.camera import PER_ADDRESS, PER_LOGIN, PER_LOGIN_HERE

    f = _Failures(window=60)
    venue = "203.0.113.7"                                                          # every camera on the venue Wi-Fi
    for _ in range(PER_LOGIN_HERE):
        assert f.refusal(venue, "cam-aaaa") is None
        f.failed(venue, "cam-aaaa")                                                # camera A has a typo in its password
    assert "this camera login" in f.refusal(venue, "cam-aaaa")
    assert f.refusal(venue, "cam-bbbb") is None                                    # camera B on the same Wi-Fi still works
    assert f.refusal("198.51.100.1", "cam-aaaa") is None                           # and A from another network
    for i in range(PER_LOGIN):                                                     # one login tried from many addresses
        f.failed(f"10.9.{i // 250}.{i % 250}", "cam-cccc")
    assert f.refusal("192.0.2.99", "cam-cccc")
    g = _Failures(window=60)
    for i in range(PER_ADDRESS):                                                   # many logins tried from one address
        g.failed("192.0.2.50", f"cam-{i}")
    assert g.refusal("192.0.2.50", "cam-new") and g.refusal("192.0.2.51", "cam-new") is None


def test_only_events_the_platform_admin_enabled_get_camera_upload(cam):
    c, s, ev, port = cam
    eid = ev["id"]
    made = s.post(f"/api/events/{eid}/camera").json()
    user, pw = made["login"]["username"], made["password"]
    connect(port, user, pw).quit()

    # the company itself can't switch it on: only the platform admin can
    assert s.post(f"/api/admin/events/{eid}/ftp", json={"allowed": False}).status_code == 404
    db.run("UPDATE users SET platform_admin=1 WHERE email='o@lens.test'")
    assert s.post(f"/api/admin/events/{eid}/ftp", json={"allowed": False}).json() == {"id": eid, "ftp_allowed": False}

    st = s.get(f"/api/events/{eid}/camera").json()
    assert st["allowed"] is False and st["login"]["username"] == user            # the login is kept, just unusable
    with pytest.raises(ftplib.error_perm):
        connect(port, user, pw)                                                    # cameras stop at once
    assert s.post(f"/api/events/{eid}/camera").status_code == 403                  # no new logins
    assert s.patch(f"/api/events/{eid}/camera", json={"enabled": True}).status_code == 403
    assert s.patch(f"/api/events/{eid}/camera", json={"enabled": False}).status_code == 200   # switching off is fine

    s.post(f"/api/admin/events/{eid}/ftp", json={"allowed": True})
    s.patch(f"/api/events/{eid}/camera", json={"enabled": True})
    connect(port, user, pw).quit()                                                 # the same login works again
    acct = s.get("/api/auth/me").json()["account"]["id"]
    a = s.get(f"/api/admin/accounts/{acct}").json()
    assert a["ftp_server"] is True and a["event_list"][0]["ftp_allowed"] is True
    assert [l["action"] for l in a["log"]][:2] == ["ftp_on", "ftp_off"] and a["log"][0]["detail"] == "Wedding"
    assert s.post("/api/admin/events/99999/ftp", json={"allowed": True}).status_code == 404


def test_new_events_start_without_camera_upload(studio):
    s, _, ev = studio
    assert s.get(f"/api/events/{ev['id']}/camera").json()["allowed"] is False
    assert s.post(f"/api/events/{ev['id']}/camera").status_code == 403


def test_admin_sees_every_event_with_camera_ftp_on(cam):
    c, s, ev, port = cam
    assert s.get("/api/admin/camera").status_code == 404                          # platform admins only
    db.run("UPDATE users SET platform_admin=1 WHERE email='o@lens.test'")
    cid = s.post("/api/clients", json={"name": "D"}).json()["id"]
    other = s.post("/api/events", json={"client_id": cid, "name": "Reception"}).json()["id"]
    s.post("/api/events", json={"client_id": cid, "name": "Not enabled"})
    s.post(f"/api/admin/events/{other}/ftp", json={"allowed": True})
    made = s.post(f"/api/events/{ev['id']}/camera").json()
    f = connect(port, made["login"]["username"], made["password"])
    send(f, "DSC_1.JPG", jpeg_bytes(seed=651))
    f.quit()
    wait_status(s, ev["id"], 1)
    r = s.get("/api/admin/camera").json()
    assert r["server"]["available"] is True
    rows = {e["name"]: e for e in r["events"]}
    assert set(rows) == {"Wedding", "Reception"}                                   # only events with FTP on
    assert r["events"][0]["name"] == "Wedding"                                     # most recently active first
    w = rows["Wedding"]
    assert (w["state"], w["uploads"], w["username"], w["company"], w["client_name"]) == ("receiving", 1, made["login"]["username"], "Lens", "C")
    assert "pw_hash" not in w and rows["Reception"]["state"] == "no_login" and rows["Reception"]["username"] is None
    s.patch(f"/api/events/{ev['id']}/camera", json={"enabled": False})
    assert {e["name"]: e["state"] for e in s.get("/api/admin/camera").json()["events"]}["Wedding"] == "off"


def test_two_cameras_on_one_login_at_the_same_time(cam):
    """Both cameras call their first shot DSC_0001.JPG and send it at the same moment: both photos arrive."""
    c, s, ev, port = cam
    made = s.post(f"/api/events/{ev['id']}/camera").json()
    one, two = jpeg_bytes(seed=661), jpeg_bytes(seed=662)
    a = connect(port, made["login"]["username"], made["password"])
    b = connect(port, made["login"]["username"], made["password"])
    a.voidcmd("TYPE I")
    conn = a.transfercmd("STOR DSC_0001.JPG")                                      # camera A is half-way through...
    conn.sendall(one[: len(one) // 2])
    send(b, "DSC_0001.JPG", two)                                                   # ...when camera B sends its own
    conn.sendall(one[len(one) // 2:])
    conn.close()
    a.voidresp()
    a.quit(); b.quit()
    wait_status(s, ev["id"], 2)
    wait_idle(s, ev["id"])
    photos = s.get(f"/api/events/{ev['id']}/photos").json()
    assert [p["name"] for p in photos] == ["DSC_0001.JPG", "DSC_0001.JPG"] and all(p["status"] == "ready" for p in photos)
    time.sleep(0.3)
    from studio.camera import inbox
    assert not any(inbox(1).iterdir())                                             # connection folders are cleaned up



def test_many_cameras_can_share_one_address(cam):
    """Twelve cameras on one venue Wi-Fi (one address), all logged in at once: none is turned away."""
    c, s, ev, port = cam
    made = s.post(f"/api/events/{ev['id']}/camera").json()
    conns = [connect(port, made["login"]["username"], made["password"]) for _ in range(12)]
    for i, f in enumerate(conns):
        send(f, f"CAM{i:02}_0001.JPG", jpeg_bytes(seed=700 + i))
    for f in conns:
        f.quit()
    assert wait_status(s, ev["id"], 12)["login"]["uploads"] == 12
