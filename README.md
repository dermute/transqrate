# transQrate

**Tell transQrate how good the result should look — it finds the encoder
settings.** Instead of guessing quality values, you set a VMAF target
(e.g. 95) per media folder: transQrate cuts short sample clips from every
file, encodes them at candidate quality levels, scores them with libvmaf
and binary-searches the highest ICQ — the smallest file — that still hits
your target. The ab-av1 idea, built into a self-hosted transcoding server.

Under the hood: Intel QSV hardware AV1 encoding, Opus audio and a slim
web UI — inspired by [Unmanic](https://github.com/Unmanic/unmanic),
[FileFlows](https://fileflows.com), [Tdarr](https://home.tdarr.io) and
[ab-av1](https://github.com/alexheretic/ab-av1).

Everything runs in a single Docker container built on
[linuxserver.io's ffmpeg image](https://github.com/linuxserver/docker-ffmpeg)
(ffmpeg 8.x with `av1_qsv`, `libvmaf`, `libopus` and the Intel media stack
prebuilt — nothing is compiled from source).

## Features

- **VMAF matching** (à la ab-av1): set a VMAF target instead of a fixed
  quality — every file gets exactly the quality you asked for, at the
  smallest size the encoder can deliver
- **Slim web UI** (no build step, no external assets) with dashboard, sources,
  profiles, logs and settings pages
- **Source folders**: assign a transcoding profile per folder, hit *Scan now*,
  or enable *Watch* to queue new files automatically (files are only queued
  once their size is stable between two scans)
- **Transcoding profiles**: predefined `AV1 QSV + Opus (ICQ 22)` — AV1 via
  `av1_qsv` in ICQ mode, audio in Opus keeping the source channel count at
  64 kbps per channel. Create as many profiles as you like (fixed ICQ or VMAF
  target, preset, audio copy/re-encode, container, extra ffmpeg args)
- **Dashboard**: live progress (%, fps, speed, ETA), queue, per-file and total
  **saved space**, retry/cancel
- **Logs in the UI**: full ffmpeg output per job plus the application log
- **Finished files are tagged** with a `TRANSQRATE` metadata tag (visible via
  `ffprobe`), so they are recognized and never transcoded twice — even after
  moving them around
- **SQLite** for all state (profiles, sources, jobs, settings) in `/config`

## Quick start

```yaml
# docker-compose.yml
services:
  transqrate:
    image: ghcr.io/dermute/transqrate:latest
    container_name: transqrate
    init: true
    ports:
      - "8585:8585"
    devices:
      - /dev/dri:/dev/dri           # Intel GPU for QSV
    volumes:
      - ./config:/config            # database, logs, temp files
      - /path/to/media:/media/library      # a source folder
      - /path/to/transcoded:/output        # optional output folder
    restart: unless-stopped
```

```bash
docker compose up -d
```

Open **http://localhost:8585**, add `/media/library` as a source, pick a
profile, press **Scan now**.

### Source & output folders

Mount every folder transQrate should see as a volume, then reference the
*container* paths in the web UI. Each source folder has an optional output
folder:

- **No output folder** — transcode *in place*: the finished file replaces
  the original (same location and name, container extension of the profile).
- **With an output folder** (e.g. `/output`) — the source tree is mirrored
  there (`/media/library/A/b.mkv` → `/output/A/b.mkv`) and originals are
  kept untouched. If an encode would end up larger than the source, the
  original is *moved* to the output folder instead, so the output tree
  always holds every processed file.

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

If the profile downscales, the *reference* sample is passed through the
identical scale filter (pure filtering, no re-encode) so both sides are
compared at the output resolution — the score isolates codec fidelity
instead of punishing the intentional resolution change (same approach as
ab-av1's `--reference-vfilter` default).

## Profiles

| Field | Meaning |
| --- | --- |
| Video codec | any ffmpeg encoder, default `av1_qsv` |
| Preset | `veryfast` … `veryslow` (QSV target usage) |
| Quality mode | **Fixed ICQ** or **VMAF target** |
| ICQ | QSV Intelligent Constant Quality, lower = better (default 22) |
| VMAF target | e.g. 95 — auto-derives the ICQ per file |
| Output resolution | keep source, or downscale to 480p/720p/1080p/4K (never upscales; width-based so scope content maps correctly) |
| Bit depth | *keep source* (default; HDR content stays 10-bit) or *force 8-bit* |
| Audio | Opus at N kbps × channel count (channels preserved), or copy |
| Audio channel limit | optional downmix cap, e.g. *max 5.1* turns 7.1 into 5.1 but leaves stereo untouched |
| Container | mkv (recommended) or mp4 |
| Extra video args | appended verbatim, e.g. `-look_ahead_depth 100 -extbrc 1 -adaptive_i 1 -adaptive_b 1 -g 240` (the default profiles' setting) |

Subtitles, chapters, metadata and (for mkv) font attachments are carried over;
`mov_text` subtitles are converted to SRT.

## Behavior details

- **In place vs. output folder**: without an output folder the original is
  replaced (same name, profile's container extension). With an output folder
  the source tree is mirrored there and originals are kept.
- **Skip if larger**: if the encode ends up bigger than the source, the result
  is discarded; with an output folder configured the original is moved there
  instead, so *every* processed file ends up in the output tree (configurable).
- **Tagging**: outputs carry `TRANSQRATE=1`, `TRANSQRATE_PROFILE`,
  `TRANSQRATE_ICQ` and `TRANSQRATE_DATE` format tags.
- **Watch**: every `scan_interval_s` (default 300 s) watched folders are
  scanned; unstable (still copying) files wait one more round.
- **HDR**: with bit depth *keep source*, 10-bit and the PQ/HLG colorimetry
  survive the encode (HDR10-compatible AV1 output). HDR10+ and Dolby
  Vision *dynamic* metadata is not carried over.
- **Crash safety**: encodes write to hidden `.…tqtmp.…` temp files that are
  atomically renamed on success and ignored by the scanner.

## Development

```bash
docker compose up --build          # run
# API docs: http://localhost:8585/docs
```

Layout: `transqrate/` Python package (FastAPI app in `main.py`, workers in
`worker.py`, VMAF search in `vmaf.py`, scanner in `scanner.py`), vanilla-JS UI
in `transqrate/static/`. State lives in `/config/transqrate.db`; logs in
`/config/logs/`.

## AI attribution

This project was developed with AI assistance (Anthropic Claude, Fable 5).

<div style="display: flex; align-items: center; white-space: nowrap; gap: 0.5rem; padding: 8px;">
  <div style="font-family: IBM Plex Sans; font-weight: 400; font-size: 16px; line-height: 22px; letter-spacing: 0px;">
    <a rel="noopener noreferrer" href="https://aiattribution.github.io/statements/AIA-EAI-Hin-Nr-?model=Fable%205-v1.0" data-cy="recommended-attribution-statement-text" target="_blank" style="font-family: IBM Plex Sans; font-weight: 400; font-size: 16px; line-height: 22px; letter-spacing: 0px;">AIA EAI Hin Nr Fable 5 v1.0 </a>
  </div>
  <div style="display: flex; gap: 0.5rem;">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <g clip-path="url(#clip0_50_2)">
        <path d="M12 23.5C18.3513 23.5 23.5 18.3513 23.5 12C23.5 5.64873 18.3513 0.5 12 0.5C5.64873 0.5 0.5 5.64873 0.5 12C0.5 18.3513 5.64873 23.5 12 23.5Z" fill="#4E4E4E" stroke="#161616">
        </path>
        <path d="M13.6471 15.6L13.1471 13.94H10.8171L10.3171 15.6H8.77715L11.0771 8.61998H12.9571L15.2271 15.6H13.6471ZM11.9971 9.99998H11.9471L11.1771 12.65H12.7771L11.9971 9.99998Z" fill="white">
        </path>
      </g>
      <defs>
        <clipPath id="clip0_50_2">
          <rect width="24" height="24" fill="white">
          </rect>
        </clipPath>
      </defs>
    </svg>
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M18 17H16.5V16H18V8H16.5V7H18C18.2651 7.0003 18.5193 7.10576 18.7068 7.29323C18.8942 7.4807 18.9997 7.73488 19 8V16C18.9996 16.2651 18.8942 16.5193 18.7067 16.7067C18.5193 16.8942 18.2651 16.9996 18 17Z" fill="#161616">
      </path>
      <path d="M15.5 13C16.0523 13 16.5 12.5523 16.5 12C16.5 11.4477 16.0523 11 15.5 11C14.9477 11 14.5 11.4477 14.5 12C14.5 12.5523 14.9477 13 15.5 13Z" fill="#161616">
      </path>
      <path d="M12 13C12.5523 13 13 12.5523 13 12C13 11.4477 12.5523 11 12 11C11.4477 11 11 11.4477 11 12C11 12.5523 11.4477 13 12 13Z" fill="#161616">
      </path>
      <path d="M8.5 13C9.05228 13 9.5 12.5523 9.5 12C9.5 11.4477 9.05228 11 8.5 11C7.94772 11 7.5 11.4477 7.5 12C7.5 12.5523 7.94772 13 8.5 13Z" fill="#161616">
      </path>
      <path d="M7.5 17H6C5.73488 16.9997 5.4807 16.8942 5.29323 16.7068C5.10576 16.5193 5.0003 16.2651 5 16V8C5.00026 7.73486 5.10571 7.48066 5.29319 7.29319C5.48066 7.10571 5.73486 7.00026 6 7H7.5V8H6V16H7.5V17Z" fill="#161616">
      </path>
      <circle cx="12" cy="12" r="11.5" stroke="#161616">
      </circle>
      <circle cx="12" cy="12" r="11.5" stroke="#161616">
      </circle>
    </svg>
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
      <circle cx="12" cy="12" r="11.5" stroke="#161616">
      </circle>
      <path d="M10 6C10.4945 6 10.9778 6.14662 11.3889 6.42133C11.8 6.69603 12.1205 7.08648 12.3097 7.54329C12.4989 8.00011 12.5484 8.50277 12.452 8.98773C12.3555 9.47268 12.1174 9.91814 11.7678 10.2678C11.4181 10.6174 10.9727 10.8555 10.4877 10.952C10.0028 11.0484 9.50011 10.9989 9.04329 10.8097C8.58648 10.6205 8.19603 10.3 7.92133 9.88893C7.64662 9.4778 7.5 8.99445 7.5 8.5C7.5 7.83696 7.76339 7.20107 8.23223 6.73223C8.70107 6.26339 9.33696 6 10 6ZM10 5C9.30777 5 8.63108 5.20527 8.0555 5.58986C7.47993 5.97444 7.03133 6.52107 6.76642 7.16061C6.50151 7.80015 6.4322 8.50388 6.56725 9.18282C6.7023 9.86175 7.03564 10.4854 7.52513 10.9749C8.01461 11.4644 8.63825 11.7977 9.31718 11.9327C9.99612 12.0678 10.6999 11.9985 11.3394 11.7336C11.9789 11.4687 12.5256 11.0201 12.9101 10.4445C13.2947 9.86892 13.5 9.19223 13.5 8.5C13.5 7.57174 13.1313 6.6815 12.4749 6.02513C11.8185 5.36875 10.9283 5 10 5Z" fill="#161616">
      </path>
      <path d="M15 19H14V16.5C14 15.837 13.7366 15.2011 13.2678 14.7322C12.7989 14.2634 12.163 14 11.5 14H8.5C7.83696 14 7.20107 14.2634 6.73223 14.7322C6.26339 15.2011 6 15.837 6 16.5V19H5V16.5C5 15.5717 5.36875 14.6815 6.02513 14.0251C6.6815 13.3687 7.57174 13 8.5 13H11.5C12.4283 13 13.3185 13.3687 13.9749 14.0251C14.6313 14.6815 15 15.5717 15 16.5V19Z" fill="#161616">
      </path>
      <path d="M19.9592 9.99025L19.3932 9.42432L17.938 10.8796L16.4827 9.42432L15.9167 9.99025L17.372 11.4455L15.9167 12.9008L16.4827 13.4667L17.938 12.0115L19.3932 13.4667L19.9592 12.9008L18.5039 11.4455L19.9592 9.99025Z" fill="#161616">
      </path>
    </svg>
  </div>
</div>

## License

MIT — see [LICENSE](LICENSE).
