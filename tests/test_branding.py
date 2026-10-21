"""Company logo: upload and clean-up, where it is shown instead of the name, and who may change it."""
import io
import re

import pytest
from PIL import Image

from conftest import Sess, jpeg_bytes, upload, wait_idle
from studio import config, db, mailer


def png_logo(size=(1600, 900), margin=200, color=(200, 30, 60, 255)):
    """A logo on a transparent canvas with empty margins all round."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (size[0] - 2 * margin, size[1] - 2 * margin), color), (margin, margin))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def put_logo(s, data, name="logo.png", ctype="image/png"):
    return s.post("/api/account/logo", files={"file": (name, data, ctype)})


def test_upload_is_cleaned_trimmed_resized_and_served(make_user, anon, tmp_path):
    s = make_user()
    r = put_logo(s, png_logo())
    assert r.status_code == 200
    url = r.json()["account"]["logo_url"]
    assert re.fullmatch(r"/logo/[\w-]{16}\.png", url)
    img = anon.get(url)                                                # public: guest pages need it
    assert img.status_code == 200 and img.headers["content-type"] == "image/png"
    assert "immutable" in img.headers["cache-control"]
    im = Image.open(io.BytesIO(img.content))
    assert im.size == (1200, 500)                                      # 1600x900 minus the 200 px margins
    assert im.getbbox() == (0, 0, im.width, im.height)                 # the empty margins are gone
    big = png_logo(size=(3000, 3000), margin=0)
    url3 = put_logo(s, big).json()["account"]["logo_url"]
    assert Image.open(io.BytesIO(anon.get(url3).content)).size == (600, 600)   # shrunk to fit 1200 x 600
    url = put_logo(s, png_logo()).json()["account"]["logo_url"]

    # replacing it: new address, the old file is deleted
    url2 = put_logo(s, png_logo(color=(0, 90, 200, 255))).json()["account"]["logo_url"]
    assert url2 != url and anon.get(url).status_code == 404 and anon.get(url2).status_code == 200
    assert s.delete("/api/account/logo").json()["account"]["logo_url"] == ""
    assert anon.get(url2).status_code == 404
    assert not list((tmp_path / "data" / "files").rglob("logo/*.png"))


def test_metadata_is_stripped(make_user, anon):
    s = make_user()
    im = Image.new("RGB", (400, 200), (10, 120, 60))
    exif = Image.Exif()
    exif[0x010F] = "SecretCam"   # camera maker
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    assert b"SecretCam" in buf.getvalue()
    url = put_logo(s, buf.getvalue(), "logo.jpg", "image/jpeg").json()["account"]["logo_url"]
    out = anon.get(url).content
    assert b"SecretCam" not in out and Image.open(io.BytesIO(out)).format == "PNG"


@pytest.mark.parametrize("data, name, code", [
    (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "logo.svg", 422),
    (b"definitely not an image", "logo.png", 422),
    (None, "empty.png", 422),           # completely transparent
])
def test_bad_logos_are_refused(make_user, data, name, code):
    s = make_user()
    if data is None:
        buf = io.BytesIO()
        Image.new("RGBA", (300, 100), (0, 0, 0, 0)).save(buf, "PNG")
        data = buf.getvalue()
    r = put_logo(s, data, name)
    assert r.status_code == code, r.text
    assert s.get("/api/auth/me").json()["account"]["logo_url"] == ""


def test_too_large_upload(make_user, monkeypatch):
    from studio import branding
    monkeypatch.setattr(branding, "MAX_UPLOAD", 1000)
    assert put_logo(make_user(), png_logo()).status_code == 413


def test_guest_and_client_pages_show_the_logo_instead_of_the_name(studio, anon):
    s, _, ev = studio
    s.patch("/api/account", json={"company": 'Lens "&" <Co>'})
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    client = e["client"]["url"].split("testserver", 1)[1]
    for path in (f"/e/{e['slug']}", client):
        page = anon.get(path).text
        assert '<img class="brandlogo' not in page and "Lens &quot;&amp;&quot; &lt;Co&gt;" in page  # no logo: the name
    url = put_logo(s, png_logo()).json()["account"]["logo_url"]
    s.patch("/api/account", json={"logo_size": "large"})
    for path in (f"/e/{e['slug']}", client):
        page = anon.get(path).text
        by = re.search(r'<p class="by">(.*?)</p>', page).group(1)
        assert f'src="{url}"' in by and 'alt="Lens &quot;&amp;&quot; &lt;Co&gt;"' in by and "height:72px" in by
        foot = re.search(r"<footer>(.*?)</footer>", page).group(1)
        assert "brandlogo" in foot and "height:22px" in foot
        assert "<Co>" not in page                                       # still escaped everywhere


def test_only_company_settings_permission_can_change_it(make_user, app_client):
    s = make_user()
    assert s.patch("/api/account", json={"logo_size": "huge"}).status_code == 422
    db.run("UPDATE users SET role='manager'")
    assert put_logo(s, png_logo()).status_code == 403
    assert s.delete("/api/account/logo").status_code == 403
    assert s.patch("/api/account", json={"logo_size": "small"}).status_code == 403


def test_logo_goes_with_the_company_and_shows_in_emails_and_admin(make_user, anon, monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(mailer, "send", lambda to, subject, text, html="": sent.append(html) or True)
    monkeypatch.setattr(config.settings, "public_url", "https://photos.example.com")
    s = make_user("owner@x.test", "Lens Co")
    url = put_logo(s, png_logo()).json()["account"]["logo_url"]
    s.post("/api/team/invites", json={"email": "new@x.test", "role": "viewer"})
    anon.post("/api/auth/forgot", json={"email": "owner@x.test"})
    assert len(sent) == 2 and all(f'src="https://photos.example.com{url}"' in h and 'alt="Lens Co"' in h for h in sent)

    pa = make_user("pa@x.test", "HQ")
    db.run("UPDATE users SET platform_admin=1 WHERE email='pa@x.test'")
    lens = next(a for a in pa.get("/api/admin/accounts").json()["items"] if a["company"] == "Lens Co")
    assert lens["logo_url"] == url

    assert s.post("/api/account/delete", json={"password": "correct horse"}).status_code == 200
    assert anon.get(url).status_code == 404
    assert not list((tmp_path / "data" / "files").rglob("logo/*.png"))


@pytest.mark.parametrize("color, transparent, badge", [
    ((40, 40, 40, 255), True, True),       # dark logo on transparency: unreadable on the dark pages -> badge
    ((240, 200, 60, 255), True, False),    # light logo: fine as it is
    ((40, 40, 40, 255), False, True),      # solid dark square: just as invisible on a dark page -> badge
    ((255, 255, 255, 255), False, False),  # a logo on a white box (typical JPG): bright enough
    ("mixed", True, True),                 # bright symbol + dark lettering: the lettering would vanish -> badge
])
def test_dark_transparent_logos_get_a_light_badge_on_dark_pages(studio, anon, color, transparent, badge):
    s, _, ev = studio
    if color == "mixed":
        im = Image.new("RGBA", (800, 400), (0, 0, 0, 0))
        im.paste(Image.new("RGBA", (300, 300), (194, 24, 91, 255)), (20, 50))   # big bright pink symbol
        im.paste(Image.new("RGBA", (380, 20), (43, 43, 43, 255)), (380, 170))   # thin dark name: ~8% of the logo
        buf = io.BytesIO(); im.save(buf, "PNG"); data = buf.getvalue()
    elif transparent:
        data = png_logo(size=(800, 400), margin=0, color=(0, 0, 0, 0))
        im = Image.open(io.BytesIO(data))
        im.paste(Image.new("RGBA", (500, 120), color), (150, 140))
        buf = io.BytesIO(); im.save(buf, "PNG"); data = buf.getvalue()
    else:
        data = png_logo(size=(800, 400), margin=0, color=color)
    me = put_logo(s, data).json()
    assert me["account"]["logo_badge"] is badge
    s.patch(f"/api/events/{ev['id']}", json={"status": "published"})
    by = re.search(r'<p class="by">(.*?)</p>', anon.get(f"/e/{ev['slug']}").text).group(1)
    assert ('class="brandlogo bd' in by) is badge                     # the dark pages' badge class
