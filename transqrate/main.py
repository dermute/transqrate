"""transQrate HTTP API + web UI."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, media, scanner, worker

logger = logging.getLogger("transqrate")
manager = worker.Manager()
watcher = scanner.Watcher()

ACTIVE = ("pending", "analyzing", "running", "cancelling")


def setup_logging() -> None:
    config.ensure_dirs()
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(config.APP_LOG, encoding="utf-8")],
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging()
    db.init()
    manager.start()
    watcher.start()
    logger.info("transqrate ready")
    yield
    watcher.stop()
    manager.stop()


app = FastAPI(title="transQrate", version="latest", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(config.STATIC_DIR / "index.html")


# ------------------------------------------------------------------ models

class ProfileIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    video_codec: str = "av1_qsv"
    preset: str = "veryslow"
    quality_mode: str = Field("icq", pattern="^(icq|vmaf)$")
    icq: int = Field(22, ge=1, le=51)
    vmaf_target: float = Field(95.0, ge=1, le=100)
    audio_codec: str = Field("libopus", pattern="^(libopus|copy)$")
    audio_kbps_per_channel: int = Field(64, ge=16, le=512)
    audio_max_channels: int = Field(0, ge=0, le=8)  # 0 = keep all
    max_resolution: str = Field("source", pattern="^(source|480p|720p|1080p|2160p)$")
    bit_depth: str = Field("source", pattern="^(source|8)$")
    container: str = Field("mkv", pattern="^(mkv|mp4)$")
    extra_video_args: str = ""


class SourceIn(BaseModel):
    path: str = Field(min_length=1)
    profile_id: int
    output_path: str | None = None
    watch: bool = False
    enabled: bool = True


# --------------------------------------------------------------- dashboard

@app.get("/api/status")
def status():
    totals = db.query_one(
        "SELECT COUNT(*) AS done, COALESCE(SUM(size_in),0) AS bytes_in,"
        " COALESCE(SUM(size_out),0) AS bytes_out FROM jobs WHERE status='done'")
    counts = {r["status"]: r["n"] for r in
              db.query("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
    return {"totals": totals, "counts": counts}


@app.get("/api/dashboard")
def dashboard():
    active = db.query(
        "SELECT j.*, s.path AS source_path FROM jobs j"
        " LEFT JOIN sources s ON s.id = j.source_id"
        " WHERE j.status IN ('analyzing','running','cancelling') ORDER BY j.started_at")
    pending = db.query(
        "SELECT id, input_path, profile_name, created_at FROM jobs"
        " WHERE status='pending' ORDER BY id LIMIT 25")
    pending_total = db.query_one(
        "SELECT COUNT(*) AS n FROM jobs WHERE status='pending'")["n"]
    recent = db.query(
        "SELECT * FROM jobs WHERE status IN ('done','failed','cancelled','skipped')"
        " ORDER BY finished_at DESC, id DESC LIMIT 30")
    return {"active": active, "pending": pending, "pending_total": pending_total,
            "recent": recent, "status": status()}


# -------------------------------------------------------------------- jobs

@app.get("/api/jobs")
def list_jobs(status_filter: str | None = None, limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 500))
    if status_filter:
        return db.query("SELECT * FROM jobs WHERE status=? ORDER BY id DESC LIMIT ? OFFSET ?",
                        (status_filter, limit, offset))
    return db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: int, tail: int = 500):
    job = db.get_job(job_id)
    if not job or not job["log_file"]:
        raise HTTPException(404, "no log for this job")
    return _tail_file(Path(job["log_file"]), tail)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: int):
    if not manager.cancel(job_id):
        raise HTTPException(409, "job is not cancellable")
    return {"ok": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if job["status"] not in ("failed", "cancelled", "skipped"):
        raise HTTPException(409, "only failed/cancelled/skipped jobs can be retried")
    db.update_job(job_id, status="pending", progress=0, error=None, fps=None,
                  speed=None, eta_s=None, started_at=None, finished_at=None,
                  size_in=None, size_out=None, chosen_icq=None, vmaf_score=None)
    db.set_file_state(job["input_path"], "queued", job_id)
    return {"ok": True}


# ---------------------------------------------------------------- profiles

@app.get("/api/profiles")
def list_profiles():
    settings = db.get_settings()
    profiles = db.query("SELECT * FROM profiles ORDER BY name")
    for p in profiles:
        p["command"] = media.command_preview(p, settings)
    return profiles


@app.post("/api/profiles/preview")
def preview_profile(p: ProfileIn):
    """Render the ffmpeg command line a (possibly unsaved) profile would run."""
    return {"command": media.command_preview(p.model_dump(), db.get_settings())}


@app.post("/api/profiles")
def create_profile(p: ProfileIn):
    if db.query_one("SELECT id FROM profiles WHERE name=?", (p.name,)):
        raise HTTPException(409, "a profile with this name already exists")
    ts = db.now()
    pid = db.execute(
        "INSERT INTO profiles(name, video_codec, preset, quality_mode, icq, vmaf_target,"
        " audio_codec, audio_kbps_per_channel, audio_max_channels, max_resolution,"
        " bit_depth, container, extra_video_args, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p.name, p.video_codec, p.preset, p.quality_mode, p.icq, p.vmaf_target,
         p.audio_codec, p.audio_kbps_per_channel, p.audio_max_channels,
         p.max_resolution, p.bit_depth, p.container, p.extra_video_args, ts, ts))
    return db.query_one("SELECT * FROM profiles WHERE id=?", (pid,))


@app.put("/api/profiles/{profile_id}")
def update_profile(profile_id: int, p: ProfileIn):
    if not db.query_one("SELECT id FROM profiles WHERE id=?", (profile_id,)):
        raise HTTPException(404, "profile not found")
    clash = db.query_one("SELECT id FROM profiles WHERE name=? AND id<>?", (p.name, profile_id))
    if clash:
        raise HTTPException(409, "a profile with this name already exists")
    db.execute(
        "UPDATE profiles SET name=?, video_codec=?, preset=?, quality_mode=?, icq=?,"
        " vmaf_target=?, audio_codec=?, audio_kbps_per_channel=?, audio_max_channels=?,"
        " max_resolution=?, bit_depth=?, container=?, extra_video_args=?, updated_at=?"
        " WHERE id=?",
        (p.name, p.video_codec, p.preset, p.quality_mode, p.icq, p.vmaf_target,
         p.audio_codec, p.audio_kbps_per_channel, p.audio_max_channels,
         p.max_resolution, p.bit_depth, p.container, p.extra_video_args,
         db.now(), profile_id))
    return db.query_one("SELECT * FROM profiles WHERE id=?", (profile_id,))


@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int):
    if db.query_one("SELECT id FROM sources WHERE profile_id=?", (profile_id,)):
        raise HTTPException(409, "profile is used by a source folder")
    db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
    return {"ok": True}


# ----------------------------------------------------------------- sources

@app.get("/api/sources")
def list_sources():
    sources = db.query(
        "SELECT s.*, p.name AS profile_name FROM sources s"
        " JOIN profiles p ON p.id = s.profile_id ORDER BY s.path")
    for s in sources:
        stats = db.query_one(
            "SELECT COUNT(*) AS done, COALESCE(SUM(size_in-size_out),0) AS saved"
            " FROM jobs WHERE source_id=? AND status='done'", (s["id"],))
        active = db.query_one(
            "SELECT COUNT(*) AS n FROM jobs WHERE source_id=? AND status IN"
            " ('pending','analyzing','running','cancelling')", (s["id"],))
        s["stats"] = {**stats, "active": active["n"]}
    return sources


@app.post("/api/sources")
def create_source(s: SourceIn):
    if not Path(s.path).is_dir():
        raise HTTPException(400, f"folder does not exist in the container: {s.path}")
    if s.output_path and not Path(s.output_path).is_dir():
        raise HTTPException(400, f"output folder does not exist: {s.output_path}")
    if not db.query_one("SELECT id FROM profiles WHERE id=?", (s.profile_id,)):
        raise HTTPException(400, "unknown profile")
    if db.query_one("SELECT id FROM sources WHERE path=?", (s.path,)):
        raise HTTPException(409, "this folder is already configured")
    sid = db.execute(
        "INSERT INTO sources(path, profile_id, output_path, watch, enabled, created_at)"
        " VALUES(?,?,?,?,?,?)",
        (s.path, s.profile_id, s.output_path or None, int(s.watch), int(s.enabled), db.now()))
    return db.query_one("SELECT * FROM sources WHERE id=?", (sid,))


@app.put("/api/sources/{source_id}")
def update_source(source_id: int, s: SourceIn):
    if not db.query_one("SELECT id FROM sources WHERE id=?", (source_id,)):
        raise HTTPException(404, "source not found")
    if not db.query_one("SELECT id FROM profiles WHERE id=?", (s.profile_id,)):
        raise HTTPException(400, "unknown profile")
    db.execute(
        "UPDATE sources SET path=?, profile_id=?, output_path=?, watch=?, enabled=? WHERE id=?",
        (s.path, s.profile_id, s.output_path or None, int(s.watch), int(s.enabled), source_id))
    return db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int):
    db.execute("DELETE FROM sources WHERE id=?", (source_id,))
    return {"ok": True}


@app.post("/api/sources/{source_id}/scan")
def scan_now(source_id: int):
    source = db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))
    if not source:
        raise HTTPException(404, "source not found")
    return scanner.scan_source(source, require_stable=False)


@app.get("/api/sources/{source_id}/files")
def source_files(source_id: int):
    source = db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))
    if not source:
        raise HTTPException(404, "source not found")
    return {"files": scanner.list_files(source)}


class RequeueIn(BaseModel):
    paths: list[str]


@app.post("/api/sources/{source_id}/requeue")
def requeue_files(source_id: int, body: RequeueIn):
    """Reset files for transcoding and queue them, bypassing the
    already-transcoded checks (both DB state and TRANSQRATE tag)."""
    source = db.query_one("SELECT * FROM sources WHERE id=?", (source_id,))
    if not source:
        raise HTTPException(404, "source not found")
    profile = db.query_one("SELECT * FROM profiles WHERE id=?", (source["profile_id"],))
    if not profile:
        raise HTTPException(409, "source has no valid profile")
    root = Path(source["path"]).resolve()
    queued = 0
    for p in body.paths:
        path = Path(p).resolve()
        if root not in path.parents or not path.is_file():
            continue
        if db.create_job(source, profile, str(path), force=True):
            st = path.stat()
            db.set_file_state(str(path), "queued", size=st.st_size, mtime=st.st_mtime)
            queued += 1
    return {"queued": queued}


# ---------------------------------------------------------------- settings

@app.get("/api/settings")
def get_settings():
    return {"values": db.get_settings(), "meta": db.SETTINGS_META,
            "groups": db.SETTINGS_GROUPS}


@app.put("/api/settings")
def put_settings(values: dict):
    db.set_settings(values)
    return {"values": db.get_settings()}


# -------------------------------------------------------------------- logs

@app.get("/api/logs")
def list_logs():
    jobs = db.query(
        "SELECT id, input_path, status, finished_at FROM jobs"
        " WHERE log_file IS NOT NULL ORDER BY id DESC LIMIT 100")
    return {"app_log": str(config.APP_LOG), "jobs": jobs}


@app.get("/api/logs/app", response_class=PlainTextResponse)
def app_log(tail: int = 500):
    return _tail_file(config.APP_LOG, tail)


# ------------------------------------------------------------------ browse

@app.get("/api/browse")
def browse(path: str = "/"):
    p = Path(path).resolve()
    if not p.is_dir():
        raise HTTPException(404, "not a directory")
    try:
        dirs = sorted(c.name for c in p.iterdir()
                      if c.is_dir() and not c.name.startswith("."))
    except PermissionError:
        raise HTTPException(403, "permission denied")
    return {"path": str(p), "parent": str(p.parent) if p != p.parent else None, "dirs": dirs}


MAX_LOG_BYTES = 20 * 1024 * 1024


def _tail_file(path: Path, tail: int) -> str:
    """Last `tail` lines of a log; tail<=0 returns the whole file (capped)."""
    if not path.exists():
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        if tail <= 0:
            fh.seek(max(0, path.stat().st_size - MAX_LOG_BYTES))
            return fh.read()
        return "".join(fh.readlines()[-min(tail, 100_000):])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", config.PORT)))
