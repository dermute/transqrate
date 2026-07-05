import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
DB_PATH = Path(os.environ.get("DB_PATH", str(CONFIG_DIR / "transqode.db")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(CONFIG_DIR / "logs")))
TMP_DIR = Path(os.environ.get("TMP_DIR", str(CONFIG_DIR / "tmp")))
APP_LOG = LOG_DIR / "transqode.log"

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")

PORT = int(os.environ.get("PORT", "8585"))

STATIC_DIR = Path(__file__).parent / "static"

# suffix used for in-flight output files; scanner and workers ignore these
TMP_MARKER = ".tqtmp"


def ensure_dirs() -> None:
    for d in (CONFIG_DIR, LOG_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
