"""Where photo files live. Local disk now; the same four methods map onto S3/R2 later."""
import shutil
from pathlib import Path

from . import config


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def event_dir(self, account_id: int, event_id: int) -> Path:
        return self.root / "accounts" / str(account_id) / "events" / str(event_id)

    def ensure(self, account_id: int, event_id: int):
        for kind in ("orig", "thumb", "web", "full"):
            (self.event_dir(account_id, event_id) / kind).mkdir(parents=True, exist_ok=True)

    def path(self, account_id: int, event_id: int, kind: str, public_id: str, ext: str = ".jpg") -> Path:
        return self.event_dir(account_id, event_id) / kind / f"{public_id}{ext}"

    def photo_path(self, row, kind: str) -> Path:
        """kind: orig (as uploaded), thumb, web, or full (a full-size JPEG of a camera RAW file)."""
        ext = row["ext"] if kind == "orig" else ".jpg"
        return self.path(row["account_id"], row["event_id"], kind, row["public_id"], ext)

    def delivery_path(self, row) -> Path:
        """The file a client or guest downloads: the original, or for a camera RAW file its full-size JPEG
        (phones and most computers can't open RAW). The studio's own downloads stay the RAW."""
        return self.photo_path(row, "full" if is_raw_ext(row["ext"]) else "orig")

    def remove_photo(self, row):
        for kind in ("orig", "thumb", "web", "full"):
            self.photo_path(row, kind).unlink(missing_ok=True)

    def remove_event(self, account_id: int, event_id: int):
        shutil.rmtree(self.event_dir(account_id, event_id), ignore_errors=True)


RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq"}


def is_raw_ext(ext: str) -> bool:
    return (ext or "").lower() in RAW_EXTS


def delivery_name(name: str, ext: str) -> str:
    """A RAW photo is delivered as JPEG: IMG_0001.CR3 -> IMG_0001.jpg"""
    return (name.rsplit(".", 1)[0] + ".jpg") if is_raw_ext(ext) else name


_storage = None


def get_storage() -> LocalStorage:
    global _storage
    root = config.settings.data_dir / "files"
    if _storage is None or _storage.root != root:
        _storage = LocalStorage(root)
    return _storage
