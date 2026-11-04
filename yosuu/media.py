import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .contracts import Inspection, MediaError, OutputFormat, Platform, Quality
from .platforms import EXTRACTORS, platform_proxy
from .processes import MAX_DURATION, MAX_INPUT, ProcessFailure, run

LOCAL_INPUT = [
    "-protocol_whitelist",
    "file",
    "-format_whitelist",
    "mov,matroska,mp3,wav,ogg,flac,aac",
]


@dataclass
class Source:
    view: Inspection
    file: Path | None
    link: str | None
    selectors: dict[str, str]


def audio_options(present: bool) -> list[Quality]:
    return (
        [
            Quality(id=str(rate), label=f"{rate} kbps", detail=detail)
            for rate, detail in [
                (128, "Smaller file"),
                (192, "Balanced"),
                (320, "Higher bitrate"),
            ]
        ]
        if present
        else []
    )


def checked_duration(value: object) -> float:
    try:
        duration = float(str(value))
    except ValueError as exc:
        raise MediaError("The media duration is unavailable.") from exc
    if not 0 < duration <= MAX_DURATION:
        raise MediaError("Use a recording between one frame and 20 minutes long.")
    return duration


async def probe(file: Path, folder: Path) -> dict:
    try:
        result = await run(
            [
                "ffprobe",
                "-v",
                "error",
                *LOCAL_INPUT,
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(file),
            ],
            folder,
            seconds=20,
        )
    except ProcessFailure as exc:
        raise MediaError(
            "This file is unreadable or uses an unsupported media format."
        ) from exc
    return json.loads(result)


def streams(info: dict) -> tuple[dict, dict]:
    video = next(
        (
            item
            for item in info["streams"]
            if item["codec_type"] == "video"
            and not item.get("disposition", {}).get("attached_pic")
        ),
        {},
    )
    audio = next(
        (item for item in info["streams"] if item["codec_type"] == "audio"), {}
    )
    return video, audio


def dimensions(video: dict) -> tuple[int, int]:
    width, height = video.get("width") or 0, video.get("height") or 0
    rotation = next(
        (
            side.get("rotation")
            for side in video.get("side_data_list", [])
            if "rotation" in side
        ),
        0,
    )
    return (height, width) if rotation % 180 else (width, height)


def check_dimensions(width: int, height: int) -> None:
    if min(width, height) < 2 or max(width, height) > 4096:
        raise MediaError(
            "Use video with dimensions between 2 and 4096 pixels per side."
        )


async def inspect_file(file: Path, title: str) -> Source:
    info = await probe(file, file.parent)
    video, audio = streams(info)
    options = []
    if video:
        width, height = dimensions(video)
        check_dimensions(width, height)
        options.append(
            Quality(id="original", label="Original", detail=f"{width} × {height}")
        )
        options.extend(
            Quality(id=str(size), label=f"{size}p", detail="Scaled to fit")
            for size in (1080, 720, 480, 360)
            if size < min(width, height)
        )
    if not video and not audio:
        raise MediaError("The file has no video or audio track.")
    view = Inspection(
        id=uuid4().hex,
        title=title,
        source="file",
        duration=checked_duration(info["format"].get("duration")),
        video=options,
        audio=audio_options(bool(audio)),
    )
    return Source(view, file, None, {})


async def ytdlp(
    platform: Platform, link: str, folder: Path, args: list[str], seconds: int
) -> str:
    async with platform_proxy(platform) as proxy:
        try:
            return await run(
                [
                    sys.executable,
                    "-m",
                    "yt_dlp",
                    "--ignore-config",
                    "--no-cache-dir",
                    "--no-plugin-dirs",
                    "--no-playlist",
                    "--no-progress",
                    "--no-colors",
                    "--no-js-runtimes",
                    "--js-runtimes",
                    "node",
                    "--proxy",
                    proxy,
                    "--socket-timeout",
                    "12",
                    "--retries",
                    "1",
                    "--extractor-retries",
                    "1",
                    "--use-extractors",
                    EXTRACTORS[platform],
                    *args,
                    "--",
                    link,
                ],
                folder,
                seconds=seconds,
            )
        except ProcessFailure as exc:
            details = exc.diagnostics.lower()
            if any(
                term in details
                for term in ("sign in", "log in", "login", "private", "captcha", "429")
            ):
                message = "The platform requires verification or has restricted access. Use a file instead."
            elif "ip address is blocked" in details:
                message = "The platform blocked this instance's network for this post. Use a file instead."
            elif "requested format is not available" in details:
                message = "This quality is no longer available. Inspect the link again."
            else:
                message = "The platform did not return playable media. Check the link or use a file."
            raise MediaError(message) from exc


def remote_options(info: dict) -> tuple[list[Quality], dict[str, str]]:
    formats = [
        item
        for item in info.get("formats", [])
        if item.get("protocol") in {"https", "m3u8_native", "http_dash_segments"}
        and str(item.get("url", "")).startswith("https://")
        and not item.get("has_drm")
        and re.fullmatch(r"[\w.-]+", str(item.get("format_id", "")), re.ASCII)
        and (item.get("filesize") or item.get("filesize_approx") or 0) <= MAX_INPUT
    ]
    audio = [item for item in formats if item.get("acodec") not in (None, "none")]
    audio_only = [item for item in audio if item.get("vcodec") == "none"]
    selectors = {}
    if audio:
        selectors["audio"] = (audio_only or audio)[-1]["format_id"]

    best: dict[int, tuple[dict, str]] = {}
    for item in formats:
        if item.get("vcodec") in (None, "none"):
            continue
        width, height = item.get("width") or 0, item.get("height") or 0
        size = min(width, height)
        if size < 2 or max(width, height) > 4096:
            continue
        selector = item["format_id"]
        if item.get("acodec") in (None, "none") and audio:
            if not audio_only:
                continue
            compatible = [
                entry for entry in audio_only if entry["acodec"].startswith("mp4a")
            ]
            selector += "+" + (compatible or audio_only)[-1]["format_id"]
        best[size] = (item, selector)

    video = []
    for size, (item, selector) in sorted(best.items(), reverse=True):
        key = f"source-{size}"
        selectors[key] = selector
        video.append(
            Quality(
                id=key, label=f"{size}p", detail=f"{item['width']} × {item['height']}"
            )
        )
    return video, selectors


async def inspect_link(platform: Platform, link: str, folder: Path) -> Source:
    result = await ytdlp(
        platform, link, folder, ["--dump-single-json", "--skip-download"], 55
    )
    info = json.loads(result)
    if (
        "entries" in info
        or info.get("is_live")
        or info.get("live_status") in {"is_upcoming", "post_live"}
    ):
        raise MediaError(
            "Use a single, published video. Live streams and collections are unsupported."
        )
    raw_duration = info.get("duration")
    duration = checked_duration(raw_duration) if raw_duration is not None else None
    video, selectors = remote_options(info)
    if not video and "audio" not in selectors:
        raise MediaError("No supported streams are available for this video.")
    return Source(
        Inspection(
            id=uuid4().hex,
            title=(info.get("title") or "Untitled video")[:180],
            source=platform,
            duration=duration,
            video=video,
            audio=audio_options("audio" in selectors),
        ),
        None,
        link,
        selectors,
    )


async def download_source(
    source: Source, format_: OutputFormat, quality_id: str, folder: Path
) -> Path:
    platform = source.view.source
    assert source.link is not None and platform != "file"
    selector = source.selectors["audio" if format_ == "mp3" else quality_id]
    size = f"{MAX_INPUT // (1024 * 1024)}M"
    await ytdlp(
        platform,
        source.link,
        folder,
        [
            "-f",
            selector,
            "--merge-output-format",
            "mkv",
            "--max-filesize",
            size,
            "--downloader",
            "native",
            "--match-filter",
            f"!is_live & duration <=? {MAX_DURATION}",
            "-o",
            "source.%(ext)s",
        ],
        135,
    )
    paths = await asyncio.to_thread(lambda: list(folder.glob("source.*")))
    candidates = [file for file in paths if file.suffix not in {".part", ".ytdl"}]
    if len(candidates) != 1 or not 0 < candidates[0].stat().st_size <= MAX_INPUT:
        raise MediaError("The source did not download or exceeds the 200 MiB limit.")
    return candidates[0]


async def convert(
    file: Path, format_: OutputFormat, quality_id: str, folder: Path
) -> Path:
    info = await probe(file, folder)
    video, audio = streams(info)
    checked_duration(info["format"].get("duration"))
    output = folder / f"output.{format_}"
    args = []
    if format_ == "mp3":
        if not audio:
            raise MediaError("This source has no audio track.")
        args = [
            "-map",
            f"0:{audio['index']}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            f"{quality_id}k",
        ]
    else:
        if not video:
            raise MediaError("This source has no video track.")
        width, height = dimensions(video)
        check_dimensions(width, height)
        if quality_id.startswith("source-") and min(width, height) != int(
            quality_id.split("-")[1]
        ):
            raise MediaError(
                "The platform returned a different resolution. Inspect the link again."
            )
        args = ["-map", f"0:{video['index']}"]
        if audio:
            args += [
                "-map",
                f"0:{audio['index']}",
                "-c:a",
                "copy" if audio["codec_name"] == "aac" else "aac",
            ]
        scale = quality_id.isdecimal()
        compatible = video["codec_name"] == "h264" and video.get("pix_fmt") in {
            "yuv420p",
            "yuvj420p",
        }
        if compatible and not scale:
            args += ["-c:v", "copy"]
        else:
            args += [
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-threads",
                "2",
            ]
            if scale:
                ratio = (
                    f"scale=-2:{quality_id}"
                    if width >= height
                    else f"scale={quality_id}:-2"
                )
                args += ["-vf", ratio]
            elif width % 2 or height % 2:
                args += ["-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2"]
        args += ["-movflags", "+faststart"]
    await run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-threads",
            "2",
            *LOCAL_INPUT,
            "-i",
            str(file),
            *args,
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            str(output),
        ],
        folder,
        seconds=210,
    )
    if not 0 < output.stat().st_size <= MAX_INPUT:
        raise MediaError(
            "The output exceeds the 200 MiB limit. Select a lower quality."
        )
    return output
