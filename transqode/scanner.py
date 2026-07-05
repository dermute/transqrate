"""Folder scanning: on-demand ("start now") and periodic watching.

Watched folders queue a file only once its size/mtime is unchanged between
two consecutive scans, so files still being copied are left alone."""

import logging
import threading
from pathlib import Path

from . import config, db, media

logger = logging.getLogger("transqode.scanner")

SKIP_STATES = {"queued", "done", "tagged", "skipped"}


def scan_source(source: dict, require_stable: bool = False) -> dict:
    """Walk one source folder and queue eligible files. Returns counters."""
    settings = db.get_settings()
    exts = {"." + e.strip().lower().lstrip(".")
            for e in settings.get("extensions", "").split(",") if e.strip()}
    min_bytes = int(float(settings.get("min_file_mb", "10")) * 1024 * 1024)
    profile = db.query_one("SELECT * FROM profiles WHERE id=?", (source["profile_id"],))
    root = Path(source["path"])
    counters = {"queued": 0, "skipped": 0, "waiting": 0, "errors": 0}
    if not profile or not root.is_dir():
        counters["errors"] += 1
        logger.warning("scan aborted for source %s: missing profile or folder", source["path"])
        return counters

    for path in sorted(root.rglob("*")):
        try:
            if not path.is_file() or path.suffix.lower() not in exts:
                continue
            if path.name.startswith(".") or config.TMP_MARKER in path.name:
                continue
            st = path.stat()
            if st.st_size < min_bytes:
                continue
            known = db.query_one("SELECT * FROM files WHERE path=?", (str(path),))
            unchanged = known and known["size"] == st.st_size and known["mtime"] == st.st_mtime
            if unchanged and known["state"] in SKIP_STATES:
                counters["skipped"] += 1
                continue
            if unchanged and known["state"] == "failed":
                counters["skipped"] += 1  # retry manually from the dashboard
                continue
            if require_stable and not unchanged:
                # first sighting (or still growing): remember it, queue next round
                db.set_file_state(str(path), "candidate", size=st.st_size, mtime=st.st_mtime)
                counters["waiting"] += 1
                continue
            # probe so already-tagged files (e.g. our own outputs) are not re-queued
            try:
                info = media.ffprobe(path)
            except media.MediaError:
                db.set_file_state(str(path), "failed", size=st.st_size, mtime=st.st_mtime)
                counters["errors"] += 1
                continue
            if media.is_tagged(info) or not media.has_video(info):
                db.set_file_state(str(path), "tagged", size=st.st_size, mtime=st.st_mtime)
                counters["skipped"] += 1
                continue
            if db.create_job(source, profile, str(path)):
                db.set_file_state(str(path), "queued", size=st.st_size, mtime=st.st_mtime)
                counters["queued"] += 1
        except OSError as exc:
            logger.warning("scan error on %s: %s", path, exc)
            counters["errors"] += 1
    logger.info("scanned %s: %s", source["path"], counters)
    return counters


ACTIVE_STATES = ("pending", "analyzing", "running", "cancelling")


def media_files(source: dict) -> list:
    """All media files in a source folder (same filters as a scan)."""
    settings = db.get_settings()
    exts = {"." + e.strip().lower().lstrip(".")
            for e in settings.get("extensions", "").split(",") if e.strip()}
    min_bytes = int(float(settings.get("min_file_mb", "10")) * 1024 * 1024)
    root = Path(source["path"])
    files = []
    for path in sorted(root.rglob("*")):
        try:
            if (not path.is_file() or path.suffix.lower() not in exts
                    or path.name.startswith(".") or config.TMP_MARKER in path.name):
                continue
            st = path.stat()
            if st.st_size < min_bytes:
                continue
            files.append((path, st))
        except OSError:
            continue
    return files


def list_files(source: dict) -> list[dict]:
    """Per-file transcode state for the source details view."""
    root = Path(source["path"])
    out = []
    for path, st in media_files(source):
        rec = db.query_one("SELECT state FROM files WHERE path=?", (str(path),))
        job = db.query_one(
            "SELECT id, status, size_in, size_out FROM jobs"
            " WHERE input_path=? ORDER BY id DESC LIMIT 1", (str(path),))
        if job and job["status"] in ACTIVE_STATES:
            state = job["status"]
        elif rec:
            state = rec["state"]
        else:
            state = "new"
        saved = None
        if job and job["size_in"] and job["size_out"]:
            saved = job["size_in"] - job["size_out"]
        out.append({
            "path": str(path),
            "rel": str(path.relative_to(root)),
            "size": st.st_size,
            "state": state,
            "job_id": job["id"] if job else None,
            "saved": saved,
        })
    return out


class Watcher:
    def __init__(self):
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = max(30, int(db.get_settings().get("scan_interval_s", "300")))
            for source in db.query("SELECT * FROM sources WHERE watch=1 AND enabled=1"):
                if self._stop.is_set():
                    return
                try:
                    scan_source(source, require_stable=True)
                except Exception:
                    logger.exception("watch scan failed for %s", source["path"])
            self._stop.wait(interval)
