"""Company logo: upload, clean-up, storage and the snippet that shows it instead of the company name.

The dashboard's editor crops, rotates, trims and resizes in the browser. Whatever arrives, the server opens it
with Pillow and draws it again as a fresh PNG: no metadata, no hidden payload, never bigger than MAX_SIDE.
Logos live under the account's folder (so deleting the company deletes them) behind a random file name.
"""
import html
import io
import re
import secrets
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from . import db
from .storage import get_storage

SIZES = {"small": 32, "medium": 48, "large": 72}       # logo height on the guest and client pages, in px
MAX_W, MAX_H = 1200, 600
MAX_UPLOAD = 8 * 1024 * 1024
FORMATS = {"PNG", "JPEG", "WEBP", "GIF", "HEIF", "BMP"}  # no SVG: it can carry scripts


DARK_BG = (20, 16, 14)       # the guest and client pages' dark background (#14100e)
LIGHT_BG = (247, 246, 242)   # and the light one (#f7f6f2)
THEMES = ("dark", "light", "auto")
STATIC = Path(__file__).parent / "static"
_W = np.array([0.2126, 0.7152, 0.0722])


def _lum(rgb) -> np.ndarray:
    """WCAG relative luminance of sRGB values in 0..255 (any shape ending in 3)."""
    c = np.asarray(rgb, dtype=np.float64) / 255
    return np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4) @ _W


def contrast(a, b) -> float:
    la, lb = float(_lum(a)), float(_lum(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def needs_badge(im: Image.Image, bg=DARK_BG) -> bool:
    """Would part of this logo vanish on background `bg`? Then show it on a badge there.

    Each visible pixel's contrast (WCAG formula) with the page background: if at least 5% of the logo (weighted
    by opacity) is below 2:1, for example dark lettering on the dark pages or white lettering on the light ones,
    it gets the badge. Lettering is often a small share of a logo, and a badge behind a logo that didn't strictly
    need one still looks right.
    """
    small = im.copy()
    small.thumbnail((200, 200))
    px = np.asarray(small.convert("RGBA"), dtype=np.float64).reshape(-1, 4)
    px = px[px[:, 3] > 16]
    if not len(px):
        return False
    lum, lbg = _lum(px[:, :3]), float(_lum(bg))
    faint = (np.maximum(lum, lbg) + 0.05) / (np.minimum(lum, lbg) + 0.05) < 2.0
    return float(px[faint, 3].sum()) >= 0.05 * float(px[:, 3].sum())


def accent_text(color: str, bg) -> str:
    """The brand colour, lightened or darkened just enough for text in it to reach 4.5:1 on `bg`."""
    import colorsys
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    if contrast(rgb, bg) >= 4.5:
        return color
    h, l, s_ = colorsys.rgb_to_hls(*(v / 255 for v in rgb))
    step = 0.02 if float(_lum(bg)) < 0.2 else -0.02     # lighten on dark pages, darken on light ones
    for _ in range(50):
        l = min(1.0, max(0.0, l + step))
        out = tuple(round(v * 255) for v in colorsys.hls_to_rgb(h, l, s_))
        if contrast(out, bg) >= 4.5 or l in (0.0, 1.0):
            return "#%02x%02x%02x" % out
    return color


def clean_logo(data: bytes):
    """Validate an uploaded image; return (tidy PNG bytes, needs a badge on the dark pages, on the light pages),
    or raise ValueError with a message for the user."""
    try:
        import faces  # noqa: F401  registers the HEIC opener

        im = Image.open(io.BytesIO(data))
        if im.format not in FORMATS:
            raise ValueError("Use a PNG, JPG or WebP image for the logo (SVG isn't supported).")
        if im.width * im.height > 40_000_000:
            raise ValueError("That image is too big. Use one under 40 megapixels.")
        im.seek(0)  # first frame of an animation
        im = ImageOps.exif_transpose(im).convert("RGBA")
    except ValueError:
        raise
    except Exception:
        raise ValueError("We couldn't read that image. Try a PNG or JPG.")
    box = im.getbbox()  # fully transparent edges never belong to a logo
    if box is None:
        raise ValueError("That image is empty (completely transparent).")
    im = im.crop(box)
    im.thumbnail((MAX_W, MAX_H), Image.LANCZOS)
    if min(im.size) < 8:
        raise ValueError("That logo is too small. Use one at least 100 pixels wide.")
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue(), needs_badge(im, DARK_BG), needs_badge(im, LIGHT_BG)


def logo_file(account_id: int, token: str) -> Path:
    return get_storage().root / "accounts" / str(account_id) / "logo" / f"{token}.png"


def logo_url(token: str, base: str = "") -> str:
    return f"{base}/logo/{token}.png" if token else ""


def mark(company: str, logo: str, size: str, small: bool = False, badge: bool = False, badge_light: bool = False) -> str:
    """HTML for "who made this" on the guest and client pages: the logo if there is one, else the company name.
    The page's theme decides which badge (if any) shows: class bd on the dark pages, bl on the light ones."""
    name = html.escape(company, quote=True)
    if not logo:
        return name
    h = 22 if small else SIZES.get(size, SIZES["medium"])
    cls = "brandlogo" + (" bd" if badge else "") + (" bl" if badge_light else "")
    return (f'<img class="{cls}" src="{logo_url(logo)}" alt="{name}" title="{name}" '
            f'style="--h:{h}px;height:{h}px;max-width:{h * 5}px;object-fit:contain;vertical-align:middle">')


SCRIPT_TAG = re.compile(r"<script(?=[\s>])")


def with_nonce(page: str, nonce: str) -> str:
    """Give the page's own <script> tags this response's nonce: under the Content-Security-Policy only scripts that
    carry it run, so a script that found its way into the page any other way does not."""
    return SCRIPT_TAG.sub(f'<script nonce="{nonce}"', page) if nonce else page


def render(page: str, e, ink: str, text: dict = None, nonce: str = "") -> str:
    """Fill a guest or client page's studio parts: theme, colours, logo or name. `e` has the account's columns.
    `text` maps more placeholders to plain text (escaped here). Everything is replaced in one pass, so an event
    name that happens to contain a placeholder's name is shown as typed. The nonce goes on the page's own scripts
    before any of that text is filled in, so nothing typed by a user can ever carry it."""
    theme = e["theme"] if e["theme"] in THEMES else "dark"
    marks = {"__MARK__": mark(e["company"], e["logo"], e["logo_size"], badge=bool(e["logo_badge"]),
                              badge_light=bool(e["logo_badge_light"])),
             "__FOOTER_MARK__": mark(e["company"], e["logo"], e["logo_size"], small=True, badge=bool(e["logo_badge"]),
                                     badge_light=bool(e["logo_badge_light"]))}
    page = page.replace("__THEME_HEAD__", (STATIC / "_theme.html").read_text(encoding="utf-8"))
    page = page.replace("__THEME_TOGGLE__", (STATIC / "_theme_toggle.html").read_text(encoding="utf-8"))
    page = page.replace("__VIEWER__", (STATIC / "_viewer.html").read_text(encoding="utf-8"))  # zoom, pan, rotate
    page = page.replace("__STORY__", (STATIC / "_story.html").read_text(encoding="utf-8"))  # the landing-page story
    page = with_nonce(page, nonce)
    values = {**marks, "__THEME__": theme, "__THEME_INITIAL__": "light" if theme == "light" else "dark",
              "__ACCENT_DARK__": accent_text(e["brand_color"], DARK_BG),
              "__ACCENT_LIGHT__": accent_text(e["brand_color"], LIGHT_BG),
              "__BRAND_INK__": ink, "__BRAND__": e["brand_color"],
              **{k: html.escape(v or "", quote=True) for k, v in (text or {}).items()}}
    pattern = re.compile("|".join(re.escape(k) for k in sorted(values, key=len, reverse=True)))
    return pattern.sub(lambda m: values[m.group(0)], page)


def save_logo(account_id: int, data: bytes) -> str:
    """Store a cleaned logo for the account, remove the previous one, return the new token."""
    png, badge, badge_light = clean_logo(data)
    token = secrets.token_urlsafe(12)
    f = logo_file(account_id, token)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(png)
    old = db.q1("SELECT logo FROM accounts WHERE id=?", (account_id,))["logo"]
    db.run("UPDATE accounts SET logo=?, logo_badge=?, logo_badge_light=? WHERE id=?",
           (token, int(badge), int(badge_light), account_id))
    if old:
        logo_file(account_id, old).unlink(missing_ok=True)
    return token


def remove_logo(account_id: int):
    old = db.q1("SELECT logo FROM accounts WHERE id=?", (account_id,))["logo"]
    db.run("UPDATE accounts SET logo='' WHERE id=?", (account_id,))
    if old:
        logo_file(account_id, old).unlink(missing_ok=True)
