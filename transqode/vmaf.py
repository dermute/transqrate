"""VMAF-targeted quality search, inspired by ab-av1 (github.com/alexheretic/ab-av1).

Short sample clips are cut from across the file, encoded at candidate ICQ
values, and scored against the source with libvmaf. A binary search finds the
highest ICQ (= smallest file) whose mean sample VMAF still meets the target.
"""

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import config, media

VMAF_RE = re.compile(r"VMAF score:\s*([0-9.]+)")


class Cancelled(Exception):
    pass


@dataclass
class SearchResult:
    icq: int
    vmaf: float
    size_ratio: float          # encoded sample bytes / source sample bytes
    hit_target: bool


def find_icq(input_path: Path, profile: dict, settings: dict, info: dict,
             job_id: int, log, cancel_check=lambda: False) -> SearchResult:
    duration = media.duration_s(info)
    if duration <= 0:
        raise media.MediaError("cannot determine duration for VMAF sampling")

    target = float(profile.get("vmaf_target", 95.0))
    icq_min = int(settings.get("icq_min", 16))
    icq_max = int(settings.get("icq_max", 35))
    sample_s = max(5, int(settings.get("vmaf_sample_s", 20)))
    n_min = max(1, int(settings.get("vmaf_min_samples", 2)))
    n_max = max(n_min, int(settings.get("vmaf_max_samples", 6)))
    # roughly one sample per 8 minutes, clamped
    n = max(n_min, min(n_max, int(duration // 480) or 1))

    workdir = config.TMP_DIR / f"vmaf_job_{job_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        samples = _extract_samples(input_path, workdir, duration, sample_s, n, log, cancel_check)
        if not samples:
            raise media.MediaError("could not extract any usable sample clips")
        log(f"vmaf search: target {target}, ICQ range [{icq_min}..{icq_max}], "
            f"{len(samples)} samples of {sample_s}s")

        cache: dict[int, tuple[float, float]] = {}

        def evaluate(q: int) -> tuple[float, float]:
            if q in cache:
                return cache[q]
            if cancel_check():
                raise Cancelled()
            scores, in_bytes, out_bytes = [], 0, 0
            for i, sample in enumerate(samples):
                enc = workdir / f"enc_{i}_q{q}.mkv"
                cmd = [config.FFMPEG, "-y", "-hide_banner", "-nostdin",
                       *media.qsv_device_args(settings), "-i", str(sample),
                       *media.video_args(profile, q), "-an", "-sn", "-dn", str(enc)]
                proc = media.run_quiet(cmd)
                if proc.returncode != 0 or not enc.exists():
                    raise media.MediaError(
                        f"sample encode failed at ICQ {q}: {proc.stderr.strip()[-800:]}")
                scores.append(_vmaf_score(enc, sample))
                in_bytes += sample.stat().st_size
                out_bytes += enc.stat().st_size
            vmaf = sum(scores) / len(scores)
            ratio = out_bytes / in_bytes if in_bytes else 1.0
            cache[q] = (vmaf, ratio)
            log(f"  ICQ {q}: VMAF {vmaf:.2f} (samples: "
                f"{', '.join(f'{s:.2f}' for s in scores)}), size ratio {ratio:.2%}")
            return vmaf, ratio

        # binary search: highest q whose vmaf >= target
        lo, hi = icq_min, icq_max
        best: SearchResult | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            vmaf, ratio = evaluate(mid)
            if vmaf >= target:
                best = SearchResult(mid, vmaf, ratio, True)
                lo = mid + 1
            else:
                hi = mid - 1

        if best is None:
            vmaf, ratio = evaluate(icq_min)
            best = SearchResult(icq_min, vmaf, ratio, False)
            log(f"warning: even ICQ {icq_min} only reaches VMAF {vmaf:.2f} "
                f"(target {target}); using ICQ {icq_min}")
        else:
            log(f"vmaf search result: ICQ {best.icq} -> predicted VMAF {best.vmaf:.2f}, "
                f"video size ratio {best.size_ratio:.2%}")
        return best
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _extract_samples(input_path: Path, workdir: Path, duration: float,
                     sample_s: int, n: int, log, cancel_check) -> list[Path]:
    samples = []
    for i in range(n):
        if cancel_check():
            raise Cancelled()
        pos = max(0.0, duration * (i + 1) / (n + 1) - sample_s / 2)
        out = workdir / f"sample_{i}.mkv"
        cmd = [config.FFMPEG, "-y", "-hide_banner", "-nostdin",
               "-ss", f"{pos:.2f}", "-i", str(input_path), "-t", str(sample_s),
               "-map", "0:v:0", "-c", "copy", "-an", "-sn", "-dn", str(out)]
        proc = media.run_quiet(cmd)
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            samples.append(out)
        else:
            log(f"  sample {i} at {pos:.0f}s failed to extract, skipping")
    return samples


def _vmaf_score(distorted: Path, reference: Path) -> float:
    threads = os.cpu_count() or 4
    cmd = [config.FFMPEG, "-hide_banner", "-nostdin",
           "-i", str(distorted), "-i", str(reference),
           "-lavfi",
           f"[0:v]setpts=PTS-STARTPTS[d];[1:v]setpts=PTS-STARTPTS[r];"
           f"[d][r]libvmaf=n_threads={threads}",
           "-f", "null", "-"]
    proc = media.run_quiet(cmd)
    match = VMAF_RE.search(proc.stderr or "")
    if proc.returncode != 0 or not match:
        raise media.MediaError(f"libvmaf scoring failed: {(proc.stderr or '').strip()[-800:]}")
    return float(match.group(1))
