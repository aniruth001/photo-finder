"""Guest link PIN: nothing about the photos before the PIN, one entry per browser, changing it locks people out."""
import pytest

from conftest import jpeg_bytes, upload, wait_idle


@pytest.fixture()
def guests(studio):
    """A published event with 2 photos; returns (studio session, event, guest path, a photo id)."""
    s, _, ev = studio
    upload(s, ev["id"], [(f"IMG_{i}.jpg", jpeg_bytes(seed=700 + i)) for i in range(2)])
    wait_idle(s, ev["id"])
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published"}).json()
    pid = s.get(f"/api/events/{ev['id']}/photos").json()[0]["id"]
    return s, e, f"/e/{e['slug']}", pid


def test_no_pin_by_default(guests, anon):
    s, e, path, pid = guests
    assert e["guest_has_pin"] is False
    info = anon.get(path + "/info").json()
    assert info["has_pin"] is False and info["unlocked"] is True and info["ready"] == 2
    assert anon.get(f"{path}/thumb/{pid}.jpg").status_code == 200


def test_pin_protects_everything_and_changing_it_locks_people_out(guests, anon, app_client):
    s, e, path, pid = guests
    assert s.post(f"/api/events/{e['id']}/guest-pin", json={"pin": "12a4"}).json()["error"] == "pin"
    assert s.post(f"/api/events/{e['id']}/guest-pin", json={"pin": "4829"}).json()["guest_has_pin"] is True
    assert anon.get(path).status_code == 200                                    # the page itself opens, to ask for the PIN
    assert anon.get(path + "/info").json() == {"name": e["name"], "has_pin": True, "unlocked": False}  # nothing leaks
    for url in (f"{path}/thumb/{pid}.jpg", f"{path}/web/{pid}.jpg", f"{path}/download/{pid}"):
        assert anon.get(url).status_code == 401
    assert anon.post(path + "/zip", data={"ids": pid}).status_code == 401
    assert anon.post(path + "/search", files={"file": ("s.jpg", jpeg_bytes(), "image/jpeg")}).status_code == 401
    assert anon.post(path + "/unlock", json={"pin": "0000"}).status_code == 403

    c = app_client
    c.cookies.clear()
    r = c.post(path + "/unlock", json={"pin": "4829"})
    assert r.status_code == 200
    assert c.get(path + "/info").json()["unlocked"] is True and c.get(f"{path}/thumb/{pid}.jpg").status_code == 200
    assert c.post(path + "/search", files={"file": ("s.jpg", jpeg_bytes(), "image/jpeg")}).status_code == 422  # in: no face, not "locked"
    s.post(f"/api/events/{e['id']}/guest-pin", json={"pin": "111222"})
    s.c.cookies.clear()
    c.cookies.set(f"pf_g{e['id']}", r.cookies.get(f"pf_g{e['id']}"))
    assert c.get(f"{path}/thumb/{pid}.jpg").status_code == 401                 # the old PIN's browsers are locked again
    assert s.post(f"/api/events/{e['id']}/guest-pin", json={"pin": ""}).json()["guest_has_pin"] is False
    assert anon.get(f"{path}/thumb/{pid}.jpg").status_code == 200


def test_pin_guessing_is_limited_and_only_event_managers_set_it(guests, anon, make_user):
    s, e, path, _ = guests
    s.post(f"/api/events/{e['id']}/guest-pin", json={"pin": "482913"})
    codes = [anon.post(path + "/unlock", json={"pin": f"{i:06d}"}).status_code for i in range(11)]
    assert codes[:10] == [403] * 10 and codes[10] == 429
    assert anon.post(path + "/unlock", json={"pin": "482913"}).status_code == 429  # even the right one, for a while
    other = make_user(email="b@studio.test", company="Studio B")
    assert other.post(f"/api/events/{e['id']}/guest-pin", json={"pin": "1234"}).status_code == 404
