"""ffprobe/ffmpeg helpers: probing, command construction, progress parsing."""

import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config

TAG_KEY = "TRANSQRATE"

# channel layouts libopus accepts (largest first); aformat converts
# e.g. 5.1(side) -> 5.1, and capping the list forces a proper downmix
OPUS_LAYOUT_LADDER = [("7.1", 8), ("6.1", 7), ("5.1", 6), ("5.0", 5),
                      ("quad", 4), ("3.0", 3), ("stereo", 2), ("mono", 1)]

# "Np" labels map to a maximum width; height follows the aspect ratio, so
# scope (21:9) content lands at e.g. 1920x800 for "1080p"
RES_WIDTHS = {"480p": 854, "720p": 1280, "1080p": 1920, "2160p": 3840}


def opus_layouts(max_channels: int = 0) -> str:
    cap = max_channels or 8
    return "|".join(name for name, ch in OPUS_LAYOUT_LADDER if ch <= cap)


def scale_args(profile: dict) -> list[str]:
    """Downscale-only: never upscales smaller sources."""
    width = RES_WIDTHS.get((profile.get("max_resolution") or "source"))
    if not width:
        return []
    return ["-vf", f"scale=w='min({width},iw)':h=-2:flags=lanczos+accurate_rnd"]


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


def qsv_device_args(settings: dict | None) -> list[str]:
    """-qsv_device pins QSV to one render node on multi-GPU hosts."""
    dev = ((settings or {}).get("qsv_device") or "").strip()
    return ["-qsv_device", dev] if dev else []


def build_command(input_path: Path, output_path: Path, profile: dict,
                  icq: int, info: dict, settings: dict | None = None,
                  vmaf_score: float | None = None) -> list[str]:
    """Full transcode command: AV1 QSV video (ICQ mode via -global_quality),
    Opus audio at N kbps per channel keeping the channel count, subs copied,
    and a TRANSQRATE tag so finished files are recognizable."""
    cmd = [config.FFMPEG, "-y", "-hide_banner", "-nostdin", "-loglevel", "info",
           *qsv_device_args(settings),
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
        cap = int(profile.get("audio_max_channels", 0) or 0)
        layouts = opus_layouts(cap)
        for i, s in enumerate(astreams):
            ch = int(s.get("channels", 2) or 2)
            out_ch = min(ch, cap or 8, 8)
            cmd += [f"-c:a:{i}", profile["audio_codec"],
                    f"-b:a:{i}", f"{out_ch * kbps}k",
                    f"-filter:a:{i}", f"aformat=channel_layouts={layouts}"]

    for i, s in enumerate(subtitle_streams(info)):
        codec = "srt" if s.get("codec_name") in ("mov_text", "tx3g", "text") else "copy"
        cmd += [f"-c:s:{i}", codec]

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cmd += ["-metadata", f"{TAG_KEY}=1",
            "-metadata", f"{TAG_KEY}_PROFILE={profile.get('name', '')}",
            "-metadata", f"{TAG_KEY}_ICQ={icq}",
            "-metadata", f"{TAG_KEY}_DATE={stamp}"]
    if vmaf_score is not None:
        cmd += ["-metadata", f"{TAG_KEY}_VMAF={vmaf_score}"]
    cmd += ["-progress", "pipe:1", str(output_path)]
    return cmd


def video_args(profile: dict, icq: int) -> list[str]:
    args = [*scale_args(profile), "-c:v", profile.get("video_codec", "av1_qsv")]
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


# example file used to render a representative command line for a profile:
# video, a 5.1 main track plus a stereo track (to show the per-stream
# bitrate = channels x kbps), and a text subtitle
_EXAMPLE_INFO = {"streams": [
    {"codec_type": "video", "codec_name": "hevc"},
    {"codec_type": "audio", "codec_name": "eac3", "channels": 6},
    {"codec_type": "audio", "codec_name": "aac", "channels": 2},
    {"codec_type": "subtitle", "codec_name": "subrip"},
]}


def command_preview(profile: dict, settings: dict | None = None) -> str:
    icq = profile.get("icq", 22) if profile.get("quality_mode") != "vmaf" else "AUTO"
    cmd = build_command(Path("/media/source/Example.mkv"),
                        Path("/output/.Example.tqtmp." + profile.get("container", "mkv")),
                        profile, icq, _EXAMPLE_INFO, settings)
    return shlex.join(str(c) for c in cmd)
