"""Culling: which photos deserve a second look before the client sees them.

Three measures per photo, worked out while it is processed (or afterwards, from the web copy, for photos that
were processed before this existed):

  blur   How soft the sharpest part of the photo is, 0 (crisp) to 1 (very soft): the Crete-Roffet measure, which
         compares the photo with a re-blurred copy of itself. Unlike edge strength it doesn't mistake a smooth
         subject (a palm, a plain wall) for a blurry one. It is taken over the most detailed tiles, so a sharp
         subject in front of a soft background counts as sharp. On 104 real wedding photos sharp shots scored
         0.18-0.30; copies blurred enough to look soft (Gaussian 2 px at 1000 px, 15 px of motion) 0.42 and up.
  dhash  A 64-bit picture fingerprint (difference hash). Burst shots and re-sent copies differ by a few bits;
         `similar()` groups them and picks the best of each group.
  eyes   How many of the main faces look like their eyes are shut: YuNet finds the faces and their eye
         positions, and a small open/closed-eye classifier (Intel's open-closed-eye-0001, Apache-2.0, 46 KB)
         looks at each eye. It also catches people looking down, which is usually a reject too. It is a hint,
         not a verdict: the dashboard says "Check eyes".

Nothing here changes a photo. The dashboard filters on these, and the best of each group of near-duplicates
is marked so the rest can be selected in one go.
"""
from pathlib import Path
from typing import Optional

import numpy as np

from . import db

BLURRY = 0.40            # blur at or above this: shown under "Blurry"
SIMILAR_BITS = 6         # fingerprints this close (of 64 bits) are the same moment
NEIGHBOURS = 40          # a photo is compared with the next 40 by name: bursts are consecutive
ANALYZE_SIDE = 1600      # eyes are judged at this size (what the classifier was checked against)
BLUR_SIDE = 1000
MIN_FACE = 70 / 1600     # faces narrower than this share of the long side are too small to judge
MAIN_FACE = 0.5          # ...and so are faces less than half as wide as the biggest one (people in the back)
EYE_CROP = 0.8           # eye crop: this times the distance between the eyes
EYES_SHUT = 0.3          # both eyes "open" below this: the face counts as eyes shut

ROOT = Path(__file__).resolve().parent.parent
YUNET = ROOT / "models" / "face_detection_yunet_2023mar.onnx"
EYE_MODEL = ROOT / "models" / "ai" / "open_closed_eye.onnx"

_det = None
_eye = None


def _detector():
    global _det
    if _det is None:
        import cv2

        _det = cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.75, 0.3, 5000)
    return _det


def _eye_model():
    """The open/closed-eye classifier, or None when its file isn't downloaded (python -m studio get-ai-models)."""
    global _eye
    if _eye is None:
        if not EYE_MODEL.exists():
            return None
        import onnxruntime as ort

        so = ort.SessionOptions()
        so.intra_op_num_threads = 1
        _eye = ort.InferenceSession(str(EYE_MODEL), so, providers=["CPUExecutionProvider"])
    return _eye


def blur_score(gray: np.ndarray) -> Optional[float]:
    """Crete-Roffet blur of the most detailed sixth of a 6x6 grid of tiles. None for a featureless picture."""
    import cv2

    g = gray.astype(np.float32)
    dfv, dfh = np.abs(np.diff(g, axis=0)), np.abs(np.diff(g, axis=1))
    dbv = np.abs(np.diff(cv2.blur(g, (1, 9)), axis=0))
    dbh = np.abs(np.diff(cv2.blur(g, (9, 1)), axis=1))
    vv, vh = np.maximum(0, dfv - dbv), np.maximum(0, dfh - dbh)
    h, w = g.shape
    tiles = []
    for ty in range(6):
        for tx in range(6):
            ys, ye, xs, xe = ty * h // 6, (ty + 1) * h // 6, tx * w // 6, (tx + 1) * w // 6
            sfv, sfh = dfv[ys:ye - 1, xs:xe].sum(), dfh[ys:ye, xs:xe - 1].sum()
            if min(sfv, sfh) < 0.15 * (ye - ys) * (xe - xs):
                continue  # too flat to say anything
            tiles.append(max((sfv - vv[ys:ye - 1, xs:xe].sum()) / sfv, (sfh - vh[ys:ye, xs:xe - 1].sum()) / sfh))
    if not tiles:
        return None
    tiles.sort()
    return float(np.mean(tiles[:max(1, len(tiles) // 6)]))


def dhash(gray: np.ndarray) -> int:
    """64-bit difference hash, as a signed integer (SQLite's INTEGER)."""
    import cv2

    s = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    v = 0
    for b in (s[:, 1:] > s[:, :-1]).flatten():
        v = (v << 1) | int(b)
    return v - (1 << 64) if v >= 1 << 63 else v


def eyes_shut(rgb: np.ndarray) -> Optional[int]:
    """How many of the main faces have their eyes shut (or are looking down). None without the classifier."""
    import cv2

    model = _eye_model()
    if model is None:
        return None
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    det = _detector()
    det.setInputSize((w, h))
    _, found = det.detect(bgr)
    if found is None:
        return 0
    min_w = MIN_FACE * max(w, h)
    big = max(float(f[2]) for f in found)
    shut = 0
    for f in found:
        x, y, fw, fh = (float(v) for v in f[:4])
        if fw < min_w or fw < MAIN_FACE * big:
            continue
        if x < 0 or y < 0 or x + fw > w or y + fh > h:
            continue  # cut off by the edge of the photo: can't judge
        (rx, ry), (lx, ly) = f[4:6], f[6:8]
        r = max(4, int(np.hypot(lx - rx, ly - ry) * EYE_CROP / 2))
        open_ = []
        for ex, ey in ((rx, ry), (lx, ly)):
            roi = bgr[max(0, int(ey) - r):int(ey) + r, max(0, int(ex) - r):int(ex) + r]
            if roi.size == 0:
                continue
            x32 = (cv2.resize(roi, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32) - 127.0) / 255.0
            # this export's second output is "open" (its documentation says the reverse; checked on real faces)
            open_.append(float(model.run(None, {"input.1": x32.transpose(2, 0, 1)[None]})[0].reshape(-1)[1]))
        if open_ and max(open_) < EYES_SHUT:
            shut += 1
    return shut


def analyze(im) -> dict:
    """{blur, dhash, eyes} for a PIL RGB image (any size: it is scaled here)."""
    import cv2
    from PIL import Image

    big = im.copy()
    big.thumbnail((ANALYZE_SIDE, ANALYZE_SIDE), Image.BILINEAR)
    rgb = np.asarray(big.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    s = BLUR_SIDE / max(gray.shape)
    small = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else gray
    blur = blur_score(small)
    try:
        eyes = eyes_shut(rgb)
    except Exception:  # never let a hint fail the photo
        eyes = None
    return {"blur": -1.0 if blur is None else round(blur, 4), "dhash": dhash(small), "eyes": eyes}


def analyze_file(path: str) -> dict:
    """For photos processed before culling existed: from the web copy."""
    from PIL import Image

    with Image.open(path) as im:
        return analyze(im.convert("RGB"))


def save(c, photo_id: int, r: Optional[dict]) -> None:
    """Store the measures (c: a cursor in the caller's transaction). r=None marks a photo that couldn't be read."""
    r = r or {"blur": -1.0, "dhash": None, "eyes": None}
    c.execute("UPDATE photos SET blur=?, dhash=?, eyes_shut=? WHERE id=?", (r["blur"], r["dhash"], r["eyes"], photo_id))


# ---------------------------------------------------------------------------------------------- the dashboard
def _bits(x: np.ndarray) -> np.ndarray:
    """Set bits per uint64 (numpy 2 has bitwise_count; older numpy counts the bytes)."""
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x)
    return np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1)


def similar(event_id: int) -> dict:
    """{photo id: (group number, best in group)} for photos that have a near-duplicate.

    Photos are compared with their next NEIGHBOURS by name (cameras and phones number bursts consecutively).
    Edited copies are left out: they are meant to look like their original. The best of a group has its eyes
    open, then is the sharpest, then the largest.
    """
    rows = db.q("""SELECT id, dhash, blur, eyes_shut, width, height FROM photos
                   WHERE event_id=? AND status='ready' AND edited=0 AND dhash IS NOT NULL
                   ORDER BY orig_name COLLATE NOCASE, id""", (event_id,))
    n = len(rows)
    if n < 2:
        return {}
    h = np.array([r["dhash"] for r in rows], dtype=np.int64).view(np.uint64)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for k in range(1, min(NEIGHBOURS, n - 1) + 1):
        close = np.flatnonzero(_bits(h[:-k] ^ h[k:]) <= SIMILAR_BITS)
        for i in close:
            a, b = find(int(i)), find(int(i) + k)
            if a != b:
                parent[b] = a
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(rows[i])
    out = {}
    for g, (_, members) in enumerate(sorted(((k, m) for k, m in groups.items() if len(m) > 1), key=lambda x: x[0])):
        best = min(members, key=lambda r: ((r["eyes_shut"] or 0) > 0, r["blur"] if r["blur"] is not None and r["blur"] >= 0 else 1,
                                           -(r["width"] or 0) * (r["height"] or 0)))
        for r in members:
            out[r["id"]] = (g, r["id"] == best["id"])
    return out


def summary(event_id: int) -> dict:
    """Counts for the dashboard's culling chips."""
    r = db.q1(f"""SELECT COUNT(*) n, SUM(blur IS NOT NULL) analyzed, SUM(blur >= {BLURRY}) blurry, SUM(eyes_shut > 0) eyes
                  FROM photos WHERE event_id=? AND status='ready'""", (event_id,))
    sim = similar(event_id)
    return {"photos": r["n"], "analyzed": r["analyzed"] or 0, "blurry": r["blurry"] or 0, "eyes": r["eyes"] or 0,
            "similar": sum(1 for _, best in sim.values() if not best),
            "groups": len({g for g, _ in sim.values()})}
