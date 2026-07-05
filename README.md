# Transqode

Self-hosted media transcoding with Intel QSV hardware AV1 encoding, Opus audio
and **VMAF-targeted quality search** — inspired by
[Unmanic](https://github.com/Unmanic/unmanic), [FileFlows](https://fileflows.com),
[Tdarr](https://home.tdarr.io) and [ab-av1](https://github.com/alexheretic/ab-av1).

Everything runs in a single Docker container built on
[linuxserver.io's ffmpeg image](https://github.com/linuxserver/docker-ffmpeg)
(ffmpeg 8.x with `av1_qsv`, `libvmaf`, `libopus` and the Intel media stack
prebuilt — nothing is compiled from source).

## Features

- **Slim web UI** (no build step, no external assets) with dashboard, sources,
  profiles, logs and settings pages
- **Source folders**: assign a transcoding profile per folder, hit *Scan now*,
  or enable *Watch* to queue new files automatically (files are only queued
  once their size is stable between two scans)
- **Transcoding profiles**: predefined `AV1 QSV + Opus (ICQ 22)` — AV1 via
  `av1_qsv` in ICQ mode, audio in Opus keeping the source channel count at
  64 kbps per channel. Create as many profiles as you like (fixed ICQ or VMAF
  target, preset, audio copy/re-encode, container, extra ffmpeg args)
- **VMAF matching** (à la ab-av1): set a VMAF target (e.g. 95) instead of a
  fixed quality. Transqode cuts short sample clips from across the file,
  encodes them at candidate ICQ values, scores them with libvmaf and
  binary-searches the highest ICQ (= smallest file) that still meets the target
- **Dashboard**: live progress (%, fps, speed, ETA), queue, per-file and total
  **saved space**, retry/cancel
- **Logs in the UI**: full ffmpeg output per job plus the application log
- **Finished files are tagged** with a `TRANSQODE` metadata tag (visible via
  `ffprobe`), so they are recognized and never transcoded twice — even after
  moving them around
- **SQLite** for all state (profiles, sources, jobs, settings) in `/config`

## Quick start

```yaml
# docker-compose.yml
services:
  transqode:
    build: .                        # or: image: ghcr.io/YOURNAME/transqode:latest
    container_name: transqode
    ports:
      - "8585:8585"
    devices:
      - /dev/dri:/dev/dri           # Intel GPU for QSV
    volumes:
      - ./config:/config
      - /path/to/movies:/media/movies
    restart: unless-stopped
```

```bash
docker compose up -d --build
```

Open **http://localhost:8585**, add `/media/movies` as a source, pick a
profile, press **Scan now**.

### Hardware requirements

AV1 hardware encoding (`av1_qsv`) needs an Intel GPU with an AV1 encoder:
**Arc (DG2) discrete GPUs or Meteor Lake (Core Ultra) and newer iGPUs**.
Older iGPUs (UHD 6xx/7xx) can decode AV1 but not encode it — use a profile
with `hevc_qsv` instead. The container needs access to `/dev/dri`.

**Multiple GPUs** (e.g. iGPU + Arc): map the render node you want at its
*original* path (renaming breaks libva's sysfs lookup) and point the
`qsv_device` setting (Settings page) at it, e.g. `/dev/dri/renderD129`.

## How VMAF matching works

For a profile in *VMAF target* mode, each file first goes through an
`analyzing` phase:

1. 2–6 sample clips (20 s each, spread across the runtime) are extracted
   losslessly
2. Samples are encoded with the profile's settings at a candidate ICQ and
   compared against the source clips with `libvmaf`
3. A binary search over the ICQ range (default 16–35) finds the **highest ICQ
   whose mean sample VMAF still reaches the target** — highest ICQ means
   smallest file
4. The whole file is then encoded once with the chosen ICQ; the predicted
   VMAF and chosen ICQ are shown on the dashboard

Sample count, clip length and the ICQ search range are configurable on the
Settings page. Cost: roughly 4–6 short sample encodes + VMAF runs per file.

## Profiles

| Field | Meaning |
| --- | --- |
| Video codec | any ffmpeg encoder, default `av1_qsv` |
| Preset | `veryfast` … `veryslow` (QSV target usage) |
| Quality mode | **Fixed ICQ** or **VMAF target** |
| ICQ | QSV Intelligent Constant Quality, lower = better (default 22) |
| VMAF target | e.g. 95 — auto-derives the ICQ per file |
| Audio | Opus at N kbps × channel count (channels preserved), or copy |
| Container | mkv (recommended) or mp4 |
| Extra video args | appended verbatim, e.g. `-look_ahead_depth 40` |

Subtitles, chapters, metadata and (for mkv) font attachments are carried over;
`mov_text` subtitles are converted to SRT.

## Behavior details

- **In place vs. output folder**: without an output folder the original is
  replaced (same name, profile's container extension). With an output folder
  the source tree is mirrored there and originals are kept.
- **Skip if larger**: if the encode ends up bigger than the source, the result
  is discarded; with an output folder configured the original is moved there
  instead, so *every* processed file ends up in the output tree (configurable).
- **Tagging**: outputs carry `TRANSQODE=1`, `TRANSQODE_PROFILE`,
  `TRANSQODE_ICQ` and `TRANSQODE_DATE` format tags.
- **Watch**: every `scan_interval_s` (default 300 s) watched folders are
  scanned; unstable (still copying) files wait one more round.
- **Crash safety**: encodes write to hidden `.…tqtmp.…` temp files that are
  atomically renamed on success and ignored by the scanner.

## Development

```bash
docker compose up --build          # run
# API docs: http://localhost:8585/docs
```

Layout: `transqode/` Python package (FastAPI app in `main.py`, workers in
`worker.py`, VMAF search in `vmaf.py`, scanner in `scanner.py`), vanilla-JS UI
in `transqode/static/`. State lives in `/config/transqode.db`; logs in
`/config/logs/`.

## License

MIT — see [LICENSE](LICENSE).
