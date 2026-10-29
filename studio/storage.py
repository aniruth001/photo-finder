"""Where photo files live. Local disk now; the same four methods map onto S3/R2 later."""
import os
import secrets
import shutil
from pathlib import Path

from . import config


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def event_dir(self, account_id: int, event_id: int) -> Path:
        return self.root / "accounts" / str(account_id) / "events" / str(event_id)

    def ensure(self, account_id: int, event_id: int):
        for kind in ("orig", "thumb", "web", "full", "small"):
            (self.event_dir(account_id, event_id) / kind).mkdir(parents=True, exist_ok=True)

    def path(self, account_id: int, event_id: int, kind: str, public_id: str, ext: str = ".jpg") -> Path:
        return self.event_dir(account_id, event_id) / kind / f"{public_id}{ext}"

    def photo_path(self, row, kind: str) -> Path:
        """kind: orig (as uploaded), thumb, web, full (a full-size JPEG of a camera RAW file), or small (the smaller
        download, see small_path)."""
        ext = row["ext"] if kind == "orig" else ".jpg"
        return self.path(row["account_id"], row["event_id"], kind, row["public_id"], ext)

    def delivery_path(self, row) -> Path:
        """The file a client or guest downloads: the original, or for a camera RAW file its full-size JPEG
        (phones and most computers can't open RAW). The studio's own downloads stay the RAW."""
        return self.photo_path(row, "full" if is_raw_ext(row["ext"]) else "orig")

    def small_path(self, row) -> Path:
        """The "smaller size" download: a JPEG at most SMALL_SIDE pixels long, quick to download and share. Made the
        first time someone asks for it, then kept. A JPEG that is no bigger already is delivered as it is."""
        src = self.delivery_path(row)
        w, h = (row["width"] or 0), (row["height"] or 0)
        if w and h and max(w, h) <= SMALL_SIDE and src.suffix.lower() in (".jpg", ".jpeg"):
            return src
        dst = self.photo_path(row, "small")
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(f"{dst.name}.{secrets.token_hex(4)}.tmp")   # two downloads at once never see half a file
            try:
                make_small(src, tmp)
                os.replace(tmp, dst)
            finally:
                tmp.unlink(missing_ok=True)
        return dst

    def remove_photo(self, row):
        for kind in ("orig", "thumb", "web", "full", "small"):
            self.photo_path(row, kind).unlink(missing_ok=True)

    def remove_event(self, account_id: int, event_id: int):
        shutil.rmtree(self.event_dir(account_id, event_id), ignore_errors=True)


SMALL_SIDE = 2048   # the "smaller size" download: plenty for phones, WhatsApp and social media


def make_small(src: Path, dst: Path):
    """src (JPEG, PNG, WebP or HEIC), the right way up, at most SMALL_SIDE pixels long, as a JPEG. The colour profile is
    kept; the rest of the camera data (location included) is left out."""
    from PIL import Image, ImageOps

    try:
        im = Image.open(src)
        if im.format == "JPEG":
            im.draft("RGB", (SMALL_SIDE, SMALL_SIDE))   # decodes at a reduced scale: fast even for 100 MP
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im)
    except Exception:   # HEIC and the like
        from faces import open_image

        im, icc = open_image(src), None
    im.thumbnail((SMALL_SIDE, SMALL_SIDE), Image.LANCZOS)
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        flat = Image.new("RGB", im.size, (255, 255, 255))   # a JPEG has no transparency: onto white
        flat.paste(im, mask=im.getchannel("A"))
        im = flat
    im.convert("RGB").save(dst, "JPEG", quality=85, optimize=True, progressive=True, icc_profile=icc or None)


RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq"}


def is_raw_ext(ext: str) -> bool:
    return (ext or "").lower() in RAW_EXTS


def delivery_name(name: str, ext: str, size: str = "original") -> str:
    """A RAW photo is delivered as JPEG: IMG_0001.CR3 -> IMG_0001.jpg. The smaller size: IMG_0001 (small).jpg"""
    if size == "small":
        return name.rsplit(".", 1)[0] + " (small).jpg"
    return (name.rsplit(".", 1)[0] + ".jpg") if is_raw_ext(ext) else name


def download_file(row, size: str):
    """(path, name) a client or guest downloads: size "original" or "small"."""
    store = get_storage()
    if size == "small":
        return store.small_path(row), delivery_name(row["orig_name"], row["ext"], "small")
    return store.delivery_path(row), delivery_name(row["orig_name"], row["ext"])


_storage = None


def get_storage() -> LocalStorage:
    global _storage
    root = config.settings.data_dir / "files"
    if _storage is None or _storage.root != root:
        _storage = LocalStorage(root)
    return _storage
