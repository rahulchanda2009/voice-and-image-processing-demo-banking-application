from __future__ import annotations

import io
import time
from pathlib import Path

import numpy as np

from app.config import settings

try:
    import librosa
except Exception as exc:  # pragma: no cover
    librosa = None
    _import_error = exc
else:
    _import_error = None


def _load_mono_16k(data: bytes, filename: str = "audio.wav") -> tuple[np.ndarray, int]:
    if librosa is None:
        raise RuntimeError(f"librosa is not available: {_import_error}")
    path_like = io.BytesIO(data)
    y, sr = librosa.load(path_like, sr=16000, mono=True)
    if y.size == 0:
        raise ValueError(f"Empty audio in {filename}")
    y, _ = librosa.effects.trim(y, top_db=25)
    if y.size < 16000 * 0.7:
        raise ValueError("Recording too short or too quiet. Speak for about 4 seconds.")
    return y, 16000


def embed_bytes(data: bytes, filename: str = "audio.wav") -> np.ndarray:
    y, sr = _load_mono_16k(data, filename)
    rms = float(np.sqrt(np.mean(y**2)))
    if rms < 0.005:
        raise ValueError("Could not hear speech. Move closer to the microphone.")
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=24, n_fft=512, hop_length=160)
    d1 = librosa.feature.delta(mfcc)
    d2 = librosa.feature.delta(mfcc, order=2)
    feat = np.vstack([mfcc, d1, d2])
    vec = np.concatenate([feat.mean(axis=1), feat.std(axis=1)]).astype(np.float32)
    norm = np.linalg.norm(vec) + 1e-9
    return vec / norm


def average_embeddings(vectors: list[np.ndarray]) -> np.ndarray:
    stacked = np.vstack(vectors)
    mean = stacked.mean(axis=0)
    return mean / (np.linalg.norm(mean) + 1e-9)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def save_wav_copy(customer_id: str, index: int, data: bytes) -> Path:
    folder = settings.voices_dir / customer_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"enroll_{index}.wav"
    path.write_bytes(data)
    return path


def verify_score(enrolled: list[float], sample: np.ndarray) -> float:
    return cosine(np.array(enrolled, dtype=np.float32), sample)


def timed_embed(data: bytes, filename: str) -> tuple[np.ndarray, float]:
    t0 = time.perf_counter()
    vec = embed_bytes(data, filename)
    return vec, time.perf_counter() - t0