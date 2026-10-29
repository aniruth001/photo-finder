"""Clients and guests choose: download the original, or a smaller size (at most 2048 pixels)."""
import io
import zipfile
from urllib.parse import unquote

from PIL import Image

from conftest import jpeg_bytes, upload, wait_idle
from studio.storage import SMALL_SIDE, get_storage


def big_jpeg(size=(4000, 3000)):
    buf = io.BytesIO()
    im = Image.effect_noise(size, 40).convert("RGB")
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def setup(s, ev):
    png = io.BytesIO(); Image.new("RGBA", (3000, 1500), (200, 40, 40, 0)).save(png, "PNG")
    upload(s, ev["id"], [("BIG.jpg", big_jpeg()), ("tiny.jpg", jpeg_bytes(seed=3))])
    s.post(f"/api/events/{ev['id']}/photos", files=[("files", ("clear.png", png.getvalue(), "image/png"))])
    wait_idle(s, ev["id"])
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    ids = {p["name"]: p["id"] for p in s.get(f"/api/events/{ev['id']}/photos").json()}
    return e["client"]["url"].split("testserver", 1)[1], f"/e/{e['slug']}", ids


def test_one_photo_original_or_smaller(studio, anon):
    s, _, ev = studio
    cpath, gpath, ids = setup(s, ev)
    for base in (cpath, gpath):
        orig = anon.get(f"{base}/download/{ids['BIG.jpg']}")
        small = anon.get(f"{base}/download/{ids['BIG.jpg']}?size=small")
        assert orig.status_code == small.status_code == 200
        im = Image.open(io.BytesIO(small.content))
        assert im.format == "JPEG" and max(im.size) == SMALL_SIDE and im.size == (2048, 1536)
        assert len(small.content) < len(orig.content) / 3
        assert unquote(small.headers["content-disposition"]).endswith("BIG (small).jpg")
        assert 'filename="BIG.jpg"' in orig.headers["content-disposition"]
        # a photo that is small already comes as it is
        assert anon.get(f"{base}/download/{ids['tiny.jpg']}?size=small").content == anon.get(f"{base}/download/{ids['tiny.jpg']}").content
        # a transparent PNG becomes a JPEG on white
        p = Image.open(io.BytesIO(anon.get(f"{base}/download/{ids['clear.png']}?size=small").content))
        assert p.format == "JPEG" and p.size == (2048, 1024) and p.getpixel((10, 10)) == (255, 255, 255)
        assert anon.get(f"{base}/download/{ids['BIG.jpg']}?size=huge").status_code == 422
    # made once, then kept
    from studio import db
    r = db.q1("SELECT * FROM photos WHERE public_id=?", (ids["BIG.jpg"],))
    assert get_storage().photo_path(r, "small").exists()
    s.post(f"/api/events/{ev['id']}/photos/delete", json={"ids": [ids["BIG.jpg"]]})
    assert not get_storage().photo_path(r, "small").exists()                       # deleted with the photo


def test_zip_original_or_smaller(studio, anon):
    s, _, ev = studio
    cpath, gpath, ids = setup(s, ev)
    z = zipfile.ZipFile(io.BytesIO(anon.post(cpath + "/zip", data={"ids": "", "size": "small"}).content))
    assert sorted(z.namelist()) == ["BIG (small).jpg", "clear (small).jpg", "tiny (small).jpg"]
    assert max(Image.open(z.open("BIG (small).jpg")).size) == SMALL_SIDE
    r = anon.post(gpath + "/zip", data={"ids": ids["BIG.jpg"], "size": "small"})
    assert r.headers["content-disposition"].endswith('-my-photos-small.zip"')
    assert zipfile.ZipFile(io.BytesIO(r.content)).namelist() == ["BIG (small).jpg"]
    z = zipfile.ZipFile(io.BytesIO(anon.post(cpath + "/zip", data={"ids": ""}).content))   # no choice: the originals, as before
    assert sorted(z.namelist()) == ["BIG.jpg", "clear.png", "tiny.jpg"]
    assert anon.post(cpath + "/zip", data={"ids": "", "size": "x"}).status_code == 422


def test_downloads_off_means_both_off(studio, anon):
    s, _, ev = studio
    cpath, gpath, ids = setup(s, ev)
    s.patch(f"/api/events/{ev['id']}", json={"client_download": False, "allow_download": False})
    assert anon.get(f"{cpath}/download/{ids['BIG.jpg']}?size=small").status_code == 403
    assert anon.get(f"{gpath}/download/{ids['BIG.jpg']}?size=small").status_code == 403
    page = anon.get(cpath).text
    assert 'id="dls"' in page and "pfChooseSize" in page
    assert 'id="dls"' in anon.get(gpath).text


def test_the_studio_chooses_which_sizes_are_offered(studio, anon):
    s, _, ev = studio
    cpath, gpath, ids = setup(s, ev)
    e = s.get(f"/api/events/{ev['id']}").json()
    assert e["download_sizes"] == "both" and e["client"]["download_sizes"] == "both"            # both, unless changed
    e = s.patch(f"/api/events/{ev['id']}", json={"guest_dl_sizes": "small", "client_dl_sizes": "original"}).json()
    assert e["download_sizes"] == "small" and e["client"]["download_sizes"] == "original"
    assert anon.get(gpath + "/info").json()["download_sizes"] == "small"
    assert anon.get(cpath + "/info").json()["download_sizes"] == "original"
    big = ids["BIG.jpg"]
    # guests: smaller only
    assert anon.get(f"{gpath}/download/{big}").status_code == 200                                 # no size asked: the one offered
    assert max(Image.open(io.BytesIO(anon.get(f"{gpath}/download/{big}").content)).size) == SMALL_SIDE
    assert anon.get(f"{gpath}/download/{big}?size=original").status_code == 403
    assert anon.post(gpath + "/zip", data={"ids": big, "size": "original"}).status_code == 403
    assert zipfile.ZipFile(io.BytesIO(anon.post(gpath + "/zip", data={"ids": big}).content)).namelist() == ["BIG (small).jpg"]
    # the client: original only
    assert anon.get(f"{cpath}/download/{big}?size=small").status_code == 403
    assert max(Image.open(io.BytesIO(anon.get(f"{cpath}/download/{big}").content)).size) == 4000
    assert anon.post(cpath + "/zip", data={"ids": "", "size": "small"}).status_code == 403
    assert s.patch(f"/api/events/{ev['id']}", json={"guest_dl_sizes": "tiny"}).status_code == 422
