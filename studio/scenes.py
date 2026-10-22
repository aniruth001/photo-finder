"""Search by description: "cake cutting", "group photo on the stage", "mehendi".

Every ready photo gets a CLIP vector. CLIP is an open-source image-and-text model; it runs on this server, so photos
never leave it and nothing is paid per photo or per search. A typed description becomes a vector in the same space,
and the event's photos closest to it are the results. The same vectors give each event a few suggestion chips
("Mehendi · 11") from a short list of common moments.

The model files (about 156 MB) are downloaded once with `python -m studio get-scene-model`. Without them, or with the
studio's "Search by description" setting off, nothing is indexed and the search isn't offered. Indexing runs in its
own thread from each photo's thumbnail, after face processing has finished, newest photos first: an event becomes
searchable a few minutes after its upload, and events uploaded before this existed are filled in on their own.

How close is close enough was measured on a real reception (101 photos, 13 searches; see `strong`): CLIP scores
are relative, so a photo counts when it scores high, or clearly higher than the rest of its event. When nothing
qualifies, the closest photos are offered and labelled as such, rather than nothing or a confident wrong answer.
"""
import hashlib
import importlib.util
import os
import threading
import time
import traceback
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import Depends, Request
from pydantic import BaseModel

from . import db
from .storage import get_storage

MODEL = "clip-b32-q"  # stored with each vector, so a future model can re-index without mixing the two
MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "clip"
SOURCE = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/d15189d7028b43f1d3e65039190477f6af591c2a/"
FILES = {  # file -> (path at SOURCE, sha256): OpenAI's CLIP ViT-B/32 (MIT licence), ONNX export, 8-bit
    "vision_model_quantized.onnx": ("onnx/vision_model_quantized.onnx", "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299"),
    "text_model_quantized.onnx": ("onnx/text_model_quantized.onnx", "73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a"),
    "tokenizer.json": ("tokenizer.json", "f7f3b7af117d467b58374797691a6438d3e6b9e9cef800dfd5dced7f697a90cd"),
}
DIM = 512
MEAN = np.array([0.48145466, 0.4578275, 0.40821073], np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], np.float32)
STRONG, MAYBE, Z = 0.25, 0.23, 2.4   # see strong()
CLOSEST = 12                          # photos offered when nothing matches clearly
MAX_QUERY = 100

# suggestion chips: (label shown, what CLIP is asked). Kept to moments that tested reliably on a real event; a chip
# appears for an event only when at least two of its photos clearly match.
TAGS = [
    ("Couple on stage", "the bride and groom on a decorated stage"),
    ("Group photos", "a large group photo of family and guests"),
    ("Selfies", "a selfie"),
    ("Mehendi", "mehendi henna on hands"),
    ("Rituals", "a traditional ritual ceremony"),
    ("Garlands", "people wearing flower garlands"),
    ("Decoration", "flower decoration at a venue"),
    ("Sarees", "a woman in a silk saree"),
    ("Gifts", "a person giving a gift box"),
    ("Elders", "an elderly person"),
    ("Kids", "children"),
    ("Cake", "cutting a cake"),
    ("Food", "food served on plates"),
    ("Dancing", "people dancing"),
    ("Couple portrait", "a romantic couple portrait"),
    ("Sunset", "a sunset sky"),
]
TAG_PROMPTS = {label.lower(): prompt for label, prompt in TAGS}


# ---------------------------------------------------------------------------------------------- the model
def installed() -> bool:
    """Model files present and the runtime importable."""
    return (all((MODEL_DIR / f).exists() for f in FILES)
            and importlib.util.find_spec("onnxruntime") is not None and importlib.util.find_spec("tokenizers") is not None)


def download(log=print) -> None:
    """Fetch the model files that are missing or damaged, and check each one against its pinned checksum."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, (path, sha) in FILES.items():
        dest = MODEL_DIR / name
        if dest.exists() and _sha256(dest) == sha:
            log(f"  {name}: already here")
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        log(f"  {name}: downloading…")
        with urllib.request.urlopen(SOURCE + path, timeout=60) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        if _sha256(tmp) != sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{name}: the download doesn't match its checksum. Try again.")
        os.replace(tmp, dest)
        log(f"  {name}: {dest.stat().st_size / 1e6:.0f} MB, checksum OK")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _unit(v: np.ndarray) -> np.ndarray:
    return (v / np.maximum(np.linalg.norm(v, axis=-1, keepdims=True), 1e-9)).astype(np.float32)


class ClipEngine:
    """CLIP ViT-B/32 on onnxruntime: images and text to unit vectors in one space. Thread-safe (onnxruntime is)."""

    def __init__(self, folder: Path = MODEL_DIR, threads: Optional[int] = None):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        so = ort.SessionOptions()
        so.intra_op_num_threads = threads or max(1, min(4, (os.cpu_count() or 2) // 2))  # leave room for the web server
        cpu = ["CPUExecutionProvider"]
        self.vision = ort.InferenceSession(str(folder / "vision_model_quantized.onnx"), so, providers=cpu)
        self.textual = ort.InferenceSession(str(folder / "text_model_quantized.onnx"), so, providers=cpu)
        self.tok = Tokenizer.from_file(str(folder / "tokenizer.json"))

    @staticmethod
    def _pixels(im) -> np.ndarray:
        from PIL import Image

        im = im.convert("RGB")
        w, h = im.size
        s = 224 / min(w, h)
        im = im.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
        w, h = im.size
        l, t = (w - 224) // 2, (h - 224) // 2
        a = (np.asarray(im.crop((l, t, l + 224, t + 224)), np.float32) / 255 - MEAN) / STD
        return a.transpose(2, 0, 1)

    def images(self, ims) -> np.ndarray:
        return _unit(self.vision.run(None, {"pixel_values": np.stack([self._pixels(im) for im in ims])})[0])

    def text(self, s: str) -> np.ndarray:
        ids = self.tok.encode(s).ids
        if len(ids) > 77:
            ids = ids[:76] + ids[-1:]  # CLIP reads at most 77 tokens; keep the end-of-text marker
        return _unit(self.textual.run(None, {"input_ids": np.array([ids], np.int64)})[0])[0]


_engine = None
_engine_lock = threading.Lock()
_texts: OrderedDict = OrderedDict()   # prompt -> vector; the chips and common searches are asked again and again


def set_engine(e) -> None:
    """Use this engine (tests use a small fake one); None goes back to loading the real model when needed."""
    global _engine
    with _engine_lock:
        _engine = e
        _texts.clear()
        index.clear()


def engine():
    """The engine, loading the model on first use; None when it isn't installed."""
    global _engine
    with _engine_lock:
        if _engine is None and installed():
            _engine = ClipEngine()
        return _engine


def available() -> bool:
    return _engine is not None or installed()


def text_vec(prompt: str) -> np.ndarray:
    hit = _texts.get(prompt)
    if hit is None:
        hit = engine().text(prompt)
        _texts[prompt] = hit
        while len(_texts) > 1000:
            _texts.popitem(last=False)
    return hit


def prompt_for(q: str) -> str:
    """What CLIP is asked for a typed search: a chip's own wording, or the words as "a photo of …"."""
    q = " ".join(q.split())[:MAX_QUERY]
    if q.lower() in TAG_PROMPTS:
        return "a photo of " + TAG_PROMPTS[q.lower()]
    return q if q.lower().startswith(("a photo", "photo of", "an image", "a picture")) else "a photo of " + q


def strong(scores: np.ndarray) -> np.ndarray:
    """Which photos clearly match: a high score, or a fair one that stands well clear of the rest of the event.

    Scores alone can't separate them (on the test event a wrong "dancing" photo scored 0.242, a right "giving a
    gift" one 0.243), and the spread alone fails for common searches, where many photos match equally well."""
    if not len(scores):
        return np.zeros(0, bool)
    sd = float(scores.std())
    z = (scores - scores.mean()) / sd if sd > 1e-6 else np.zeros_like(scores)
    return (scores >= STRONG) | ((scores >= MAYBE) & (z >= Z))


# ---------------------------------------------------------------------------------------------- an event's vectors
class SceneIndex:
    """Each recently searched event's vectors in memory, reloaded when its photos change."""

    def __init__(self, max_events: int = 64):
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.max_events = max_events  # 64 weddings of 5,000 photos is about 650 MB

    def clear(self):
        with self.lock:
            self.cache.clear()

    @staticmethod
    def version(event_id):
        r = db.q1("SELECT COUNT(*) c, COALESCE(MAX(photo_id), 0) m FROM photo_scenes WHERE event_id=? AND model=?",
                  (event_id, MODEL))
        return r["c"], r["m"]

    def get(self, event_id):
        """(version, vectors (n, 512), photo ids (n,)) of the event's readable photos."""
        ver = self.version(event_id)
        with self.lock:
            hit = self.cache.get(event_id)
            if hit and hit[0] == ver:
                self.cache.move_to_end(event_id)
                return hit
        rows = db.q("SELECT photo_id, vec FROM photo_scenes WHERE event_id=? AND model=? AND length(vec)=?",
                    (event_id, MODEL, DIM * 4))
        vecs = (np.frombuffer(b"".join(r["vec"] for r in rows), np.float32).reshape(-1, DIM) if rows
                else np.zeros((0, DIM), np.float32))
        hit = (ver, vecs, np.array([r["photo_id"] for r in rows], np.int64), {})
        with self.lock:
            self.cache[event_id] = hit
            self.cache.move_to_end(event_id)
            while len(self.cache) > self.max_events:
                self.cache.popitem(last=False)
        return hit


index = SceneIndex()


def search(event_id: int, q: str):
    """(photo ids best first, whether they clearly match). Nothing clear: the CLOSEST nearest, flagged False."""
    _, vecs, ids, _ = index.get(event_id)
    if not len(ids):
        return [], False
    s = vecs @ text_vec(prompt_for(q))
    order = np.argsort(-s, kind="stable")
    keep = strong(s)
    hits = [int(ids[i]) for i in order if keep[i]]
    if hits:
        return hits, True
    return [int(ids[i]) for i in order[:CLOSEST]], False


def event_tags(event_id: int) -> list:
    """The suggestion chips for an event: [{label, count}], most photos first, at most 8."""
    ver, vecs, ids, memo = index.get(event_id)
    if "tags" not in memo:
        out = []
        if len(ids):
            scores = vecs @ np.stack([text_vec("a photo of " + p) for _, p in TAGS]).T
            for k, (label, _) in enumerate(TAGS):
                n = int(strong(scores[:, k]).sum())
                if n >= 2:
                    out.append({"label": label, "count": n})
        memo["tags"] = sorted(out, key=lambda t: -t["count"])[:8]
    return memo["tags"]


def counts(event_id: int) -> dict:
    r = db.q1("""SELECT COUNT(*) ready, COALESCE(SUM(EXISTS(SELECT 1 FROM photo_scenes s WHERE s.photo_id=p.id AND s.model=?)), 0) done
                 FROM photos p WHERE p.event_id=? AND p.status='ready'""", (MODEL, event_id))
    return {"ready": r["ready"], "indexed": r["done"]}


def forget_account(account_id: int) -> None:
    """The studio turned the search off: its vectors go (they come back, re-made, if it's turned on again)."""
    db.run("DELETE FROM photo_scenes WHERE event_id IN (SELECT id FROM events WHERE account_id=?)", (account_id,))
    index.clear()


# ---------------------------------------------------------------------------------------------- background indexing
class SceneIndexer:
    """Gives ready photos their vector, a batch at a time, while face processing is idle."""

    BATCH = 16

    def __init__(self, busy=lambda: False):
        self.busy = busy  # True while photos still wait for face processing: that goes first
        self.stop_flag = threading.Event()
        self.wake = threading.Event()
        self.thread = None

    def start(self):
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._loop, name="scene-indexer", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=10)

    def poke(self):
        self.wake.set()

    def _sleep(self, seconds):
        self.wake.wait(seconds)
        self.wake.clear()

    def _loop(self):
        while not self.stop_flag.is_set():
            try:
                if not available():
                    self._sleep(60)
                    continue
                if self.busy():
                    self._sleep(3)
                    continue
                rows = self.todo(self.BATCH)
                if not rows:
                    self._sleep(5)
                    continue
                self.run(rows)
            except Exception:  # never let the indexer die; try again shortly
                traceback.print_exc()
                self._sleep(10)

    @staticmethod
    def todo(n):
        return db.q("""SELECT p.id, p.account_id, p.event_id, p.public_id, p.ext FROM photos p
                       JOIN accounts a ON a.id=p.account_id
                       WHERE p.status='ready' AND a.scene_search=1 AND a.disabled_at IS NULL
                         AND NOT EXISTS (SELECT 1 FROM photo_scenes s WHERE s.photo_id=p.id AND s.model=?)
                       ORDER BY p.id DESC LIMIT ?""", (MODEL, n))

    @staticmethod
    def run(rows):
        from PIL import Image

        store, ims, ok, bad = get_storage(), [], [], []
        for r in rows:
            try:
                with Image.open(store.photo_path(r, "thumb")) as im:
                    ims.append(im.convert("RGB"))
                ok.append(r)
            except Exception:
                bad.append(r)  # no readable thumbnail: marked, so it isn't tried forever
        vecs = engine().images(ims) if ims else []
        put = """INSERT OR REPLACE INTO photo_scenes(photo_id, event_id, vec, model) SELECT ?, ?, ?, ?
                 WHERE EXISTS (SELECT 1 FROM photos WHERE id=? AND status='ready')"""  # deleted meanwhile: skip
        with db.tx():
            for r, v in zip(ok, vecs):
                db.run(put, (r["id"], r["event_id"], v.astype(np.float32).tobytes(), MODEL, r["id"]))
            for r in bad:
                db.run(put, (r["id"], r["event_id"], b"", MODEL, r["id"]))
        return len(ok)


# ---------------------------------------------------------------------------------------------- web
class SceneSwitch(BaseModel):
    enabled: bool


def mount_scenes(app, h):
    """h: helpers from server.create_app (user_dep, can, err, get_event, client_open, client_ip, limiter)."""
    err = h.err

    def turned_on(account_id) -> bool:
        r = db.q1("SELECT scene_search FROM accounts WHERE id=?", (account_id,))
        return bool(r and r["scene_search"])

    def query(q: str) -> str:
        q = " ".join((q or "").split())
        if not q:
            raise err(422, "Type what you're looking for, for example \"cake cutting\".", "q")
        if len(q) > MAX_QUERY:
            raise err(422, f"Keep it under {MAX_QUERY} characters.", "q")
        return q

    def usable(account_id):
        if not available():
            raise err(503, "Search by description isn't set up on this server yet.", "not_installed")
        if not turned_on(account_id):
            raise err(409, "Search by description is turned off in Settings.", "off")

    # ------------------------------------------------------------------ the studio (dashboard)
    @app.get("/api/scenes")
    def status(u: dict = Depends(h.user_dep)):
        r = db.q1("""SELECT COUNT(*) ready, COALESCE(SUM(EXISTS(SELECT 1 FROM photo_scenes s WHERE s.photo_id=p.id AND s.model=?)), 0) done
                     FROM photos p WHERE p.account_id=? AND p.status='ready'""", (MODEL, u["account_id"]))
        return {"installed": available(), "enabled": turned_on(u["account_id"]), "ready": r["ready"], "indexed": r["done"]}

    @app.post("/api/scenes")
    def switch(body: SceneSwitch, u: dict = Depends(h.can("company"))):
        db.run("UPDATE accounts SET scene_search=? WHERE id=?", (int(body.enabled), u["account_id"]))
        if not body.enabled:
            forget_account(u["account_id"])
        indexer = getattr(app.state, "scene_indexer", None)
        if indexer and body.enabled:
            indexer.poke()
        return status(u)

    @app.get("/api/events/{eid}/scenes")
    def event_status(eid: int, u: dict = Depends(h.user_dep)):
        h.get_event(u, eid)
        on = available() and turned_on(u["account_id"])
        return {"available": on, **counts(eid), "tags": event_tags(eid) if on else []}

    @app.get("/api/events/{eid}/describe")
    def studio_search(eid: int, q: str = "", u: dict = Depends(h.user_dep)):
        h.get_event(u, eid)
        q = query(q)
        usable(u["account_id"])
        ids, clear = search(eid, q)
        rows = _rows(ids, """SELECT p.id, p.public_id, p.orig_name, p.size, p.status, p.error, v.name AS vendor, a.picked_by AS album
                             FROM photos p LEFT JOIN vendor_links v ON v.id=p.vendor_id
                             LEFT JOIN album_picks a ON a.photo_id=p.id AND a.event_id=p.event_id WHERE p.status='ready' AND p.id IN ({})""")
        return {"q": q, "clear": clear, "photos": [{"id": r["public_id"], "name": r["orig_name"], "size": r["size"], "status": r["status"],
                                                     "error": r["error"], "vendor": r["vendor"], "album": r["album"]} for r in rows]}

    # ------------------------------------------------------------------ the client gallery (its link and PIN)
    @app.get("/c/{token}/scenes")
    def client_status(token: str, request: Request):
        e = h.client_open(token, request)
        on = available() and turned_on(e["account_id"]) and counts(e["id"])["indexed"] > 0
        return {"available": on, "tags": event_tags(e["id"]) if on else []}

    @app.get("/c/{token}/describe")
    def client_search(token: str, request: Request, q: str = ""):
        e = h.client_open(token, request)
        ip = h.client_ip(request)
        if not h.limiter.allow(("describe", ip, token), 60, 600) or not h.limiter.allow(("describe-ip", ip), 200, 600):
            raise err(429, "Too many searches. Please wait a few minutes.", "rate")
        q = query(q)
        usable(e["account_id"])
        ids, clear = search(e["id"], q)
        rows = _rows(ids, "SELECT p.id, p.public_id, p.orig_name, p.width, p.height, p.edited FROM photos p WHERE p.status='ready' AND p.id IN ({})")
        return {"q": q, "clear": clear,
                "photos": [{"id": r["public_id"], "name": r["orig_name"], "w": r["width"], "h": r["height"],
                            "edited": bool(r["edited"])} for r in rows]}


def _rows(ids, sql):
    """The rows for these photo ids, in their order (ids of photos deleted since indexing just drop out)."""
    found = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in db.q(sql.format(",".join("?" * len(part))), part):
            found[r["id"]] = r
    return [found[i] for i in ids if i in found]
