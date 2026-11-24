from __future__ import annotations

import io
import time
from typing import Any

import numpy as np
from PIL import Image

cv2 = None
try:
    import cv2 as _cv2

    if hasattr(_cv2, "CascadeClassifier") and hasattr(_cv2, "imdecode"):
        cv2 = _cv2
except Exception:
    cv2 = None

_CASCADE = None
if cv2 is not None:
    try:
        xml = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        cascade = cv2.CascadeClassifier(xml)
        if not cascade.empty():
            _CASCADE = cascade
    except Exception:
        _CASCADE = None

# Photo area on the generated specimen card (720x420 canvas, photo drawn at x 28-200, y 92-304).
# A small inset avoids the card border.
SPECIMEN_PHOTO_BOX = (30 / 720, 94 / 420, 198 / 720, 302 / 420)


def backend_name() -> str:
    if _CASCADE is not None:
        return "opencv_haar_hist"
    return "pil_hist"


def _read_rgb(data: bytes) -> np.ndarray:
    if cv2 is not None:
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = Image.open(io.BytesIO(data)).convert("RGB")
    return np.array(img)


def _crop_frac(rgb: np.ndarray, box: tuple[float, float, float, float]) -> np.ndarray:
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)
    crop = rgb[max(0, y0):min(h, y1), max(0, x0):min(w, x1)]
    return crop if crop.size else rgb


def _haar_face(rgb: np.ndarray) -> np.ndarray | None:
    if _CASCADE is None:
        return None
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    faces = _CASCADE.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(60, 60))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    pad = int(0.1 * w)
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(rgb.shape[1], x + w + pad), min(rgb.shape[0], y + h + pad)
    crop = rgb[y0:y1, x0:x1]
    return crop if crop.size else None


def _guess_face(rgb: np.ndarray, prefer_left: bool) -> np.ndarray:
    h, w = rgb.shape[:2]
    if prefer_left:
        x0, x1 = int(w * 0.03), int(w * 0.40)
        y0, y1 = int(h * 0.16), int(h * 0.86)
    else:
        side = min(h, w)
        x0 = (w - side) // 2 + int(side * 0.12)
        y0 = (h - side) // 2 + int(side * 0.08)
        x1 = x0 + int(side * 0.76)
        y1 = y0 + int(side * 0.84)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, max(x0 + 8, x1)), min(h, max(y0 + 8, y1))
    crop = rgb[y0:y1, x0:x1]
    return crop if crop.size else rgb


def detect_face_rgb(rgb: np.ndarray, prefer_left: bool = False) -> np.ndarray:
    found = _haar_face(rgb)
    if found is not None:
        return found
    return _guess_face(rgb, prefer_left=prefer_left)


def _hist_vec(rgb: np.ndarray) -> np.ndarray:
    im = Image.fromarray(rgb).resize((64, 64))
    arr = np.array(im)
    parts = [np.histogram(arr[:, :, c], bins=16, range=(0, 256), density=True)[0] for c in range(3)]
    gray = np.mean(arr, axis=2)
    parts.append(np.histogram(gray, bins=16, range=(0, 256), density=True)[0])
    vec = np.concatenate(parts).astype(np.float32)
    vec /= np.linalg.norm(vec) + 1e-9
    return vec


def match_faces(card_bytes: bytes, live_bytes: bytes, specimen: bool = False) -> dict[str, Any]:
    t0 = time.perf_counter()
    card = _read_rgb(card_bytes)
    live = _read_rgb(live_bytes)
    if specimen:
        # The specimen card photo is the full camera frame, so compare like with like.
        card_face = _crop_frac(card, SPECIMEN_PHOTO_BOX)
        live_face = live
        backend = backend_name() + "_specimen_photo"
    else:
        card_face = detect_face_rgb(card, prefer_left=True)
        live_face = detect_face_rgb(live, prefer_left=False)
        backend = backend_name()
    score = float(np.dot(_hist_vec(card_face), _hist_vec(live_face)))
    return {
        "backend": backend,
        "card_face_found": True,
        "live_face_found": True,
        "score": score,
        "duration_s": time.perf_counter() - t0,
    }