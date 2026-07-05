"""ffprobe/ffmpeg helpers: probing, command construction, progress parsing."""

import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config

TAG_KEY = "TRANSQODE"

# channel layouts libopus accepts; aformat converts e.g. 5.1(side) -> 5.1
OPUS_LAYOUTS = "7.1|6.1|5.1|5.0|quad|3.0|stereo|mono"


class MediaError(Exception):
    pass


def ffprobe(path: str | Path) -> dict:
    cmd = [config.FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed for {path}: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout)


def duration_s(info: dict) -> float:
    try:
        return float(info["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        for s in info.get("streams", []):
            try:
                return float(s["duration"])
            except (KeyError, TypeError, ValueError):
                continue
    return 0.0


def is_tagged(info: dict) -> bool:
    tags = info.get("format", {}).get("tags", {}) or {}
    return any(k.upper() == TAG_KEY for k in tags)


def has_video(info: dict) -> bool:
    return any(s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic", 0) != 1
               for s in info.get("streams", []))


def audio_streams(info: dict) -> list[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == "audio"]


def subtitle_streams(info: dict) -> list[dict]:
    return [s for s in info.get("streams", []) if s.get("codec_type") == "subtitle"]


def build_command(input_path: Path, output_path: Path, profile: dict,
                  icq: int, info: dict) -> list[str]:
    """Full transcode command: AV1 QSV video (ICQ mode via -global_quality),
    Opus audio at N kbps per channel keeping the channel count, subs copied,
    and a TRANSQODE tag so finished files are recognizable."""
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "info",
           "-probesize", "100M", "-analyzeduration", "200M",
           "-i", str(input_path),
           "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-dn",
           "-map_metadata", "0", "-map_chapters", "0"]
    if profile.get("container", "mkv") == "mkv":
        cmd += ["-map", "0:t?"]  # font attachments, matroska only

    cmd += video_args(profile, icq)

    astreams = audio_streams(info)
    if profile.get("audio_codec", "libopus") == "copy" or not astreams:
        cmd += ["-c:a", "copy"]
    else:
        kbps = int(profile.get("audio_kbps_per_channel", 64))
        for i, s in enumerate(astreams):
            ch = int(s.get("channels", 2) or 2)
            cmd += [f"-c:a:{i}", profile["audio_codec"],
                    f"-b:a:{i}", f"{ch * kbps}k",
                    f"-filter:a:{i}", f"aformat=channel_layouts={OPUS_LAYOUTS}"]

    for i, s in enumerate(subtitle_streams(info)):
        codec = "srt" if s.get("codec_name") in ("mov_text", "tx3g", "text") else "copy"
        cmd += [f"-c:s:{i}", codec]

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cmd += ["-metadata", f"{TAG_KEY}=1",
            "-metadata", f"{TAG_KEY}_PROFILE={profile.get('name', '')}",
            "-metadata", f"{TAG_KEY}_ICQ={icq}",
            "-metadata", f"{TAG_KEY}_DATE={stamp}",
            "-progress", "pipe:1",
            str(output_path)]
    return cmd


def video_args(profile: dict, icq: int) -> list[str]:
    args = ["-c:v", profile.get("video_codec", "av1_qsv")]
    if profile.get("preset"):
        args += ["-preset:v", profile["preset"]]
    # scoped to :v - unscoped global_quality would also hit audio encoders
    args += ["-global_quality:v", str(icq)]
    extra = (profile.get("extra_video_args") or "").strip()
    if extra:
        args += shlex.split(extra)
    return args


def run_ffmpeg(cmd: list[str], log_fh, duration: float | None = None,
               on_progress=None, on_spawn=None) -> int:
    """Run ffmpeg writing stderr to log_fh; parse -progress key=value pairs
    from stdout and report (pct, fps, speed, eta_s) via on_progress."""
    log_fh.write("$ " + shlex.join(cmd) + "\n")
    log_fh.flush()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log_fh,
                            text=True, bufsize=1)
    if on_spawn:
        on_spawn(proc)
    fps = None
    speed = None
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, val = line.strip().partition("=")
            if key == "fps":
                try:
                    fps = float(val)
                except ValueError:
                    fps = None
            elif key == "speed":
                speed = val.strip()
            elif key == "out_time_us":
                if duration and on_progress:
                    try:
                        pos = int(val) / 1_000_000
                    except ValueError:
                        continue
                    pct = max(0.0, min(100.0, pos / duration * 100))
                    eta = None
                    try:
                        sp = float((speed or "").rstrip("x"))
                        if sp > 0:
                            eta = int((duration - pos) / sp)
                    except ValueError:
                        pass
                    on_progress(pct, fps, speed, eta)
    finally:
        proc.wait()
    return proc.returncode


def run_quiet(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
