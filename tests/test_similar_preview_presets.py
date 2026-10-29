"""Similar shots in the client gallery; the editor opening a big photo from a preview; Load settings (Camera Raw and
Lightroom presets)."""
import io
import json
import zipfile

import numpy as np
from PIL import Image

from conftest import jpeg_bytes, upload, wait_idle
from studio import acr_presets
from studio.editing import RECIPE_OPS


def gradient(flip=False, dot=0, size=(320, 240)):
    w, h = size
    x = np.linspace(0, 255, w)[None, :].repeat(h, 0)
    y = np.linspace(0, 120, h)[:, None].repeat(w, 1)
    a = (x[:, ::-1] if flip else x) * 0.7 + y * 0.3
    im = Image.fromarray(np.stack([a, a * 0.8, 255 - a], 2).astype(np.uint8))
    if dot:
        im.putpixel((dot, dot), (255, 255, 255))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_clients_see_similar_shots_grouped(studio, anon):
    s, _, ev = studio
    upload(s, ev["id"], [("IMG_1.jpg", gradient()), ("IMG_2.jpg", gradient(dot=5)), ("IMG_3.jpg", gradient(flip=True))])
    wait_idle(s, ev["id"])
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    cpath = e["client"]["url"].split("testserver", 1)[1]

    assert anon.get(cpath + "/info").json()["similar"] == 1
    groups = anon.get(cpath + "/similar").json()["groups"]
    assert len(groups) == 1 and sorted(p["name"] for p in groups[0]["photos"]) == ["IMG_1.jpg", "IMG_2.jpg"]
    assert sum(p["best"] for p in groups[0]["photos"]) == 1                       # one marked best
    assert all(len(p["id"]) > 8 and not str(p["id"]).isdigit() for p in groups[0]["photos"])   # public ids only
    page = anon.get(cpath).text
    assert 'id="simbtn"' in page and "Choose the best of each" in page


def test_similar_needs_the_pin(studio, anon):
    s, _, ev = studio
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    s.post(f"/api/events/{ev['id']}/client-pin", json={"pin": "4821"})
    cpath = e["client"]["url"].split("testserver", 1)[1]
    assert anon.get(cpath + "/similar").status_code == 401
    assert anon.get(cpath + "/info").json()["similar"] is None


def test_the_editor_opens_a_big_photo_from_a_preview(studio):
    s, _, ev = studio
    big = io.BytesIO(); Image.new("RGB", (6000, 4000), (40, 120, 90)).save(big, "JPEG", quality=95)
    png = io.BytesIO(); Image.new("RGBA", (3000, 1000), (200, 50, 50, 100)).save(png, "PNG")
    upload(s, ev["id"], [("big.jpg", big.getvalue())])
    s.post(f"/api/events/{ev['id']}/photos", files=[("files", ("clear.png", png.getvalue(), "image/png"))])
    wait_idle(s, ev["id"])
    ids = {p["name"]: p["id"] for p in s.get(f"/api/events/{ev['id']}/photos").json()}
    base = f"/api/events/{ev['id']}/photos/{ids['big.jpg']}/editor"

    pv = s.get(base + "/preview?v=0")
    assert pv.status_code == 200 and pv.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(pv.content)).size == (2560, 1707) and len(pv.content) < len(big.getvalue()) / 4
    assert pv.headers["cache-control"].startswith("private")
    assert s.get(base + "/preview?v=0").content == pv.content                     # the second time from memory
    assert Image.open(io.BytesIO(s.get(base + "/preview?side=99999").content)).size[0] == 4096   # capped
    assert s.get(base + "/preview").headers["cache-control"] == "no-store"        # no version: not kept

    src = s.get(base + "/source?v=0")
    assert src.content == big.getvalue() and src.headers["cache-control"].startswith("private")
    assert s.get(base + "/source").headers["cache-control"] == "no-store"
    assert s.get(base + "/source?v=123").headers["cache-control"] == "no-store"  # not the current version

    p2 = s.get(f"/api/events/{ev['id']}/photos/{ids['clear.png']}/editor/preview?v=0")
    im = Image.open(io.BytesIO(p2.content))
    assert p2.headers["content-type"] == "image/png" and im.mode == "RGBA" and im.size == (2560, 853)   # transparency kept
    page = s.get("/editor").text
    assert "openFast" in page and "window.PFOriginal" in page and "<!--ORIGCACHE-->" not in page
    dash = s.get("/").text
    assert "window.PFOriginal" in dash and "<!--ORIGCACHE-->" not in dash and dash.count('<script nonce="') >= 2

    # the original comes in pieces at once (HTTP ranges), which together are the file itself
    n = len(big.getvalue()); cut = n // 3
    a = s.get(base + "/source?v=0", headers={"Range": f"bytes=0-{cut - 1}"})
    b = s.get(base + "/source?v=0", headers={"Range": f"bytes={cut}-{n - 1}"})
    assert a.status_code == b.status_code == 206 and a.headers["content-range"] == f"bytes 0-{cut - 1}/{n}"
    assert a.content + b.content == big.getvalue()


XMP = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    crs:PresetType="Normal" crs:Version="15.0" crs:ProcessVersion="11.0"
    crs:Exposure2012="+0.60" crs:Contrast2012="+20" crs:Highlights2012="-45" crs:Shadows2012="+35"
    crs:Whites2012="+10" crs:Blacks2012="-15" crs:Clarity2012="+12" crs:Vibrance="+18" crs:Saturation="-5"
    crs:IncrementalTemperature="+12" crs:IncrementalTint="-4" crs:HueAdjustmentOrange="-8" crs:SaturationAdjustmentBlue="-30"
    crs:LuminanceAdjustmentOrange="+15" crs:SplitToningShadowHue="210" crs:SplitToningShadowSaturation="20"
    crs:SplitToningHighlightHue="40" crs:SplitToningHighlightSaturation="15" crs:Sharpness="40" crs:SharpenRadius="+1.0"
    crs:PostCropVignetteAmount="-20" crs:GrainAmount="12" crs:LuminanceSmoothing="20" crs:CameraProfile="Adobe Standard">
   <crs:Name><rdf:Alt><rdf:li xml:lang="x-default">Warm Film</rdf:li></rdf:Alt></crs:Name>
   <crs:Look><rdf:Description crs:Amount="1"><crs:Name>Adobe Color</crs:Name></rdf:Description></crs:Look>
   <crs:ToneCurvePV2012><rdf:Seq><rdf:li>0, 20</rdf:li><rdf:li>128, 135</rdf:li><rdf:li>255, 245</rdf:li></rdf:Seq></crs:ToneCurvePV2012>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>"""

LRT = b"""s = {
	id = "6A1B",
	internalName = "Matte Black and White",
	title = "Matte Black and White",
	type = "Develop",
	value = {
		settings = {
			ConvertToGrayscale = true,
			Contrast2012 = 25,
			Exposure2012 = -0.2,
			ToneCurvePV2012 = {
				0,
				30,
				255,
				240,
			},
		},
		uuid = "6A1B",
	},
	version = 0,
}"""


def test_camera_raw_settings_become_editor_steps():
    name, st = acr_presets.parse_xmp(XMP)
    assert name == "Warm Film" and st["Exposure2012"] == "+0.60" and st["ToneCurvePV2012"][1] == "128, 135"
    steps, left = acr_presets.convert(st)
    ops = [x["op"] for x in steps]
    assert ops == ["wb", "adjust", "tone", "curves", "hsl", "lut", "sharpen", "vignette", "noise"]
    assert all(o in RECIPE_OPS for o in ops)
    by = {x["op"]: x["v"] for x in steps}
    assert by["wb"] == {"temperature": 12, "tint": -4}
    assert set(by["adjust"]) == {"exposure", "brightness", "contrast", "saturation", "hue", "temperature"}   # every key the filter reads
    assert by["tone"]["highlights"] == -45 and by["tone"]["shadows"] == 35
    assert by["curves"]["rgb"] == [[0, 20], [128, 135], [255, 245]] and by["curves"]["r"] == [[0, 0], [255, 255]]
    assert by["hsl"]["orange"] == {"h": -2, "s": 0, "l": 15} and by["hsl"]["blue"]["s"] == -30
    assert by["lut"]["size"] == 17 and len(by["lut"]["data"]) == 17 ** 3 * 3 and all(0 <= c <= 255 for c in by["lut"]["data"])
    assert "noise reduction" in left and "camera profile" in left                  # said, not silently dropped

    name, st = acr_presets.parse_lrtemplate(LRT)
    steps, _ = acr_presets.convert(st)
    assert name == "Matte Black and White" and [x["op"] for x in steps] == ["adjust", "curves", "grayscale"]
    assert steps[1]["v"]["rgb"] == [[0, 30], [255, 240]]


def test_load_settings(studio, app_client):
    s, _, ev = studio
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Pack/Warm Film.xmp", XMP)
        zf.writestr("Pack/BW.lrtemplate", LRT)
        zf.writestr("__MACOSX/Pack/._Warm Film.xmp", b"junk")
        zf.writestr("Pack/readme.txt", b"thanks")
    r = s.post("/api/edit-presets/import", files=[("files", ("pack.zip", z.getvalue(), "application/zip")),
                                                  ("files", ("bad.xmp", b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><x>&a;</x>", "text/xml")),
                                                  ("files", ("photo.jpg", jpeg_bytes(), "image/jpeg"))])
    assert r.status_code == 200, r.text
    j = r.json()
    assert sorted(p["name"] for p in j["imported"]) == ["Matte Black and White", "Warm Film"]
    assert sorted(x["file"] for x in j["skipped"]) == ["bad.xmp", "photo.jpg"]
    listed = {p["name"]: p for p in s.get("/api/edit-presets").json()}
    assert [st["op"] for st in listed["Matte Black and White"]["recipe"]["steps"]] == ["adjust", "curves", "grayscale"]

    again = s.post("/api/edit-presets/import", files=[("files", ("Warm Film.xmp", XMP, "text/xml"))]).json()
    assert again["imported"][0]["name"] == "Warm Film (2)"                          # nothing is overwritten
    assert s.post("/api/edit-presets/import", files=[("files", ("x.txt", b"hi", "text/plain"))]).json()["skipped"]

    from test_inventory import member
    vw, _ = member(s, app_client, "vw@studio.test", "viewer")
    assert vw.post("/api/edit-presets/import", files=[("files", ("Warm Film.xmp", XMP, "text/xml"))]).status_code == 403
    assert "loadSettings" in s.get("/editor").text
