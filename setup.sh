#!/bin/zsh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Project: $(pwd)"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required."
  exit 1
fi

python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt

python - <<'PY'
import importlib.util, subprocess, sys
if importlib.util.find_spec("face_recognition") is None:
    print("Optional: installing face_recognition (needs Xcode Command Line Tools).")
    print("If this fails, the OpenCV fallback matcher will be used.")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "face-recognition"])
    except subprocess.CalledProcessError:
        print("face_recognition install failed. Continuing with OpenCV fallback.")
PY

mkdir -p data/voices data/faces
python -m app.seed_db

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example. Add GEMINI_API_KEY before running."
else
  chmod 600 .env || true
  echo ".env already exists. Merge any missing keys from .env.example."
fi

echo
echo "Setup done."
echo "1. Confirm Hindi voice Lekha: System Settings → Accessibility → Spoken Content → System Voice → Manage Voices → Hindi → Lekha"
echo "2. Start the app: ./run.sh"
echo "3. Open http://127.0.0.1:8000"