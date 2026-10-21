"""Light and dark mode on the guest and client pages: the studio picks the default, visitors may switch."""
import io
import random
import re

import pytest
from PIL import Image

from studio import db
from studio.branding import DARK_BG, LIGHT_BG, accent_text, contrast


@pytest.fixture()
def pages(studio):
    """Guest page and client gallery of one event; returns (studio, [paths])."""
    s, _, ev = studio
    e = s.patch(f"/api/events/{ev['id']}", json={"status": "published", "client_enabled": True}).json()
    return s, [f"/e/{e['slug']}", e["client"]["url"].split("testserver", 1)[1]]


def attrs(page):
    m = re.search(r'<html lang="en" data-studio-theme="(\w+)" data-theme="(\w+)">', page)
    return m.groups()


def test_studio_chooses_the_default_mode(pages, anon):
    s, paths = pages
    for path in paths:
        page = anon.get(path).text
        assert attrs(page) == ("dark", "dark")                          # today's look stays the default
        assert page.index('localStorage.getItem("pf_theme")') < page.index("<body>")   # decided before drawing
        assert 'id="themebtn"' in page and not re.search(r"__[A-Z_]+__", page)   # every placeholder filled
    assert s.patch("/api/account", json={"theme": "light"}).json()["account"]["theme"] == "light"
    assert all(attrs(anon.get(p).text) == ("light", "light") for p in paths)
    s.patch("/api/account", json={"theme": "auto"})
    assert all(attrs(anon.get(p).text) == ("auto", "dark") for p in paths)   # resolved on the visitor's device
    assert 'matchMedia("(prefers-color-scheme: light)")' in anon.get(paths[0]).text


def test_theme_setting_rules(make_user):
    s = make_user()
    assert s.patch("/api/account", json={"theme": "purple"}).status_code == 422
    db.run("UPDATE users SET role='manager'")
    assert s.patch("/api/account", json={"theme": "light"}).status_code == 403


def test_brand_colour_text_is_readable_in_both_modes(pages, anon):
    s, paths = pages
    s.patch("/api/account", json={"brand_color": "#f0c52f"})              # yellow: fine on dark, unreadable on white
    page = anon.get(paths[0]).text
    assert "--accent: #f0c52f" in page and "--accent-text: #f0c52f;" in page
    assert "--accent-text: #876b0a;" in page                               # darkened for the light pages


@pytest.mark.parametrize("seed", range(5))
def test_accent_text_always_reaches_4_5_to_1(seed):
    rnd = random.Random(seed)
    for _ in range(60):
        c = "#%02x%02x%02x" % tuple(rnd.randrange(256) for _ in range(3))
        for bg in (DARK_BG, LIGHT_BG):
            out = accent_text(c, bg)
            assert contrast(tuple(int(out[i:i + 2], 16) for i in (1, 3, 5)), bg) >= 4.5, (c, bg, out)


def _logo(rgb):
    im = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    im.paste(Image.new("RGBA", (500, 80), rgb + (255,)), (50, 60))
    buf = io.BytesIO(); im.save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.parametrize("rgb, dark_badge, light_badge", [
    ((255, 255, 255), False, True),   # white logo: vanishes on the light pages -> dark badge there
    ((30, 30, 30), True, False),      # black logo: vanishes on the dark pages -> light badge there
    ((200, 30, 60), False, False),    # a strong colour reads on both
])
def test_logos_get_the_badge_their_background_needs(pages, anon, rgb, dark_badge, light_badge):
    s, paths = pages
    me = s.post("/api/account/logo", files={"file": ("l.png", _logo(rgb), "image/png")}).json()
    assert (me["account"]["logo_badge"], me["account"]["logo_badge_light"]) == (dark_badge, light_badge)
    cls = re.search(r'<p class="by"><img class="([^"]+)"', anon.get(paths[1]).text).group(1).split()
    assert ("bd" in cls, "bl" in cls) == (dark_badge, light_badge)
