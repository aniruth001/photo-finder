"""Live updates (studio/live.py): open dashboards get a nudge when something changes. And the editor works on the original
photo: its full size, its format."""
import io
import json
import threading
import time

from PIL import Image

from conftest import jpeg_bytes, upload, wait_idle
from studio import db, live


def read_events(resp, n, timeout=10):
    """The first n "data:" messages of an event stream (after the "ready" one)."""
    out, end = [], time.time() + timeout
    for line in resp.iter_lines():
        if line.startswith("data:"):
            d = json.loads(line[5:])
            if "topic" in d:
                out.append(d)
                if len(out) >= n:
                    break
        if time.time() > end:
            break
    return out


def account(s):
    return s.get("/api/auth/me").json()["account"]["id"]


def test_a_change_reaches_open_dashboards(studio, make_user, app_client):
    s, _, ev = studio
    aid = account(s)

    def later():
        time.sleep(0.6)
        s.req("PATCH", f"/api/events/{ev['id']}", json={"subtitle": "Reception at 7"}, headers={"X-PF-Tab": "tab-a"})

    threading.Thread(target=later).start()   # the test client hands a stream back only once it has ended
    with app_client.stream("GET", "/api/live?once=1", cookies={"pf_session": s.token}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        msgs = read_events(r, 1)
    assert msgs and msgs[0]["topic"] == "event" and msgs[0]["event"] == ev["id"] and msgs[0]["tab"] == "tab-a"
    assert live.bus.listeners(aid) == 0          # the connection is let go when it ends


def test_background_and_client_changes_are_sent(studio, app_client, anon):
    s, _, ev = studio
    aid = account(s)
    upload(s, ev["id"], [("a.jpg", jpeg_bytes(seed=71))])
    wait_idle(s, ev["id"])
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    cpath = e["client"]["url"].split("testserver", 1)[1]
    pid = s.get(f"/api/events/{ev['id']}/photos").json()[0]["id"]

    def later():
        time.sleep(0.6)
        anon.post(cpath + "/album", json={"ids": [pid]})   # the client picks a photo: no login, still a nudge
        live.publish(aid, "photos", event=ev["id"])          # as the photo workers do from their thread

    threading.Thread(target=later).start()
    with app_client.stream("GET", "/api/live?once=2", cookies={"pf_session": s.token}) as r:
        msgs = read_events(r, 2)
    assert [m["topic"] for m in msgs] == ["album", "photos"] and all(m["event"] == ev["id"] for m in msgs)


def test_live_needs_a_login_and_is_per_company(make_user, anon):
    assert anon.get("/api/live").status_code == 401
    a, b = make_user(), make_user(email="b@studio.test", company="Studio B")
    seen = []
    loop_bus = live.Bus()
    # a nudge for one company never reaches another's subscribers
    import asyncio

    async def run():
        sa, sb = loop_bus.subscribe(1), loop_bus.subscribe(2)
        loop_bus.publish(1, "tasks")
        await asyncio.sleep(0.05)
        seen.append((sa[1].qsize(), sb[1].qsize()))
    asyncio.run(run())
    assert seen == [(1, 0)]


def test_the_editor_gets_the_original_and_saves_in_its_format(studio):
    s, _, ev = studio
    big = io.BytesIO(); Image.new("RGB", (7000, 3000), (30, 90, 160)).save(big, "JPEG", quality=90)
    png = io.BytesIO(); Image.new("RGBA", (640, 480), (200, 50, 50, 128)).save(png, "PNG")
    upload(s, ev["id"], [("wide.jpg", big.getvalue())])
    s.post(f"/api/events/{ev['id']}/photos", files=[("files", ("logo.png", png.getvalue(), "image/png"))])
    wait_idle(s, ev["id"])
    ids = {p["name"]: p["id"] for p in s.get(f"/api/events/{ev['id']}/photos").json()}

    src = s.get(f"/api/events/{ev['id']}/photos/{ids['wide.jpg']}/editor/source")
    assert src.content == big.getvalue()                                                 # the file itself: 7000 px, not 6000
    info = s.get(f"/api/events/{ev['id']}/photos/{ids['logo.png']}/editor/info").json()
    assert info["format"] == "png" and info["ext"] == ".png"
    src = s.get(f"/api/events/{ev['id']}/photos/{ids['logo.png']}/editor/source")
    assert src.headers["content-type"] == "image/png" and src.content == png.getvalue()

    edited = io.BytesIO(); Image.new("RGBA", (640, 480), (20, 200, 50, 100)).save(edited, "PNG")
    c = s.post(f"/api/events/{ev['id']}/photos/{ids['logo.png']}/edits", files={"file": ("e.png", edited.getvalue(), "image/png")}).json()
    assert c["name"] == "logo (edited).png" and c["format"] == "png"                     # a PNG stays a PNG
    wait_idle(s, ev["id"])
    assert Image.open(io.BytesIO(s.get(f"/api/events/{ev['id']}/photos/{c['id']}/orig").content)).mode == "RGBA"
    wrong = s.put(f"/api/events/{ev['id']}/photos/{c['id']}/edit", files={"file": ("e.jpg", jpeg_bytes(seed=72), "image/jpeg")})
    assert wrong.status_code == 422 and wrong.json()["error"] == "wrong_format"
    assert s.post(f"/api/events/{ev['id']}/photos/{ids['logo.png']}/edits",
                  files={"file": ("e.gif", b"GIF89a" + b"x" * 50, "image/gif")}).json()["error"] == "not_jpeg"
