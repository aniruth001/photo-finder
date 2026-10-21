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
        for kind in ("orig", "thumb", "web"):
            (self.event_dir(account_id, event_id) / kind).mkdir(parents=True, exist_ok=True)

    def path(self, account_id: int, event_id: int, kind: str, public_id: str, ext: str = ".jpg") -> Path:
        return self.event_dir(account_id, event_id) / kind / f"{public_id}{ext}"

    def photo_path(self, row, kind: str) -> Path:
        ext = row["ext"] if kind == "orig" else ".jpg"
        return self.path(row["account_id"], row["event_id"], kind, row["public_id"], ext)

    def remove_photo(self, row):
        for kind in ("orig", "thumb", "web"):
            self.photo_path(row, kind).unlink(missing_ok=True)

    def remove_event(self, account_id: int, event_id: int):
        shutil.rmtree(self.event_dir(account_id, event_id), ignore_errors=True)


_storage = None


def get_storage() -> LocalStorage:
    global _storage
    root = config.settings.data_dir / "files"
    if _storage is None or _storage.root != root:
        _storage = LocalStorage(root)
    return _storage
