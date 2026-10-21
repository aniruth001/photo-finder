"""Face detection + embedding. Used by the indexer, the studio's photo workers and the selfie search.

Two engines with the same interface, faces(im, ...) -> [(area, unit-length float32 vector)]:

  sface  YuNet detector + SFace recognizer (OpenCV). The default. Much better at telling people apart: on a real
         wedding the gap between "same person" and "someone else" was ~0.2 wide, against ~0.02 for dlib.
  dlib   HOG (or CNN) detector + dlib ResNet. The original engine, kept so older indexes keep working.

Vectors of the two engines are not comparable: an index and its selfies must use the same one.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:  # optional: iPhone .heic photos
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass

MODELS = Path(__file__).parent / "models"
DEFAULT_MODEL = "sface"

# match thresholds (Euclidean distance between vectors) per engine: strict, balanced, relaxed
PRESETS = {
    "sface": {"strict": 0.92, "balanced": 0.95, "relaxed": 1.0},
    "dlib": {"strict": 0.45, "balanced": 0.5, "relaxed": 0.56},
}
LIMITS = {"sface": (0.8, 1.15), "dlib": (0.35, 0.65)}  # allowed tolerance range per engine


def open_image(source) -> Image.Image:
    """Open a path or file object, apply the EXIF rotation, return RGB."""
    im = Image.open(source)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def _shrink(im: Image.Image, side: int) -> Image.Image:
    w, h = im.size
    scale = min(1.0, side / max(w, h))
    return im.resize((round(w * scale), round(h * scale)), Image.LANCZOS) if scale < 1.0 else im


class SFaceEngine:
    """YuNet finds faces and 5 landmarks; SFace turns the aligned 112x112 face into a 128-d vector.

    Each vector is the sum of the face and its mirror image (then normalised), so a selfie saved mirrored
    (many Android phones do this) gives the same vector as an unmirrored one.
    """

    model = "sface"

    def __init__(self, detector: str = "", score: float = 0.75):
        import cv2  # imported here so the web process doesn't load OpenCV until a selfie arrives

        self._cv2 = cv2
        self._det = cv2.FaceDetectorYN.create(str(MODELS / "face_detection_yunet_2023mar.onnx"), "", (320, 320),
                                              score, 0.3, 5000)
        self._rec = cv2.FaceRecognizerSF.create(str(MODELS / "face_recognition_sface_2021dec.onnx"), "")

    def _detect(self, bgr):
        self._det.setInputSize((bgr.shape[1], bgr.shape[0]))
        _, found = self._det.detect(bgr)
        return [] if found is None else list(found)

    def faces(self, im: Image.Image, detect_side: int = 2000, min_face: int = 40, jitters: int = 1):
        """Return [(area, 128-d float32 unit vector)] for every face wider than `min_face` px (at `detect_side`)."""
        small = _shrink(im, detect_side)
        bgr = self._cv2.cvtColor(np.asarray(small), self._cv2.COLOR_RGB2BGR)
        found = self._detect(bgr)
        if not found and max(small.size) > 800:  # a face filling the frame (close selfie) is found more easily smaller
            small = _shrink(im, 640)
            bgr = self._cv2.cvtColor(np.asarray(small), self._cv2.COLOR_RGB2BGR)
            found = self._detect(bgr)
            min_face = min_face * 640 / detect_side
        out = []
        for f in found:
            if f[2] < min_face:
                continue
            crop = self._rec.alignCrop(bgr, f)
            v = self._rec.feature(crop).flatten() + self._rec.feature(np.ascontiguousarray(crop[:, ::-1])).flatten()
            n = float(np.linalg.norm(v))
            if n > 0:
                out.append((float(f[2] * f[3]), (v / n).astype(np.float32)))
        return out


class DlibEngine:
    """detect -> align -> 128-d embedding. Two faces of the same person are ~0.4 apart or less."""

    model = "dlib"

    def __init__(self, detector: str = "hog"):
        import dlib

        self.detector = detector
        if detector == "cnn":  # more accurate on side/small faces, much slower without a GPU
            self._cnn = dlib.cnn_face_detection_model_v1(str(MODELS / "mmod_human_face_detector.dat"))
        else:
            self._hog = dlib.get_frontal_face_detector()
        self._landmarks = dlib.shape_predictor(str(MODELS / "shape_predictor_5_face_landmarks.dat"))
        self._embed = dlib.face_recognition_model_v1(str(MODELS / "dlib_face_recognition_resnet_model_v1.dat"))

    def faces(self, im: Image.Image, detect_side: int = 2000, min_face: int = 40, jitters: int = 1):
        """Return [(area, 128-d float32 vector)] for every face wider than `min_face` px.

        The image is shrunk so its long side is `detect_side` before detection (speed);
        a bigger value finds smaller faces in group shots but is slower.
        """
        im = _shrink(im, detect_side)
        arr = np.ascontiguousarray(np.asarray(im))
        if self.detector == "cnn":
            rects = [d.rect for d in self._cnn(arr, 1)]
        else:
            rects = self._hog(arr, 1)  # 1 = upsample once, finds faces down to ~40 px
        out = []
        for r in rects:
            if r.width() < min_face:
                continue
            shape = self._landmarks(arr, r)
            vec = np.array(self._embed.compute_face_descriptor(arr, shape, jitters), dtype=np.float32)
            out.append((r.width() * r.height(), vec))
        return out


FaceEngine = DlibEngine  # the old name


def make_engine(model: str = DEFAULT_MODEL, detector: str = "hog"):
    if model == "dlib":
        return DlibEngine(detector)
    if model == "sface":
        return SFaceEngine()
    raise ValueError(f"unknown face model {model!r} (use sface or dlib)")
