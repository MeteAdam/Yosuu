import asyncio
import json
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from yosuu.app import app
from yosuu.contracts import MediaError
from yosuu.media import remote_options
from yosuu.platforms import allowed_host, canonical_link, platform_proxy, public_address
from yosuu.processes import run


@pytest.mark.parametrize(
    ("link", "platform"),
    [
        ("https://youtu.be/abcdefghijk?t=5", "youtube"),
        ("https://www.instagram.com/reel/abcde/?tracking=1", "instagram"),
        ("https://vm.tiktok.com/abcde/", "tiktok"),
    ],
)
def test_supported_links(link, platform):
    source, canonical = canonical_link(link)
    assert source == platform
    assert "tracking" not in canonical and "t=5" not in canonical


@pytest.mark.parametrize(
    "link",
    [
        "http://www.youtube.com/watch?v=abcdefghijk",
        "https://www.youtube.com.evil.example/watch?v=abcdefghijk",
        "https://www.youtube.com@localhost/watch?v=abcdefghijk",
        "https://www.youtube.com:443/watch?v=abcdefghijk",
        "https://www.youtube.com/playlist?list=abcdefghijk",
        "https://www.tiktok.com/@somebody/photo/1234567890",
    ],
)
def test_rejects_unsupported_links(link):
    with pytest.raises(MediaError):
        canonical_link(link)


def test_egress_filters():
    assert allowed_host("a.googlevideo.com", "youtube")
    assert not allowed_host("googlevideo.com.evil.example", "youtube")
    assert not allowed_host("googlevideo.com", "tiktok")
    assert all(
        not public_address(host)
        for host in (
            "127.0.0.1",
            "10.1.2.3",
            "169.254.169.254",
            "::1",
            "::ffff:127.0.0.1",
            "224.0.0.1",
        )
    )
    assert public_address("1.1.1.1")


def test_proxy_rejects_internal_targets():
    async def check():
        async with platform_proxy("youtube") as proxy:
            port = int(proxy.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            try:
                writer.write(b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n")
                await writer.drain()
                assert (await reader.readline()).startswith(b"HTTP/1.1 502")
            finally:
                writer.close()
                await writer.wait_closed()

    asyncio.run(check())


def test_cancelled_process_releases_its_temporary_logs(tmp_path):
    async def check():
        task = asyncio.create_task(
            run(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                tmp_path,
                seconds=90,
            )
        )
        async with asyncio.timeout(5):
            await asyncio.sleep(0)
            assert (tmp_path / "process.out").exists()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(check())
    assert not list(tmp_path.iterdir())


def test_remote_quality_uses_short_edge_and_separate_audio():
    formats = [
        {"format_id": "audio", "acodec": "mp4a.40.2", "vcodec": "none"},
        {
            "format_id": "portrait",
            "width": 720,
            "height": 1280,
            "vcodec": "avc1",
            "acodec": "none",
        },
        {
            "format_id": "protected",
            "width": 1080,
            "height": 1920,
            "vcodec": "avc1",
            "has_drm": True,
        },
    ]
    for entry in formats:
        entry.update(url="https://media.example/file", protocol="https")
    choices, selectors = remote_options({"formats": formats})
    assert [choice.label for choice in choices] == ["720p"]
    assert selectors == {"audio": "audio", "source-720": "portrait+audio"}


def test_upload_and_export_both_formats(tmp_path):
    clip = tmp_path / "portrait.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=480x640:rate=10",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(clip),
        ],
        check=True,
    )
    with TestClient(app, base_url="http://localhost") as client:
        response = client.post(
            "/api/inspect/file",
            content=clip.read_bytes(),
            headers={"X-Media-Name": "Portrait.mp4"},
        )
        assert response.status_code == 200, response.text
        source = response.json()
        assert source["title"] == "Portrait"
        assert [quality["id"] for quality in source["video"]] == ["original", "360"]
        assert len(source["audio"]) == 3
        rejected = client.post(
            "/api/jobs",
            json={"source_id": source["id"], "format": "mp4", "quality_id": "1080"},
        )
        assert rejected.status_code == 422
        for format_, quality in [("mp4", "360"), ("mp3", "192")]:
            create = client.post(
                "/api/jobs",
                json={
                    "source_id": source["id"],
                    "format": format_,
                    "quality_id": quality,
                },
            )
            assert create.status_code == 202, create.text
            job = create.json()
            deadline = time.monotonic() + 15
            while (
                job["status"] in {"queued", "running"} and time.monotonic() < deadline
            ):
                time.sleep(0.08)
                job = client.get("/api/jobs/" + job["id"]).json()
            assert job["status"] == "ready", job
            download = client.get(job["download_url"])
            assert download.status_code == 200
            assert f"Portrait.{format_}" in download.headers["content-disposition"]
            path = tmp_path / f"output.{format_}"
            path.write_bytes(download.content)
            result = subprocess.check_output(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)]
            )
            output = json.loads(result)["streams"]
            if format_ == "mp4":
                assert {(track["codec_name"]) for track in output} == {"h264", "aac"}
                assert (output[0]["width"], output[0]["height"]) == (360, 480)
            else:
                assert len(output) == 1 and output[0]["codec_name"] == "mp3"
                assert output[0]["bit_rate"] == "192000"


def test_link_access_is_opt_in_and_same_origin(monkeypatch):
    monkeypatch.delenv("YOSUU_LINK_PLATFORMS", raising=False)
    with TestClient(app, base_url="http://localhost") as client:
        payload = {"url": "https://youtu.be/abcdefghijk", "authorized": True}
        assert client.get("/api/config").json()["link_platforms"] == []
        assert client.post("/api/inspect/link", json=payload).status_code == 403
        assert (
            client.post(
                "/api/inspect/link",
                json=payload,
                headers={"Origin": "https://elsewhere.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/inspect/link", json={**payload, "authorized": False}
            ).status_code
            == 422
        )
