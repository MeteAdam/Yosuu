# Yosuu

Yosuu is a self-hosted web app that converts media to MP4 video or MP3 audio. Its dark workbench shows the source duration, available resolutions, and target MP3 bitrates before you start an export.

## Run with Docker

```sh
docker compose up --build
```

Open [Yosuu](http://127.0.0.1:8000), choose **File**, and inspect a recording. Pick **MP4** or **MP3**, select the quality, then choose **Prepare** and **Download**. Source files and exports are temporary.

## Enable platform links

Link processing is opt-in per instance. Enable only the platforms and use cases you are permitted to access. After you enable links, choose **Link**, paste the URL for one video, confirm permission, and choose **Inspect link**.

```sh
# Bash
YOSUU_LINK_PLATFORMS=youtube,instagram,tiktok docker compose up --build
```

```powershell
# PowerShell
$env:YOSUU_LINK_PLATFORMS = 'youtube,instagram,tiktok'
docker compose up --build
```

| Platform | Accepted links | Extraction |
| --- | --- | --- |
| YouTube | Watch, Shorts, and youtu.be | yt-dlp with the bundled EJS dependency and Node.js |
| Instagram | Reel and individual video posts | yt-dlp with guest access |
| TikTok | Individual video posts and recognized short links | yt-dlp with guest access |

A matching URL can still be unavailable due to source restrictions, platform changes, or the hosting IP. The application falls back to an explicit error with the option to inspect a local file. For YouTube uploads, [Studio and Takeout](https://support.google.com/youtube/answer/56100) provide official export paths. TikTok offers [device saving](https://support.tiktok.com/en/using-tiktok/exploring-videos/video-downloads) where permitted. Instagram content can be imported from an authorized export or the original file.

The workbench handles one source at a time per job. Files can be up to 200 MiB and 20 minutes long. Live streams, collections, private or login-restricted sources, and DRM-protected streams are outside the supported scope. MP3 offers 128, 192, and 320 kbps target bitrates. A higher target bitrate does not restore detail lost in the source. Link-based MP4 options use source renditions. Local files can also be scaled down without changing aspect ratio.

## Run from source

Install Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 24, and FFmpeg with `ffmpeg`, `ffprobe`, `libx264`, and `libmp3lame` available. Run from the repository root.

```sh
uv sync --locked
npm --prefix frontend ci
npm --prefix frontend run build
uv run python -m uvicorn yosuu.app:app --host 127.0.0.1 --no-access-log
```

For frontend development, run the API on port 8000 and start `npm --prefix frontend run dev` in a second terminal. Vite forwards `/api` to the backend. To enable platform links without Docker, set `YOSUU_LINK_PLATFORMS` before starting Uvicorn.

## Updating yt-dlp

The Python project and lockfile pin the [upstream release](https://github.com/yt-dlp/yt-dlp/releases/tag/2026.10.26) and resolve its matching EJS package. The application adapts yt-dlp's JSON and format IDs in `yosuu/media.py`. The URL and outbound host rules live in `yosuu/platforms.py`.

1. Sync your main branch with `git pull --ff-only` and create a dependency update branch.
2. Check the upstream release notes, then update the exact yt-dlp pin in `pyproject.toml`. Run `uv lock` and `uv sync --locked`.
3. Review the lockfile diff, run the checks below, and smoke-test inspection plus an actual export with a permitted sample from each enabled platform on the intended host. Review new CDN hostnames before adding them to the allowlist.
4. Commit the pin and lockfile together. Keep the previous pair available for rollback if platform extraction regresses.

## Verification

```sh
uv run ruff check .
uv run ruff format --check
uv run pytest
npm --prefix frontend run build
```

CI generates a portrait fixture and checks actual MP4 and MP3 outputs with FFprobe. The deterministic test suite does not rely on external social platforms.

## Operating an instance

The default binds to the loopback interface. Run one server worker because job IDs and the queue live in memory. Link requests are limited to explicit platform URL patterns. yt-dlp uses a per-job HTTPS tunnel that permits the configured platform/CDN hosts and rejects non-public DNS answers. FFmpeg reads local files, and queued work, input size, and processing time are bounded. Files expire after about 15 minutes.

For a shared deployment, add authentication and per-user quotas at the reverse proxy, keep the worker network isolated from internal services, and set `YOSUU_HOSTS` to the allowed deployment hostname. Review each platform's current terms and your instance's Terms and Privacy text before making it publicly accessible.
