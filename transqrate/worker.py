"""Transcode workers: claim pending jobs, run the VMAF search when the
profile asks for it, drive ffmpeg with live progress, finalize outputs."""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path

from . import config, db, media, vmaf

logger = logging.getLogger("transqrate.worker")


class JobLog:
    def __init__(self, path: Path):
        self.path = path
        self.fh = open(path, "a", buffering=1, encoding="utf-8", errors="replace")

    def __call__(self, msg: str) -> None:
        self.fh.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + msg + "\n")

    def close(self) -> None:
        try:
            self.fh.close()
        except OSError:
            pass


class Manager:
    def __init__(self):
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._procs: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        n = max(1, int(db.get_settings().get("workers", "1")))
        for i in range(n):
            t = threading.Thread(target=self._loop, name=f"worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)
        logger.info("started %d transcode worker(s)", n)

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for proc in self._procs.values():
                try:
                    proc.terminate()
                except OSError:
                    pass

    def cancel(self, job_id: int) -> bool:
        job = db.get_job(job_id)
        if not job:
            return False
        if job["status"] == "pending":
            db.update_job(job_id, status="cancelled", finished_at=db.now())
            db.set_file_state(job["input_path"], "candidate")
            return True
        if job["status"] in ("analyzing", "running"):
            db.update_job(job_id, status="cancelling")
            with self._lock:
                proc = self._procs.get(job_id)
            if proc:
                try:
                    proc.terminate()
                except OSError:
                    pass
            return True
        return False

    def _register(self, job_id: int, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[job_id] = proc

    def _unregister(self, job_id: int) -> None:
        with self._lock:
            self._procs.pop(job_id, None)

    def _loop(self) -> None:
        while not self._stop.is_set():
            job = db.claim_next_job()
            if not job:
                self._stop.wait(2)
                continue
            try:
                self._process(job)
            except Exception:
                logger.exception("unhandled error in job %s", job["id"])
                db.update_job(job["id"], status="failed", finished_at=db.now(),
                              error="internal error, see application log")
            finally:
                self._unregister(job["id"])

    # ------------------------------------------------------------ pipeline

    def _process(self, job: dict) -> None:
        job_id = job["id"]
        log_path = config.LOG_DIR / f"job_{job_id}.log"
        db.update_job(job_id, log_file=str(log_path))
        log = JobLog(log_path)
        input_path = Path(job["input_path"])
        settings = db.get_settings()
        profile = json.loads(job["profile_json"])
        def cancelled() -> bool:
            return db.job_status(job_id) == "cancelling"

        tmp_out: Path | None = None
        try:
            log(f"job {job_id}: {input_path}")
            log(f"profile: {profile['name']}")
            logger.info("job %d: starting %s (profile: %s)",
                        job_id, input_path.name, profile["name"])
            if not input_path.exists():
                raise media.MediaError("input file no longer exists")

            info = media.ffprobe(input_path)
            if media.is_tagged(info) and not job.get("force"):
                log("file already carries a TRANSQRATE tag - skipping"
                    " (re-queue it from the source details to force)")
                self._finish(job_id, "skipped", input_path,
                             note="already transcoded", file_state="tagged")
                return
            if not media.has_video(info):
                raise media.MediaError("no video stream found")

            duration = media.duration_s(info)
            source = db.query_one("SELECT * FROM sources WHERE id=?", (job["source_id"],)) \
                if job["source_id"] else None
            final_out = self._output_path(input_path, source, profile)
            final_out.parent.mkdir(parents=True, exist_ok=True)
            tmp_out = final_out.parent / f".{final_out.stem}{config.TMP_MARKER}{final_out.suffix}"
            db.update_job(job_id, output_path=str(final_out))

            icq = int(profile.get("icq", 22))
            if profile.get("quality_mode") == "vmaf":
                log("quality mode: VMAF target - searching for matching ICQ")
                result = vmaf.find_icq(input_path, profile, settings, info,
                                       job_id, log, cancel_check=cancelled)
                icq = result.icq
                db.update_job(job_id, chosen_icq=icq, vmaf_score=round(result.vmaf, 2))
                logger.info("job %d: vmaf search done - ICQ %d, predicted VMAF %.2f",
                            job_id, icq, result.vmaf)
            else:
                log(f"quality mode: fixed ICQ {icq}")
                db.update_job(job_id, chosen_icq=icq)

            if cancelled():
                raise vmaf.Cancelled()

            db.update_job(job_id, status="running", progress=0)
            log(f"starting full encode at ICQ {icq} -> {final_out}")
            logger.info("job %d: encoding %s at ICQ %d", job_id, input_path.name, icq)
            cmd = media.build_command(input_path, tmp_out, profile, icq, info, settings)

            last_write = 0.0
            last_applog = time.monotonic()

            def on_progress(pct, fps, speed, eta):
                nonlocal last_write, last_applog
                if time.monotonic() - last_write >= 1.0:
                    last_write = time.monotonic()
                    db.update_job(job_id, progress=round(pct, 1), fps=fps,
                                  speed=speed, eta_s=eta)
                if time.monotonic() - last_applog >= 300:
                    last_applog = time.monotonic()
                    logger.info("job %d: %.1f%% (speed %s, ETA %s s)",
                                job_id, pct, speed, eta)

            rc = media.run_ffmpeg(cmd, log.fh, duration, on_progress,
                                  on_spawn=lambda p: self._register(job_id, p))
            if cancelled() or db.job_status(job_id) == "cancelling":
                raise vmaf.Cancelled()
            if rc != 0:
                raise media.MediaError(f"ffmpeg exited with code {rc} (see job log)")

            size_in = input_path.stat().st_size
            size_out = tmp_out.stat().st_size
            if size_out <= 0:
                raise media.MediaError("output file is empty")

            if settings.get("skip_if_larger", "1") == "1" and size_out >= size_in:
                log(f"output ({size_out} B) is not smaller than input ({size_in} B) - "
                    "keeping original")
                tmp_out.unlink(missing_ok=True)
                note = "output would be larger than input"
                # processed files always end up in the output folder, even
                # untranscoded ones - move the original there
                if source and source["output_path"]:
                    dest = self._output_path(input_path, source, profile).with_suffix(
                        input_path.suffix)
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(input_path), str(dest))
                        note = "output would be larger - original moved to output"
                        log(f"moved original to {dest}")
                    except OSError as exc:
                        log(f"could not move original to output: {exc}")
                self._finish(job_id, "skipped", input_path, size_in=size_in, note=note)
                return

            os.replace(tmp_out, final_out)
            in_place = not (source and source["output_path"])
            if in_place and final_out != input_path:
                input_path.unlink()
                log(f"replaced original with {final_out.name}")

            db.update_job(job_id, status="done", progress=100, finished_at=db.now(),
                          size_in=size_in, size_out=size_out, eta_s=0)
            db.set_file_state(str(input_path), "done", job_id)
            saved = size_in - size_out
            log(f"done: {size_in} -> {size_out} bytes "
                f"(saved {saved} B, {saved / size_in * 100:.1f}%)")
            logger.info("job %d: done - %s, saved %.1f%% (%d -> %d bytes)",
                        job_id, final_out.name, saved / size_in * 100, size_in, size_out)
        except vmaf.Cancelled:
            log("job cancelled")
            logger.info("job %d: cancelled", job_id)
            db.update_job(job_id, status="cancelled", finished_at=db.now())
            db.set_file_state(str(input_path), "candidate")
        except Exception as exc:
            log("ERROR: " + str(exc))
            log(traceback.format_exc())
            logger.error("job %d: failed - %s", job_id, exc)
            db.update_job(job_id, status="failed", finished_at=db.now(),
                          error=str(exc)[:1000])
            db.set_file_state(str(input_path), "failed", job_id)
        finally:
            if tmp_out:
                tmp_out.unlink(missing_ok=True)
            log.close()

    def _finish(self, job_id: int, status: str, input_path: Path,
                size_in: int | None = None, note: str = "",
                file_state: str = "skipped") -> None:
        db.update_job(job_id, status=status, finished_at=db.now(),
                      size_in=size_in, error=note or None)
        db.set_file_state(str(input_path), file_state, job_id)

    @staticmethod
    def _output_path(input_path: Path, source: dict | None, profile: dict) -> Path:
        ext = "." + profile.get("container", "mkv")
        if source and source["output_path"]:
            try:
                rel = input_path.relative_to(source["path"])
            except ValueError:
                rel = Path(input_path.name)
            return (Path(source["output_path"]) / rel).with_suffix(ext)
        return input_path.with_suffix(ext)
