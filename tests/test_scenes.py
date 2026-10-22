"""Search by description: background indexing, the client gallery's search and chips, the studio's switch, who sees
what. A small fake engine stands in for CLIP (it knows red, green and blue); one test runs the real model if present."""
import time

import numpy as np
import pytest

from conftest import jpeg_bytes, upload, wait_idle
from studio import db, scenes

RED, GREEN, BLUE = (220, 30, 30), (30, 200, 40), (30, 40, 220)


class FakeClip:
    """Images: a unit vector on the colour's axis. Text: 0.3 along the colour it names (a clear match, like a real
    CLIP score of 0.3), the rest on an axis no image uses; words it doesn't know point nowhere near any photo."""
    WORDS = {"red": 0, "mehendi": 0, "green": 1, "blue": 2}

    def images(self, ims):
        out = []
        for im in ims:
            v = np.zeros(scenes.DIM, np.float32)
            v[int(np.argmax(np.asarray(im.convert("RGB"), np.float32).reshape(-1, 3).mean(0)))] = 1
            out.append(v)
        return np.stack(out)

    def text(self, s):
        v = np.zeros(scenes.DIM, np.float32)
        hit = next((k for w, k in self.WORDS.items() if w in s.lower()), None)
        if hit is None:
            v[500] = 1
        else:
            v[hit], v[499] = 0.3, np.sqrt(1 - 0.09)
        return v


@pytest.fixture()
def fake():
    scenes.set_engine(FakeClip())
    yield
    scenes.set_engine(None)


def index_all(app_client, timeout=30):
    app_client.app_ref.state.scene_indexer.poke()
    end = time.time() + timeout
    while time.time() < end:
        if not scenes.SceneIndexer.todo(1):
            return
        time.sleep(0.2)
    raise AssertionError("photos were not indexed in time")


@pytest.fixture()
def gallery(fake, studio, app_client):
    """An event with 2 red, 1 green and 1 blue photo, indexed, with its client gallery on."""
    s, _, ev = studio
    upload(s, ev["id"], [("R1.jpg", jpeg_bytes(RED, seed=1)), ("R2.jpg", jpeg_bytes(RED, seed=2)),
                         ("G1.jpg", jpeg_bytes(GREEN, seed=3)), ("B1.jpg", jpeg_bytes(BLUE, seed=4))])
    wait_idle(s, ev["id"])
    index_all(app_client)
    e = s.patch(f"/api/events/{ev['id']}", json={"client_enabled": True}).json()
    return s, e, e["client"]["url"].split("testserver", 1)[1]


def test_client_searches_by_description_and_gets_chips(gallery, anon):
    s, e, path = gallery
    st = anon.get(path + "/scenes").json()
    assert st["available"] is True and st["tags"] == [{"label": "Mehendi", "count": 2}]   # 2 red photos; 1 isn't enough
    r = anon.get(path + "/describe", params={"q": "something red"}).json()
    assert r["clear"] is True and sorted(p["name"] for p in r["photos"]) == ["R1.jpg", "R2.jpg"]
    assert anon.get(path + "/describe", params={"q": "Mehendi"}).json()["clear"] is True       # a chip's own wording
    r = anon.get(path + "/describe", params={"q": "a purple elephant"}).json()
    assert r["clear"] is False and len(r["photos"]) == 4          # nothing clear: the closest, flagged as such
    assert anon.get(path + "/describe", params={"q": "  "}).status_code == 422
    assert anon.get(path + "/describe", params={"q": "x" * 101}).status_code == 422
    assert s.get(f"/api/events/{e['id']}/scenes").json() == {"available": True, "ready": 4, "indexed": 4,
                                                             "tags": [{"label": "Mehendi", "count": 2}]}


def test_pin_and_switch_protect_it(gallery, anon, app_client):
    s, e, path = gallery
    s.post(f"/api/events/{e['id']}/client-pin", json={"pin": "482913"})
    assert anon.get(path + "/scenes").status_code == 401 and anon.get(path + "/describe?q=red").status_code == 401
    s.post(f"/api/events/{e['id']}/client-pin", json={"pin": ""})

    assert s.post("/api/scenes", json={"enabled": False}).json()["indexed"] == 0     # off: the vectors are deleted
    assert db.q1("SELECT COUNT(*) n FROM photo_scenes")["n"] == 0
    assert anon.get(path + "/scenes").json() == {"available": False, "tags": []}
    assert anon.get(path + "/describe?q=red").json()["error"] == "off"
    assert scenes.SceneIndexer.todo(10) == []                                      # and nothing is indexed again
    s.post("/api/scenes", json={"enabled": True})
    index_all(app_client)
    assert len(anon.get(path + "/describe?q=red").json()["photos"]) == 2


def test_studio_search_roles_deletion_and_isolation(gallery, make_user, app_client):
    s, e, _ = gallery
    r = s.get(f"/api/events/{e['id']}/describe", params={"q": "blue"}).json()
    assert r["clear"] and [(p["name"], p["status"], p["vendor"]) for p in r["photos"]] == [("B1.jpg", "ready", None)]
    blue = r["photos"][0]["id"]
    s.post(f"/api/events/{e['id']}/photos/delete", json={"ids": [blue]})
    assert db.q1("SELECT COUNT(*) n FROM photo_scenes")["n"] == 3                   # its vector went with it
    assert s.get(f"/api/events/{e['id']}/describe", params={"q": "blue"}).json()["clear"] is False

    from test_inventory import member
    mgr, _ = member(s, app_client, "m@studio.test", "manager")
    assert mgr.get(f"/api/events/{e['id']}/describe?q=red").status_code == 200   # everyone in the studio can search
    assert mgr.post("/api/scenes", json={"enabled": False}).status_code == 403    # only the company settings role switches
    other = make_user(email="b@studio.test", company="Studio B")
    assert other.get(f"/api/events/{e['id']}/describe?q=red").status_code == 404
    assert other.get(f"/api/events/{e['id']}/scenes").status_code == 404
    assert other.get("/api/scenes").json()["ready"] == 0


def test_without_the_model_nothing_is_offered(studio, anon):
    s, _, ev = studio
    e = s.patch(f"/api/events/{ev['id']}", json={"client_enabled": True}).json()
    path = e["client"]["url"].split("testserver", 1)[1]
    assert s.get("/api/scenes").json()["installed"] is False
    assert anon.get(path + "/scenes").json() == {"available": False, "tags": []}
    assert anon.get(path + "/describe?q=cake").json()["error"] == "not_installed"


def test_strong_matches_follow_the_measured_rule():
    # from the real reception: common searches score high; a rare one counts only when it stands clear of the rest
    rng = np.random.default_rng(7)
    spread = lambda mean, sd: np.clip(rng.normal(mean, sd, 100), 0, mean + 1.5 * sd)  # an event's ordinary photos
    common = np.concatenate([[0.27, 0.268, 0.265, 0.26], spread(0.227, 0.012)])         # "the couple on stage"
    assert strong_count(common) == 4
    rare = np.concatenate([[0.25, 0.244, 0.243], spread(0.206, 0.012)])                 # "a selfie": 3 of them
    assert strong_count(rare) == 3
    junk = np.concatenate([[0.242, 0.236], spread(0.221, 0.011)])                       # "dancing", with no dancing
    assert strong_count(junk) == 0
    assert strong_count(np.array([0.24])) == 0 and strong_count(np.zeros(0)) == 0


def strong_count(s):
    return int(scenes.strong(s.astype(np.float32)).sum())


@pytest.mark.skipif(not all((scenes.MODEL_DIR / f).exists() for f in scenes.FILES), reason="run python -m studio get-scene-model")
def test_the_real_model_tells_colours_and_things_apart():
    from PIL import Image, ImageDraw

    clip = scenes.ClipEngine(threads=2)
    red, blue = Image.new("RGB", (300, 200), RED), Image.new("RGB", (300, 200), BLUE)
    cake = Image.new("RGB", (300, 300), "white")
    d = ImageDraw.Draw(cake)
    d.rectangle((60, 150, 240, 260), fill=(240, 220, 200)); d.rectangle((60, 120, 240, 150), fill=(250, 250, 250))
    for x in range(80, 240, 40):
        d.rectangle((x, 70, x + 8, 120), fill=(250, 200, 60))
    v = clip.images([red, blue])
    assert v.shape == (2, 512) and np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-4)
    assert v[0] @ clip.text("a photo of something red") > v[1] @ clip.text("a photo of something red")
    assert v[1] @ clip.text("a photo of something blue") > v[0] @ clip.text("a photo of something blue")
    long_text = clip.text("a photo of " + "very " * 100 + "long")                    # over 77 tokens: cut, not an error
    assert long_text.shape == (512,)
