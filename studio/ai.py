"""AI helpers for the photo editor, run on this server with open-source models: no API, nothing paid per photo,
and photos never leave the studio's own machine.

  cutout   IS-Net "general use" (rembg's export; DIS, Apache-2.0): the subject's mask, for removing or replacing
           the background. ~2 s a photo on a laptop CPU.
  erase    LaMa (Carve's ONNX export, Apache-2.0): fills an area the photographer brushed over (a passer-by, a
           pole, a wire) from what's around it. Works on a 512 x 512 crop around the area, ~4 s.
  upscale  Real-ESRGAN general x4v3 (BSD-3-Clause): 2x or 4x larger photos for big album prints. Slow on a CPU
           (about a second per 256 x 256 tile), so it runs as a background job and saves an edited copy.
  sky      DETR panoptic (Facebook, Apache-2.0; Xenova's 8-bit ONNX export): where the sky is, for "Select sky" in the
           editor's masks. Its mask is coarse (a quarter of the input size), so it is refined from the photo itself: the
           sky's colours and smoothness are learnt from what the model is sure of, grown to the connected sky, and
           snapped to the edges with a guided filter. ~4 s.
  eyes     Intel's open-closed-eye-0001 (Apache-2.0): used by culling (studio/culling.py), not by the editor.
  faces    YuNet, already used for face search: where the faces are, for the editor's face-aware Auto fix.

The model files (about 435 MB) are downloaded once with `python -m studio get-ai-models`, each checked against a
pinned checksum. Without them the editor hides the features that need them; everything else keeps working.
"""
import hashlib
import io
import os
import threading
import time
import traceback
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np
from fastapi import Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from . import config, db, live

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "ai"
MODELS = {  # name -> (file, url, sha256, MB, what it does)
    "cutout": ("isnet-general-use.onnx",
               "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
               "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a", 179, "remove background"),
    "erase": ("lama_fp32.onnx",
              "https://huggingface.co/Carve/LaMa-ONNX/resolve/c3c0c9e468934d62e79c329e35d82dd09ff8c444/lama_fp32.onnx",
              "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6", 208, "erase objects"),
    "upscale": ("realesr-general-x4v3.onnx",
                "https://huggingface.co/jonathanst29/tinier-upscale-models/resolve/899dc1e4b22bbf1955c2f1739c085edc080cb366/realesr-general-x4v3.onnx",
                "924ebad6532777303582d4ce7811849b88869231a3ae7093e0f21200249df8d5", 5, "upscale"),
    "sky": ("detr-panoptic-q.onnx",
            "https://huggingface.co/Xenova/detr-resnet-50-panoptic/resolve/ea24b2d4e0bfae31f0a1299ba3fb892a2df064de/onnx/model_quantized.onnx",
            "93abfec8b8abd77329f203726b231ee5e5d4c2d52c881a74ab1c1134d888c41d", 44, "select sky"),
    "eyes": ("open_closed_eye.onnx",
             "https://storage.openvinotoolkit.org/repositories/open_model_zoo/public/2022.1/open-closed-eye-0001/open_closed_eye.onnx",
             "4daa100034482525a26c9afb9297c16580a531189e66e3d2b2ac7d32becfd593", 0.05, "eyes-closed check"),
}
MAX_INPUT = 4096           # longest side the editor may send (it scales down before sending)
MAX_UPLOAD = 40 << 20
ERASE_SIDE = 512           # LaMa's fixed input
UPSCALE_MAX = 8000         # longest side of an upscaled photo
TILE, PAD = 192, 16        # upscaling works tile by tile, with an overlap that is cut away


# ---------------------------------------------------------------------------------------------- the models
def installed(name: str) -> bool:
    return (MODEL_DIR / MODELS[name][0]).exists()


def download(names=None, log=print) -> None:
    """Fetch the model files that are missing or damaged, each checked against its pinned checksum."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name in names or MODELS:
        file, url, sha, mb, what = MODELS[name]
        dest = MODEL_DIR / file
        if dest.exists() and _sha256(dest) == sha:
            log(f"  {what}: already here")
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        log(f"  {what}: downloading {file} (about {mb:g} MB)…")
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        if _sha256(tmp) != sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{file}: the download doesn't match its checksum. Try again.")
        os.replace(tmp, dest)
        log(f"  {what}: checksum OK")


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class _Model:
    """One ONNX model, loaded on first use. One photo at a time per model: each run already uses every core."""

    def __init__(self, name):
        self.name, self.session, self.lock = name, None, threading.Lock()

    def run(self, feed: dict, timeout: float = 180):
        if not self.lock.acquire(timeout=timeout):
            raise TimeoutError("busy")
        try:
            if self.session is None:
                import onnxruntime as ort

                so = ort.SessionOptions()
                so.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
                so.log_severity_level = 3   # errors only: the sky export warns about its output shape on every run
                self.session = ort.InferenceSession(str(MODEL_DIR / MODELS[self.name][0]), so,
                                                    providers=["CPUExecutionProvider"])
            return self.session.run(None, feed)
        finally:
            self.lock.release()


_models = {k: _Model(k) for k in ("cutout", "erase", "upscale", "sky")}
SKY_CLASS = 187            # COCO panoptic "sky-other-merged" (the export's label list doesn't name it)
SKY_SIDE = 384             # the model's input, short side: ~3 s on a laptop CPU


def cutout_mask(im) -> "Image":
    """The subject's mask (L, 0-255) at the image's size."""
    from PIL import Image

    x = np.asarray(im.convert("RGB").resize((1024, 1024), Image.BILINEAR), np.float32) / 255.0 - 0.5
    out = _models["cutout"].run({"input_image": x.transpose(2, 0, 1)[None]})[0][0, 0]
    out = (out - out.min()) / max(float(out.max() - out.min()), 1e-6)
    return Image.fromarray((out * 255).astype(np.uint8)).resize(im.size, Image.BILINEAR)


def sky_mask(im) -> "Optional[Image]":
    """Where the sky is (L, 0-255, the image's size), or None when there is none."""
    import cv2
    from PIL import Image

    im = im.convert("RGB")
    work = im.copy()
    work.thumbnail((1024, 1024))
    w, h = work.size
    k = SKY_SIDE / min(w, h)
    W, H = max(32, int(round(w * k / 32) * 32)), max(32, int(round(h * k / 32) * 32))
    x = (np.asarray(work.resize((W, H), Image.BILINEAR), np.float32) / 255 - np.float32([0.485, 0.456, 0.406])) / np.float32([0.229, 0.224, 0.225])
    logits, _, masks = _models["sky"].run({"pixel_values": x.transpose(2, 0, 1)[None], "pixel_mask": np.ones((1, 64, 64), np.int64)})
    p = np.exp(logits[0] - logits[0].max(-1, keepdims=True))
    p /= p.sum(-1, keepdims=True)
    cls = p[:, :-1].argmax(-1)
    score = p[np.arange(len(cls)), cls]
    keep = np.flatnonzero(score > 0.7)
    if not len(keep) or not (cls[keep] == SKY_CLASS).any():
        return None
    prob = (1 / (1 + np.exp(-np.clip(masks[0][keep], -40, 40)))) * score[keep][:, None, None]   # each pixel goes to its likeliest segment
    seg = keep[prob.argmax(0)]
    coarse = np.where(np.isin(seg, keep[cls[keep] == SKY_CLASS]), prob.max(0), 0).astype(np.float32)
    if coarse.max() < 0.3:
        return None
    m = _refine_sky(np.asarray(work, np.float32) / 255, cv2.resize(coarse, (w, h), interpolation=cv2.INTER_LINEAR))
    return Image.fromarray((m * 255).astype(np.uint8)).resize(im.size, Image.BILINEAR)


def _refine_sky(rgb, c):
    """The model's coarse sky made exact: its colours and smoothness learnt from the sure part, grown to the connected
    sky of the same colour family, and the edges snapped to the photo (a guided filter)."""
    import cv2

    h, w = c.shape
    sure = c > 0.6
    if sure.sum() < 50:
        return c
    box = lambda a, r: cv2.blur(a, (2 * r + 1, 2 * r + 1))
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    gray = cv2.cvtColor((rgb * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)
    tex = cv2.blur(np.abs(cv2.Laplacian(gray, cv2.CV_32F)), (5, 5))
    smooth = np.exp(-(tex / (np.percentile(tex[sure], 90) + 2)) ** 2)
    X = lab[sure].reshape(-1, 3)
    inv = np.linalg.inv(np.cov(X.T) + np.eye(3) * 4)
    dd = lab.reshape(-1, 3) - X.mean(0)
    maha = np.sqrt(np.einsum("ij,jk,ik->i", dd, inv, dd)).reshape(h, w)   # how sky-coloured, along its own gradient
    cand = ((maha < 4.0) & (smooth > 0.35)).astype(np.uint8)
    _, lbl = cv2.connectedComponents(cand, connectivity=4)
    hit = np.unique(lbl[sure & (cand > 0)])
    grown = np.isin(lbl, hit[hit > 0])
    near = np.maximum(cv2.dilate((c > 0.25).astype(np.uint8), np.ones((31, 31), np.uint8)).astype(np.float32), grown)
    colour = np.maximum(np.exp(-(maha / 3.0) ** 2), grown * np.exp(-(maha / 4.0) ** 2) * 1.6)
    p = np.maximum(np.clip(colour * smooth * near, 0, 1), (c > 0.85) * smooth)
    g = gray / 255
    mI, mp = box(g, 8), box(p, 8)
    a = (box(g * p, 8) - mI * mp) / (box(g * g, 8) - mI * mI + 1e-3)
    return np.clip(box(a, 8) * g + box(mp - a * mI, 8), 0, 1)


def erase(im, mask) -> "Image":
    """LaMa on a 512 x 512 RGB image and its mask (white = fill this)."""
    from PIL import Image

    x = np.asarray(im.convert("RGB"), np.float32).transpose(2, 0, 1)[None] / 255.0
    m = (np.asarray(mask.convert("L"), np.float32) > 127).astype(np.float32)[None, None]
    out = _models["erase"].run({"image": x, "mask": m})[0][0]
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8).transpose(1, 2, 0))


def upscale(im, scale: int, progress=None) -> "Image":
    """Real-ESRGAN x4, tile by tile; for 2x each tile is brought down from 4x (sharper than upscaling 2x directly)."""
    from PIL import Image

    im = im.convert("RGB")
    w, h = im.size
    out = Image.new("RGB", (w * scale, h * scale))
    src = np.asarray(im, np.float32) / 255.0
    tiles = [(x, y) for y in range(0, h, TILE) for x in range(0, w, TILE)]
    for n, (x, y) in enumerate(tiles):
        x0, y0, x1, y1 = max(0, x - PAD), max(0, y - PAD), min(w, x + TILE + PAD), min(h, y + TILE + PAD)
        t = src[y0:y1, x0:x1].transpose(2, 0, 1)[None]
        r = _models["upscale"].run({"input": np.ascontiguousarray(t)})[0][0]
        r = (np.clip(r, 0, 1) * 255 + 0.5).astype(np.uint8).transpose(1, 2, 0)
        tile = Image.fromarray(r)
        if scale != 4:
            tile = tile.resize(((x1 - x0) * scale, (y1 - y0) * scale), Image.LANCZOS)
        cx, cy = (x - x0) * scale, (y - y0) * scale
        cw, ch = (min(w, x + TILE) - x) * scale, (min(h, y + TILE) - y) * scale
        out.paste(tile.crop((cx, cy, cx + cw, cy + ch)), (x * scale, y * scale))
        if progress:
            progress((n + 1) / len(tiles))
    return out


def find_faces(im) -> list:
    """[{x, y, w, h, eyes: [[x, y], [x, y]]}] in the image's pixels (YuNet)."""
    import cv2

    from faces import MODELS as FACE_MODELS

    rgb = np.asarray(im.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    det = cv2.FaceDetectorYN.create(str(FACE_MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.7, 0.3, 5000)
    det.setInputSize((bgr.shape[1], bgr.shape[0]))
    _, found = det.detect(bgr)
    return [{"x": round(float(f[0]), 1), "y": round(float(f[1]), 1), "w": round(float(f[2]), 1), "h": round(float(f[3]), 1),
             "eyes": [[round(float(f[4]), 1), round(float(f[5]), 1)], [round(float(f[6]), 1), round(float(f[7]), 1)]]}
            for f in ([] if found is None else found)]


# ---------------------------------------------------------------------------------------------- background jobs
class Jobs:
    """Upscaling, one job at a time, in its own thread. A restart picks up what was queued or running."""

    def __init__(self, plan_of, poke):
        self.plan_of, self.poke = plan_of, poke
        self.wake, self.stop_flag, self.thread = threading.Event(), threading.Event(), None

    def start(self):
        db.run("UPDATE ai_jobs SET status='queued', progress=0 WHERE status='running'")
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._loop, name="ai-jobs", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self):
        while not self.stop_flag.is_set():
            job = db.q1("SELECT * FROM ai_jobs WHERE status='queued' ORDER BY id LIMIT 1")
            if not job:
                self.wake.wait(5)
                self.wake.clear()
                continue
            db.run("UPDATE ai_jobs SET status='running' WHERE id=?", (job["id"],))
            try:
                result = self._upscale(job)
                db.run("UPDATE ai_jobs SET status='done', progress=1, result_photo_id=?, finished_at=? WHERE id=?",
                       (result, int(time.time()), job["id"]))
                live.publish(job["account_id"], "photos", event=job["event_id"])
            except Exception as e:
                if not isinstance(e, (ValueError, TimeoutError)):
                    traceback.print_exc()
                msg = str(e) if isinstance(e, ValueError) else "Upscaling failed on this photo. Try again."
                db.run("UPDATE ai_jobs SET status='failed', error=?, finished_at=? WHERE id=?", (msg, int(time.time()), job["id"]))
                live.publish(job["account_id"], "photos", event=job["event_id"])

    def _upscale(self, job) -> int:
        from faces import open_image

        from .ingest import add_photo
        from .storage import get_storage

        p = db.q1("SELECT * FROM photos WHERE id=?", (job["photo_id"],))
        acct = db.q1("SELECT plan FROM accounts WHERE id=?", (job["account_id"],))
        if not p or not acct:
            raise ValueError("The photo was deleted.")
        im = open_image(get_storage().photo_path(p, "orig"))
        scale = 4 if job["kind"] == "upscale4" else 2
        if max(im.size) * scale > UPSCALE_MAX:
            raise ValueError(f"Too large to upscale {scale}x: the result would be over {UPSCALE_MAX} pixels wide.")
        last = [0.0]

        def progress(f):
            if f - last[0] >= 0.02 or f >= 1:
                last[0] = f
                db.run("UPDATE ai_jobs SET progress=? WHERE id=?", (round(f * 0.97, 3), job["id"]))
            if self.stop_flag.is_set():
                raise TimeoutError("stopping")  # picked up again after the restart

        big = upscale(im, scale, progress)
        buf = io.BytesIO()
        big.save(buf, "JPEG", quality=94, subsampling=0)
        root = p["edited_of"] if p["edited"] and p["edited_of"] else p["id"]
        base = Path(p["orig_name"]).stem.removesuffix(" (edited)")
        res = add_photo(job["account_id"], job["event_id"], self.plan_of(acct["plan"]), f"{base} (upscaled {scale}x).jpg",
                        io.BytesIO(buf.getvalue()), edited=True, edited_of=root)
        if res["status"] == "duplicate":
            raise ValueError("This upscaled photo is already in the event.")
        if res["status"] != "queued":
            raise ValueError(res.get("error") or "The upscaled photo couldn't be saved.")
        self.poke()
        return db.q1("SELECT id FROM photos WHERE public_id=?", (res["id"],))["id"]


# ---------------------------------------------------------------------------------------------- the API
class UpscaleIn(BaseModel):
    scale: int = 2


def mount_ai(app, h) -> Jobs:
    """h: helpers from server.create_app (user_dep, can, err, get_event, plan_of, poke)."""
    err = h.err
    jobs = Jobs(h.plan_of, h.poke)

    def need(name):
        if not installed(name):
            raise err(503, "This needs the AI models on the server: python -m studio get-ai-models", "no_model")

    def image_in(file: UploadFile, max_side: int = MAX_INPUT):
        from PIL import Image

        data = file.file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise err(413, "That image is too large.", "too_large")
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
        except Exception:
            raise err(422, "That image couldn't be read.", "bad_image")
        if max(im.size) > max_side:
            raise err(422, f"Send at most {max_side} pixels on the long side.", "too_big")
        return im

    def png(im) -> Response:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})

    def busy(fn, *a):
        try:
            return fn(*a)
        except TimeoutError:
            raise err(503, "The server is busy with another photo. Try again in a moment.", "busy")

    @app.get("/api/ai")
    def ai_status(u: dict = Depends(h.user_dep)):
        """What the editor can offer on this server."""
        from faces import raw_supported

        return {"cutout": installed("cutout"), "erase": installed("erase"), "upscale": installed("upscale"), "sky": installed("sky"),
                "raw": raw_supported(),
                "faces": True, "eyes": installed("eyes"), "upscale_max": UPSCALE_MAX}

    @app.post("/api/ai/cutout")
    def ai_cutout(file: UploadFile = File(...), u: dict = Depends(h.can("upload"))):
        """The subject's mask (grey PNG, same size as the image sent: white = keep)."""
        need("cutout")
        return png(busy(cutout_mask, image_in(file)))

    @app.post("/api/ai/sky")
    def ai_sky(file: UploadFile = File(...), u: dict = Depends(h.can("upload"))):
        """The sky's mask (grey PNG, same size as the image sent: white = sky); 404 no_sky when there is none."""
        need("sky")
        m = busy(sky_mask, image_in(file))
        if m is None:
            raise err(404, "No sky found in this photo.", "no_sky")
        return png(m)

    @app.post("/api/ai/erase")
    def ai_erase(file: UploadFile = File(...), mask: UploadFile = File(...), u: dict = Depends(h.can("upload"))):
        """A 512 x 512 crop with the area to fill painted white in `mask`: the crop, filled in."""
        need("erase")
        im, m = image_in(file, ERASE_SIDE), image_in(mask, ERASE_SIDE)
        if im.size != (ERASE_SIDE, ERASE_SIDE) or m.size != im.size:
            raise err(422, f"Send a {ERASE_SIDE} x {ERASE_SIDE} crop and a mask of the same size.", "bad_size")
        return png(busy(erase, im, m))

    @app.post("/api/ai/faces")
    def ai_faces(file: UploadFile = File(...), u: dict = Depends(h.user_dep)):
        """Faces in the image sent (for the editor's Auto fix), in its pixels."""
        im = image_in(file, 2048)
        return {"w": im.size[0], "h": im.size[1], "faces": find_faces(im)}

    def job_out(r) -> dict:
        res = db.q1("SELECT public_id, orig_name FROM photos WHERE id=?", (r["result_photo_id"],)) if r["result_photo_id"] else None
        return {"id": r["id"], "kind": r["kind"], "status": r["status"], "progress": r["progress"], "error": r["error"],
                "result": {"id": res["public_id"], "name": res["orig_name"]} if res else None}

    @app.post("/api/events/{eid}/photos/{pid}/upscale")
    def start_upscale(eid: int, pid: str, body: UpscaleIn, u: dict = Depends(h.can("upload"))):
        """Queue a 2x or 4x upscale of this photo. The result is saved as an edited copy in the event."""
        need("upscale")
        h.get_event(u, eid)
        if body.scale not in (2, 4):
            raise err(422, "Choose 2x or 4x.", "bad_scale")
        p = db.q1("SELECT id, width, height, status FROM photos WHERE public_id=? AND event_id=? AND account_id=?",
                  (pid, eid, u["account_id"]))
        if not p:
            raise err(404, "Photo not found.", "not_found")
        if p["status"] != "ready":
            raise err(409, "This photo is still being processed.", "not_ready")
        if max(p["width"] or 0, p["height"] or 0) * body.scale > UPSCALE_MAX:
            raise err(422, f"Too large to upscale {body.scale}x: the result would be over {UPSCALE_MAX} pixels wide.", "too_big")
        open_ = db.q1("SELECT * FROM ai_jobs WHERE photo_id=? AND status IN ('queued','running')", (p["id"],))
        if open_:
            return job_out(open_)
        if db.q1("SELECT COUNT(*) n FROM ai_jobs WHERE account_id=? AND status IN ('queued','running')", (u["account_id"],))["n"] >= 20:
            raise err(429, "Twenty photos are already waiting to be upscaled. Try again when some are done.", "queue_full")
        jid = db.ex("""INSERT INTO ai_jobs(account_id, event_id, photo_id, kind, created_by, created_at)
                       VALUES(?,?,?,?,?,?)""", (u["account_id"], eid, p["id"], f"upscale{body.scale}", u["name"] or u["email"], int(time.time())))
        jobs.wake.set()
        return job_out(db.q1("SELECT * FROM ai_jobs WHERE id=?", (jid,)))

    @app.get("/api/events/{eid}/ai-jobs")
    def list_jobs(eid: int, u: dict = Depends(h.user_dep)):
        """This event's upscaling jobs from the last day, newest first."""
        h.get_event(u, eid)
        rows = db.q("""SELECT j.*, p.public_id AS photo FROM ai_jobs j LEFT JOIN photos p ON p.id=j.photo_id
                       WHERE j.event_id=? AND j.account_id=? AND j.created_at > ? ORDER BY j.id DESC LIMIT 50""",
                    (eid, u["account_id"], int(time.time()) - 86400))
        return [{**job_out(r), "photo": r["photo"]} for r in rows]

    return jobs
