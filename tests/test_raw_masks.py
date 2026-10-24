"""Camera RAW uploads (clients get a JPEG, the studio keeps the RAW), and editor masks kept with recipe steps."""
import io
import json
import os
import zipfile

import pytest
from PIL import Image, ImageDraw

from conftest import jpeg_bytes, upload, wait_idle
from studio import ai, config, db
from studio.client_gallery import unique_names
from studio.storage import delivery_name

needs_rawpy = pytest.mark.skipif(".cr3" not in config.ALLOWED_EXTS, reason="rawpy not installed")


def test_delivery_names():
    assert delivery_name("IMG_0001.CR3", ".cr3") == "IMG_0001.jpg"
    assert delivery_name("DSC_0059.NEF", ".nef") == "DSC_0059.jpg"
    assert delivery_name("photo.jpg", ".jpg") == "photo.jpg"
    rows = [{"orig_name": "A.CR3", "ext": ".cr3", "public_id": "aaaaaa1"}, {"orig_name": "A.jpg", "ext": ".jpg", "public_id": "bbbbbb2"}]
    assert [n for _, n in unique_names(rows)] == ["A.jpg", "A-bbbbbb.jpg"]   # the RAW's JPEG and a real JPEG don't collide


@needs_rawpy
def test_a_raw_file_that_cannot_be_read_fails_with_a_clear_message(studio):
    s, _, ev = studio
    r = upload(s, ev["id"], [("IMG_9999.CR3", b"\x00\x00\x00\x18ftypcrx " + b"x" * 5000)]).json()
    assert r["results"][0]["status"] == "queued"
    wait_idle(s, ev["id"])
    p = s.get(f"/api/events/{ev['id']}/photos").json()[0]
    assert p["status"] == "failed" and "RAW file" in p["error"]


@pytest.mark.skipif(not os.environ.get("PF_TEST_RAW"), reason="set PF_TEST_RAW to a camera RAW file to run")
def test_raw_round_trip(studio, anon):
    s, _, ev = studio
    path = os.environ["PF_TEST_RAW"]
    data = open(path, "rb").read()
    name = os.path.basename(path)
    upload(s, ev["id"], [(name, data)])
    wait_idle(s, ev["id"], timeout=180)
    p = s.get(f"/api/events/{ev['id']}/photos").json()[0]
    assert p["status"] == "ready", p
    assert s.get(f"/api/events/{ev['id']}/photos/{p['id']}/orig").content == data          # the studio keeps the RAW
    src = s.get(f"/api/events/{ev['id']}/photos/{p['id']}/editor/source")                  # the editor gets it developed
    assert src.status_code == 200 and src.content[:3] == b"\xff\xd8\xff"
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    cpath = e["client"]["url"].split("testserver", 1)[1]
    d = anon.get(f"{cpath}/download/{p['id']}")
    assert d.content[:3] == b"\xff\xd8\xff" and name.rsplit(".", 1)[0] + ".jpg" in d.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(anon.post(cpath + "/zip", data={"ids": ""}).content))
    assert z.namelist() == [name.rsplit(".", 1)[0] + ".jpg"]


def test_masked_steps_are_kept_with_the_copy(studio):
    s, _, ev = studio
    upload(s, ev["id"], [("IMG_1.jpg", jpeg_bytes(seed=31))])
    wait_idle(s, ev["id"])
    pid = s.get(f"/api/events/{ev['id']}/photos").json()[0]["id"]
    steps = [{"op": "adjust", "v": {"exposure": 0.5}, "mask": {"kind": "subject"}},
             {"op": "blur", "v": {"radius": 10}, "mask": {"kind": "background"}},
             {"op": "tone", "v": {"shadows": 30}, "mask": {"kind": "linear", "linear": {"x0": .5, "y0": 0, "x1": .5, "y1": .4}}}]
    c = s.post(f"/api/events/{ev['id']}/photos/{pid}/edits", files={"file": ("e.jpg", jpeg_bytes(seed=32), "image/jpeg")},
               data={"recipe": json.dumps({"steps": steps})}).json()
    assert [st.get("mask", {}).get("kind") for st in c["recipe"]["steps"]] == ["subject", "background", "linear"]
    r = s.post("/api/edit-presets", json={"name": "Brighter people", "recipe": json.dumps({"steps": steps[:1]})}).json()
    assert r["recipe"]["steps"][0]["mask"] == {"kind": "subject"}


@pytest.mark.skipif(not ai.installed("sky"), reason="AI models not downloaded (python -m studio get-ai-models)")
def test_sky(studio):
    s, _, ev = studio
    assert s.get("/api/ai").json()["sky"] is True
    im = Image.new("RGB", (480, 360), (150, 120, 90))   # an indoor scene: walls and a doorway, no sky
    d = ImageDraw.Draw(im)
    d.rectangle([180, 80, 300, 360], fill=(70, 50, 30))
    d.rectangle([20, 200, 140, 330], fill=(200, 190, 160))
    buf = io.BytesIO(); im.save(buf, "JPEG")
    r = s.post("/api/ai/sky", files={"file": ("p.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 404 and r.json()["error"] == "no_sky"
