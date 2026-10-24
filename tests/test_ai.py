"""Culling (blurry, eyes, near-duplicates), editor presets and recipes, and the AI helpers (cut-out, eraser, upscaling)."""
import io
import json
import time

import pytest
from PIL import Image, ImageDraw, ImageFilter

from conftest import jpeg_bytes, upload, wait_idle
from studio import ai, culling, db


def detailed(seed=0, blur=0.0, size=(640, 480)):
    """A photo with plenty of edges (stripes and shapes), optionally blurred."""
    im = Image.new("RGB", size, (200, 190, 170))
    d = ImageDraw.Draw(im)
    for k in range(0, size[0], 16):
        d.line([(k + seed % 7, 0), (k + seed % 7 + 40, size[1])], fill=(20 + k % 200, 40, 90), width=3)
    for k in range(12):
        x, y = (k * 53 + seed * 11) % size[0], (k * 37 + seed * 5) % size[1]
        d.ellipse([x, y, x + 40, y + 30], outline=(250, 250, 250), width=4)
    if blur:
        im = im.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=92)
    return buf.getvalue()


def test_culling_measures_and_filters(studio):
    s, _, ev = studio
    upload(s, ev["id"], [("A_001.jpg", detailed(1)), ("A_002.jpg", detailed(2)), ("A_003.jpg", detailed(1, blur=4)),
                         ("Z_flat.jpg", jpeg_bytes(seed=3))])
    wait_idle(s, ev["id"])
    rows = {r["orig_name"]: r for r in db.q("SELECT orig_name, blur, dhash, eyes_shut FROM photos WHERE event_id=?", (ev["id"],))}
    assert rows["A_001.jpg"]["blur"] < culling.BLURRY <= rows["A_003.jpg"]["blur"]       # sharp vs. blurred copy
    assert rows["Z_flat.jpg"]["blur"] < culling.BLURRY                                    # plain is not blurry
    assert culling.blur_score(__import__("numpy").full((300, 400), 128, "uint8")) is None   # nothing at all to judge
    assert all(r["dhash"] is not None for r in rows.values())

    summary = s.get(f"/api/events/{ev['id']}/culling").json()
    assert summary["photos"] == summary["analyzed"] == 4 and summary["blurry"] == 1
    assert [p["name"] for p in s.get(f"/api/events/{ev['id']}/photos?cull=blurry").json()] == ["A_003.jpg"]
    listed = {p["name"]: p for p in s.get(f"/api/events/{ev['id']}/photos").json()}
    assert listed["A_003.jpg"]["blurry"] and not listed["A_001.jpg"]["blurry"] and listed["A_001.jpg"]["eyes"] == 0

    # the same moment three times: one group, the sharp one first and marked best
    sim = s.get(f"/api/events/{ev['id']}/photos?cull=similar").json()
    group = [p for p in sim if p["name"].startswith("A_")]
    assert len(group) >= 2 and group[0]["best"] and not group[0]["blurry"] and len({p["group"] for p in group}) == 1
    assert summary["similar"] >= 1 and summary["groups"] >= 1


def test_photos_processed_earlier_are_measured_in_the_background(studio):
    s, _, ev = studio
    upload(s, ev["id"], [("old.jpg", detailed(5))])
    wait_idle(s, ev["id"])
    db.run("UPDATE photos SET blur=NULL, dhash=NULL, eyes_shut=NULL WHERE event_id=?", (ev["id"],))   # as before culling existed
    s.c.app_ref.state.processor.poke()
    end = time.time() + 30
    while time.time() < end and db.q1("SELECT blur FROM photos WHERE event_id=?", (ev["id"],))["blur"] is None:
        time.sleep(0.3)
    assert db.q1("SELECT blur FROM photos WHERE event_id=?", (ev["id"],))["blur"] is not None


def test_presets_and_recipes(studio, make_user, app_client):
    s, _, ev = studio
    upload(s, ev["id"], [("IMG_1.jpg", detailed(7))])
    wait_idle(s, ev["id"])
    pid = s.get(f"/api/events/{ev['id']}/photos").json()[0]["id"]
    look = {"steps": [{"op": "adjust", "v": {"exposure": 0.3, "brightness": 0, "contrast": 10, "saturation": 5, "hue": 0, "temperature": 12}},
                      {"op": "autofix", "v": {}}]}
    r = s.post("/api/edit-presets", json={"name": "  Warm   wedding ", "recipe": json.dumps(look)}).json()
    assert r["name"] == "Warm wedding" and r["recipe"]["steps"][0]["op"] == "adjust"
    assert s.post("/api/edit-presets", json={"name": "Warm wedding", "recipe": json.dumps(look)}).json()["error"] == "exists"
    assert s.post("/api/edit-presets", json={"name": "Warm wedding", "recipe": json.dumps({"steps": look["steps"][:1]}), "replace": True}).status_code == 200
    assert len(s.get("/api/edit-presets").json()[0]["recipe"]["steps"]) == 1
    assert s.post("/api/edit-presets", json={"name": "Bad", "recipe": json.dumps({"steps": [{"op": "eval", "v": {}}]})}).json()["error"] == "bad_recipe"
    assert s.post("/api/edit-presets", json={"name": "Empty", "recipe": json.dumps({"steps": []})}).json()["error"] == "empty"
    other = make_user(email="b@studio.test", company="Studio B")
    assert other.get("/api/edit-presets").json() == []                                   # each company has its own
    assert other.delete(f"/api/edit-presets/{r['id']}").status_code == 404

    # a copy saved with its steps opens again with them; saving without steps clears them
    c = s.post(f"/api/events/{ev['id']}/photos/{pid}/edits", files={"file": ("e.jpg", detailed(8), "image/jpeg")},
               data={"recipe": json.dumps(look)}).json()
    assert c["recipe"]["steps"][1]["op"] == "autofix"
    wait_idle(s, ev["id"])
    info = s.get(f"/api/events/{ev['id']}/photos/{c['id']}/editor/info").json()
    assert info["recipe"]["steps"][0]["v"]["exposure"] == 0.3 and info["source"]["id"] == pid
    u = s.put(f"/api/events/{ev['id']}/photos/{c['id']}/edit", files={"file": ("e.jpg", detailed(9), "image/jpeg")}).json()
    assert u["recipe"] is None
    assert s.post(f"/api/events/{ev['id']}/photos/{pid}/edits", files={"file": ("e.jpg", detailed(10), "image/jpeg")},
                  data={"recipe": "{not json"}).json()["error"] == "bad_recipe"
    assert s.delete(f"/api/edit-presets/{r['id']}").json()["ok"]


needs_models = pytest.mark.skipif(not all(ai.installed(k) for k in ("cutout", "erase", "upscale")),
                                  reason="AI models not downloaded (python -m studio get-ai-models)")


@needs_models
def test_cutout_erase_and_faces(studio, anon):
    s, _, ev = studio
    status = s.get("/api/ai").json()
    assert status["cutout"] and status["erase"] and status["upscale"]
    im = Image.new("RGB", (400, 300), (40, 120, 200))
    ImageDraw.Draw(im).ellipse([140, 60, 260, 240], fill=(230, 200, 160))
    buf = io.BytesIO(); im.save(buf, "JPEG")
    m = s.post("/api/ai/cutout", files={"file": ("p.jpg", buf.getvalue(), "image/jpeg")})
    assert m.status_code == 200 and m.headers["content-type"] == "image/png"
    mask = Image.open(io.BytesIO(m.content))
    assert mask.size == (400, 300) and mask.mode == "L"

    crop = Image.new("RGB", (512, 512), (90, 140, 60)); ImageDraw.Draw(crop).rectangle([200, 200, 300, 300], fill=(255, 0, 0))
    hole = Image.new("L", (512, 512), 0); ImageDraw.Draw(hole).rectangle([190, 190, 310, 310], fill=255)
    bc, bm = io.BytesIO(), io.BytesIO(); crop.save(bc, "PNG"); hole.save(bm, "PNG")
    out = s.post("/api/ai/erase", files={"file": ("c.png", bc.getvalue(), "image/png"), "mask": ("m.png", bm.getvalue(), "image/png")})
    assert out.status_code == 200
    r, g, b = Image.open(io.BytesIO(out.content)).convert("RGB").getpixel((250, 250))
    assert r < 200 and g > 80                                                            # the red square is gone
    small = io.BytesIO(); crop.resize((100, 100)).save(small, "PNG")
    assert s.post("/api/ai/erase", files={"file": ("c.png", small.getvalue(), "image/png"), "mask": ("m.png", small.getvalue(), "image/png")}).json()["error"] == "bad_size"

    assert s.post("/api/ai/faces", files={"file": ("p.jpg", buf.getvalue(), "image/jpeg")}).json()["faces"] == []
    assert anon.post("/api/ai/cutout", files={"file": ("p.jpg", buf.getvalue(), "image/jpeg")}).status_code == 401


@needs_models
def test_upscale_job_saves_an_edited_copy(studio, make_user):
    s, _, ev = studio
    upload(s, ev["id"], [("small.jpg", detailed(11, size=(200, 150)))])
    wait_idle(s, ev["id"])
    pid = s.get(f"/api/events/{ev['id']}/photos").json()[0]["id"]
    assert s.post(f"/api/events/{ev['id']}/photos/{pid}/upscale", json={"scale": 3}).json()["error"] == "bad_scale"
    job = s.post(f"/api/events/{ev['id']}/photos/{pid}/upscale", json={"scale": 2}).json()
    assert job["status"] in ("queued", "running")
    assert s.post(f"/api/events/{ev['id']}/photos/{pid}/upscale", json={"scale": 2}).json()["id"] == job["id"]   # not twice
    end = time.time() + 120
    while time.time() < end:
        j = next(x for x in s.get(f"/api/events/{ev['id']}/ai-jobs").json() if x["id"] == job["id"])
        if j["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    assert j["status"] == "done", j
    assert j["result"]["name"] == "small (upscaled 2x).jpg"
    wait_idle(s, ev["id"])
    copy = next(p for p in s.get(f"/api/events/{ev['id']}/photos").json() if p["id"] == j["result"]["id"])
    assert copy["edited"] and copy["source"]["id"] == pid
    assert Image.open(io.BytesIO(s.get(f"/api/events/{ev['id']}/photos/{copy['id']}/orig").content)).size == (400, 300)
    other = make_user(email="c@studio.test", company="Studio C")
    assert other.post(f"/api/events/{ev['id']}/photos/{pid}/upscale", json={"scale": 2}).status_code == 404
    assert other.get(f"/api/events/{ev['id']}/ai-jobs").status_code == 404
