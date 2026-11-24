from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


class Settings:
    llm_provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash").strip()

    lmstudio_base_url = os.getenv("LMSTUDIO_BASE_URL", "http://localhost:1234/v1").strip()
    lmstudio_api_key = os.getenv("LMSTUDIO_API_KEY", "lm-studio").strip()
    lmstudio_model = os.getenv("LMSTUDIO_MODEL", "").strip()

    galileo_api_key = os.getenv("GALILEO_API_KEY", "").strip()
    galileo_project = os.getenv("GALILEO_PROJECT", "rahul-project").strip()
    galileo_log_stream = os.getenv("GALILEO_LOG_STREAM", "sahayak-bank-demo").strip()
    galileo_console_url = os.getenv("GALILEO_CONSOLE_URL", "https://console.multitenant.galileocloud.io").strip()
    galileo_log_stream_url = os.getenv("GALILEO_LOG_STREAM_URL", "").strip()

    voice_match_threshold = _float("VOICE_MATCH_THRESHOLD", 0.78)
    face_match_threshold = _float("FACE_MATCH_THRESHOLD", 0.55)
    liveness_min_motion = _float("LIVENESS_MIN_MOTION", 5.0)
    show_scores = _bool("SHOW_SCORES", True)

    passphrase_hi = "मेरी आवाज़ ही मेरा पासवर्ड है"
    bank_name = "Sahayak Bank"

    data_dir = ROOT / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bank.db"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def faces_dir(self) -> Path:
        return self.data_dir / "faces"

    @property
    def galileo_enabled(self) -> bool:
        return bool(self.galileo_api_key)


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.voices_dir.mkdir(parents=True, exist_ok=True)
settings.faces_dir.mkdir(parents=True, exist_ok=True)