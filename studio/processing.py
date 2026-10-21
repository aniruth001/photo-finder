"""Background photo processing and per-event face search.

Uploads only save the original and mark the photo 'uploaded'. A single dispatcher thread hands those to a pool
of worker processes (thumbnail + web copy + face vectors) and writes the results back, so a big upload never
slows the web server down and a restart resumes where it stopped.
"""
import multiprocessing
import threading
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from concurrent.futures.process import BrokenProcessPool

import numpy as np

from . import config, db
from .storage import get_storage

THUMB_SIDE = 480
WEB_SIDE = 1600

_engines = {}
_detector = "hog"


def _init_worker(detector):
    global _detector
    _detector = detector
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C is for the server, which stops the pool; no traceback from each worker
    import faces  # noqa: F401  registers the HEIC opener before the first photo is opened


def _engine_for(model):
    if model not in _engines:  # built on first use in each worker process
        from faces import make_engine

        _engines[model] = make_engine(model, _detector)
    return _engines[model]


def _save_jpeg(im, side, path):
    c = im.copy()
    c.thumbnail((side, side))
    c.save(path, "JPEG", quality=82, optimize=True)


def _process(job):
    """Runs in a worker process. Returns (width, height, [face vector bytes]).

    JPEGs are decoded at reduced size (PIL draft mode, up to 8x faster than a full decode); thumbnails, web copies
    and face detection all work from that image, which is never smaller than the detection size.
    """
    from PIL import Image, ImageOps

    orig, thumb, web, detect_side, model = job
    im = Image.open(orig)
    ow, oh = im.size
    rot = im.getexif().get(274)
    if im.format == "JPEG":
        im.draft("RGB", (detect_side, detect_side))  # picks a scale that stays >= detect_side on the long edge
    im = ImageOps.exif_transpose(im).convert("RGB")
    if rot in (5, 6, 7, 8):
        ow, oh = oh, ow  # EXIF rotation swaps the axes
    w, h = ow, oh
    _save_jpeg(im, THUMB_SIDE, thumb)
    _save_jpeg(im, WEB_SIDE, web)
    return w, h, [v.astype(np.float32).tobytes() for _, v in _engine_for(model).faces(im, detect_side=detect_side)]


def _friendly(e: BaseException) -> str:
    name = type(e).__name__
    if name in ("UnidentifiedImageError", "DecompressionBombError", "SyntaxError", "OSError"):
        return "We couldn't read this image (corrupt or unsupported)."
    return f"{name}: {str(e)[:150]}"


class Processor:
    def __init__(self):
        self.pool = None
        self.thread = None
        self.stop_flag = threading.Event()
        self.wake = threading.Event()
        self.capacity = 2

    def start(self):
        workers = config.settings.workers or max(1, (__import__("os").cpu_count() or 2) - 1)
        self.capacity = workers * 2
        self._new_pool(workers)
        self.workers = workers
        # photos that were mid-processing when the server stopped go back in the queue
        db.run("UPDATE photos SET status='uploaded' WHERE status='processing'")
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._loop, name="photo-processor", daemon=True)
        self.thread.start()
        self.wake.set()

    def _new_pool(self, workers):
        self.pool = ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"),
                                        initializer=_init_worker, initargs=(config.settings.detector,))

    def poke(self):
        self.wake.set()

    def stop(self):
        self.stop_flag.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=10)
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def idle(self) -> bool:
        r = db.q1("SELECT COUNT(*) c FROM photos WHERE status IN ('uploaded','processing')")
        return r["c"] == 0

    # -- internals -------------------------------------------------------------------------------------------
    def _claim(self, n):
        with db.tx() as c:
            rows = c.execute(
                """SELECT p.id, p.account_id, p.event_id, p.public_id, p.ext, e.face_model FROM photos p
                   JOIN events e ON e.id=p.event_id WHERE p.status='uploaded' ORDER BY p.id LIMIT ?""",
                (n,)).fetchall()
            if rows:
                c.execute(f"UPDATE photos SET status='processing' WHERE id IN ({','.join('?' * len(rows))})",
                          [r["id"] for r in rows])
        return rows

    def _job(self, r):
        s = get_storage()
        s.ensure(r["account_id"], r["event_id"])
        return (str(s.photo_path(r, "orig")), str(s.photo_path(r, "thumb")), str(s.photo_path(r, "web")),
                config.settings.detect_side, r["face_model"])

    def _loop(self):
        inflight = {}
        while not self.stop_flag.is_set():
            try:
                free = self.capacity - len(inflight)
                if free > 0:
                    for r in self._claim(free):
                        inflight[self.pool.submit(_process, self._job(r))] = r
                if not inflight:
                    self.wake.wait(2)
                    self.wake.clear()
                    continue
                done, _ = wait(list(inflight), timeout=1, return_when=FIRST_COMPLETED)
                for f in done:
                    self._finish(inflight.pop(f), f)
            except BrokenProcessPool:
                # a worker died (for example out of memory): fail what was running, start a fresh pool
                for r in inflight.values():
                    db.run("UPDATE photos SET status='failed', error=? WHERE id=?",
                           ("Processing crashed on this file. Try Retry, or upload a smaller copy.", r["id"]))
                inflight.clear()
                self._new_pool(self.workers)
            except Exception:  # never let the dispatcher die
                import traceback

                traceback.print_exc()
                self.stop_flag.wait(1)

    def _finish(self, r, fut):
        s = get_storage()
        try:
            w, h, vecs = fut.result()
        except BrokenProcessPool:
            raise
        except Exception as e:
            db.run("UPDATE photos SET status='failed', error=? WHERE id=? AND status='processing'",
                   (_friendly(e), r["id"]))
            return
        with db.tx() as c:
            alive = c.execute("""SELECT e.faces_purged FROM photos p JOIN events e ON e.id=p.event_id
                                 WHERE p.id=? AND p.status='processing'""", (r["id"],)).fetchone()
            if alive:
                c.execute("DELETE FROM faces WHERE photo_id=?", (r["id"],))
                if alive["faces_purged"]:  # face data deleted for privacy: thumbnails yes, face vectors no
                    vecs = []
                c.executemany("INSERT INTO faces(event_id, photo_id, vec, model) VALUES(?,?,?,?)",
                              [(r["event_id"], r["id"], v, r["face_model"]) for v in vecs])
                c.execute("UPDATE photos SET status='ready', width=?, height=?, error=NULL WHERE id=?", (w, h, r["id"]))
        if not alive:  # the photo or its event was deleted while we worked: don't leave files behind
            for kind in ("thumb", "web"):
                s.photo_path(r, kind).unlink(missing_ok=True)


def switch_model(event_id, old_model, old_tolerance, new_model):
    """Point an event at another face engine and queue its photos to be indexed again.

    The tolerance keeps its meaning (strict / balanced / relaxed) in the new engine's scale. Until each photo is
    redone, search only sees the photos already done with the new engine, never a mix of the two.
    """
    from faces import PRESETS

    old = PRESETS.get(old_model, PRESETS["dlib"])
    level = min(old, key=lambda k: abs(old[k] - old_tolerance))
    with db.tx():
        db.run("UPDATE events SET face_model=?, tolerance=? WHERE id=?", (new_model, PRESETS[new_model][level], event_id))
        db.run("UPDATE photos SET status='uploaded', error=NULL WHERE event_id=? AND status IN ('ready','failed')",
               (event_id,))


def upgrade_events() -> int:
    """At start-up: move every event still on an older face engine to the configured one (re-indexing it).
    Events whose face data was deleted on purpose are left alone."""
    want = config.settings.face_model
    rows = db.q("SELECT id, face_model, tolerance FROM events WHERE face_model != ? AND faces_purged=0", (want,))
    for r in rows:
        switch_model(r["id"], r["face_model"], r["tolerance"], want)
    if rows:
        print(f"Re-indexing {len(rows)} event(s) with the {want} face engine. Guest search keeps working meanwhile.")
    return len(rows)


def confirmed(vecs, d, tolerance, max_anchors=200):
    """Indexes of the faces that match, with a check for the grey zone just under the tolerance.

    A selfie comes from another camera, lighting and processing than the event photos, so selfie-to-photo
    distances are the noisy part: near the threshold they swing with the phone, and look-alikes land there.
    Faces the selfie is sure about (well under the tolerance) are accepted. A face in the grey zone is accepted
    only if it is also close to one of those sure faces, a photo-to-photo comparison from the same camera, which is
    far more reliable. With no sure face at all, only the safer lower part of the zone counts. (Measured on a real
    wedding: this kept two different phones' results for the same guest consistent; tests/test_face_engine.py.)
    """
    sure, top, link = tolerance - 0.10, tolerance + 0.02, tolerance - 0.05
    anchors = np.where(d <= sure)[0]
    if not len(anchors):
        return np.where(d <= link)[0]
    anchors = anchors[np.argsort(d[anchors])[:max_anchors]]
    band = np.where((d > sure) & (d <= top))[0]
    if not len(band):
        return anchors
    # unit vectors: |a - b|^2 = 2 - 2 a.b
    near = np.sqrt(np.maximum(0.0, 2.0 - 2.0 * (vecs[band] @ vecs[anchors].T))).min(axis=1)
    return np.concatenate([np.where(d <= sure)[0], band[near <= link]])


class VectorIndex:
    """Face vectors of recently searched events, kept in memory. Reloaded when an event's faces change."""

    def __init__(self, max_bytes: int = 1 << 30):
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.max_bytes = max_bytes  # about 2 million faces (a face vector is 512 bytes); least recently searched events go first

    @staticmethod
    def _version(event_id, model):
        r = db.q1("SELECT COUNT(*) c, COALESCE(MAX(id), 0) m FROM faces WHERE event_id=? AND model=?", (event_id, model))
        return r["c"], r["m"]

    def get(self, event_id, model):
        key = (event_id, model)
        ver = self._version(event_id, model)
        with self.lock:
            hit = self.cache.get(key)
            if hit and hit[0] == ver:
                self.cache.move_to_end(key)
                return hit[1], hit[2]
        rows = db.q("SELECT photo_id, vec FROM faces WHERE event_id=? AND model=?", (event_id, model))
        if rows:
            vecs = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(-1, 128)
            owners = np.array([r["photo_id"] for r in rows], dtype=np.int64)
        else:
            vecs, owners = np.zeros((0, 128), np.float32), np.zeros(0, np.int64)
        with self.lock:
            self.cache[key] = (ver, vecs, owners)
            self.cache.move_to_end(key)
            while len(self.cache) > 1 and sum(v[1].nbytes for v in self.cache.values()) > self.max_bytes:
                self.cache.popitem(last=False)
        return vecs, owners

    def search(self, event_id, model, vec, tolerance):
        """[(photo_id, distance)] best match first, one entry per photo. Only vectors of `model` are compared."""
        vecs, owners = self.get(event_id, model)
        if not len(owners):
            return []
        d = np.linalg.norm(vecs - vec, axis=1)
        hits = confirmed(vecs, d, tolerance) if model == "sface" else np.where(d <= tolerance)[0]
        best = {}
        for i in hits:
            pid = int(owners[i])
            best[pid] = min(best.get(pid, 9.0), float(d[i]))
        return sorted(best.items(), key=lambda kv: kv[1])
