"""Scan a folder of photos, detect + embed every face, keep thumbnails. Results are cached on disk,
so a restart only processes photos that are new or changed."""
import hashlib
import os
import pickle
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from faces import DEFAULT_MODEL, make_engine, open_image

EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
THUMB_SIDE = 480
WEB_SIDE = 1600
CACHE_VERSION = 2


def photo_id(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]


def scan(root: Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.suffix.lower() in EXTS and p.is_file() and not any(part.startswith(".") for part in rel.parts):
            yield p, rel.as_posix()


_engine = None


def _init_worker(detector, model):
    global _engine
    _engine = make_engine(model, detector)


def _save_jpeg(im, side, path):
    c = im.copy()
    c.thumbnail((side, side))
    c.save(path, "JPEG", quality=82, optimize=True)


def _process(job):
    path, pid, cache_dir, detect_side = job
    try:
        im = open_image(path)
        _save_jpeg(im, THUMB_SIDE, Path(cache_dir) / "thumb" / f"{pid}.jpg")
        _save_jpeg(im, WEB_SIDE, Path(cache_dir) / "web" / f"{pid}.jpg")
        vecs = [v for _, v in _engine.faces(im, detect_side=detect_side)]
        return pid, vecs, None
    except Exception as e:  # corrupt or unsupported file: skip it, keep going
        return pid, [], f"{type(e).__name__}: {e}"


class Index:
    def __init__(self, root: Path, cache_root: Path, detect_side: int = 2000, detector: str = "hog",
                 model: str = DEFAULT_MODEL):
        self.root = root.resolve()
        key = hashlib.sha1(str(self.root).encode()).hexdigest()[:10]
        self.cache_dir = cache_root / key
        self.detect_side, self.detector, self.model = detect_side, detector, model
        self.photos = {}  # id -> {path, name, mtime, size, faces: (k,128) array}
        self.vecs = np.zeros((0, 128), dtype=np.float32)
        self.owner = []  # owner[i] = photo id of row i in self.vecs

    @property
    def meta(self):
        return {"v": CACHE_VERSION, "detect_side": self.detect_side, "detector": self.detector, "model": self.model}

    def build(self, workers: int = 0, reindex: bool = False):
        (self.cache_dir / "thumb").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "web").mkdir(parents=True, exist_ok=True)
        pkl = self.cache_dir / "index.pkl"
        old = {}
        if pkl.exists() and not reindex:
            try:
                data = pickle.loads(pkl.read_bytes())
                if data.get("meta") == self.meta:
                    old = data["photos"]
            except Exception:
                old = {}

        found, todo = {}, []
        for p, rel in scan(self.root):
            pid, st = photo_id(rel), p.stat()
            entry = {"path": str(p), "name": p.name, "mtime": st.st_mtime, "size": st.st_size}
            prev = old.get(pid)
            ready = (self.cache_dir / "thumb" / f"{pid}.jpg").exists() and (self.cache_dir / "web" / f"{pid}.jpg").exists()
            if prev and prev["mtime"] == entry["mtime"] and prev["size"] == entry["size"] and ready:
                entry["faces"] = prev["faces"]
            else:
                todo.append((str(p), pid, str(self.cache_dir), self.detect_side))
            found[pid] = entry

        print(f"Photos in folder: {len(found)}  |  to process: {len(todo)}  |  cached: {len(found) - len(todo)}")
        if todo:
            workers = workers or max(1, (os.cpu_count() or 2) - 1)
            t0, done, failed = time.time(), 0, []
            with ProcessPoolExecutor(workers, initializer=_init_worker, initargs=(self.detector, self.model)) as pool:
                for pid, vecs, err in pool.map(_process, todo, chunksize=2):
                    done += 1
                    if err:
                        failed.append((found[pid]["name"], err))
                        del found[pid]  # unreadable file: leave it out of the gallery
                    else:
                        found[pid]["faces"] = np.array(vecs, dtype=np.float32).reshape(-1, 128)
                    if done % 10 == 0 or done == len(todo):
                        rate = done / (time.time() - t0)
                        print(f"  processed {done}/{len(todo)}  ({rate:.1f} photos/s)", flush=True)
            for name, err in failed:
                print(f"  skipped {name}: {err}")
            pkl.write_bytes(pickle.dumps({"meta": self.meta, "photos": found}))
        elif not pkl.exists() or old != found:
            pkl.write_bytes(pickle.dumps({"meta": self.meta, "photos": found}))

        self.photos = found
        rows, owner = [], []
        for pid, e in found.items():
            for v in e["faces"]:
                rows.append(v)
                owner.append(pid)
        self.vecs = np.array(rows, dtype=np.float32).reshape(-1, 128)
        self.owner = owner
        no_face = sum(1 for e in found.values() if len(e["faces"]) == 0)
        print(f"Indexed {len(owner)} faces in {len(found)} photos ({no_face} photos with no face found).")

    def search(self, vec, tolerance: float):
        """Photos containing a face within `tolerance` of `vec`, best match first."""
        if not len(self.owner):
            return []
        d = np.linalg.norm(self.vecs - vec, axis=1)
        best = {}
        for i in np.where(d <= tolerance)[0]:
            pid = self.owner[i]
            if pid not in best or d[i] < best[pid]:
                best[pid] = float(d[i])
        return sorted(best.items(), key=lambda kv: kv[1])
