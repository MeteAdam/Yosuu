# Yosuu state

## Purpose
Self-hosted web workbench for authorized media, with selectable MP4 and MP3 exports.

## Working state and constraints
- FastAPI and React share a single origin. FFmpeg processes local files.
- yt-dlp is locked with EJS. YouTube, Instagram, and TikTok link adapters are opt-in.
- Live inspection worked on a sample from each platform. The upstream YouTube test clip exported as MP4 and MP3.
- Job state and files are temporary. Run one server worker.
- CI uses generated media and does not depend on third-party social platforms.

## Next immediate step
Validate exports with permitted Instagram and TikTok samples on the intended host.
