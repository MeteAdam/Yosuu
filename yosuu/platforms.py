import asyncio
import ipaddress
import re
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from urllib.parse import parse_qs, urlsplit

from .contracts import MediaError, Platform

EXTRACTORS = {
    "youtube": "youtube",
    "instagram": "Instagram",
    "tiktok": "TikTok,vm[.]tiktok",
}
EGRESS = {
    "youtube": (
        "youtube.com",
        "youtubei.googleapis.com",
        "googlevideo.com",
        "ytimg.com",
    ),
    "instagram": ("instagram.com", "cdninstagram.com", "fbcdn.net"),
    "tiktok": (
        "tiktok.com",
        "tiktokv.com",
        "tiktokcdn.com",
        "tiktokcdn-us.com",
        "byteoversea.com",
        "ibytedtos.com",
        "bytefcdn-oversea.com",
    ),
}


def canonical_link(value: str) -> tuple[Platform, str]:
    try:
        url = urlsplit(value.strip())
        host = url.hostname
        if (
            url.scheme != "https"
            or url.username
            or url.password
            or url.port is not None
        ):
            raise ValueError
    except ValueError as exc:
        raise MediaError("Paste an HTTPS link to one video.") from exc

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        video = re.fullmatch(r"/(?:shorts/)?([\w-]{11})/?", url.path, re.ASCII)
        video_id = video[1] if video else ""
        if host != "youtu.be" and url.path == "/watch":
            video_id = parse_qs(url.query).get("v", [""])[0]
        if re.fullmatch(r"[\w-]{11}", video_id, re.ASCII):
            return "youtube", f"https://www.youtube.com/watch?v={video_id}"

    if host in {"instagram.com", "www.instagram.com"}:
        post = re.fullmatch(r"/(reel|p|tv)/([\w-]{5,30})/?", url.path, re.ASCII)
        if post:
            return "instagram", f"https://www.instagram.com/{post[1]}/{post[2]}/"

    if host in {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}:
        video = re.fullmatch(r"/@([\w.]+)/video/(\d{8,25})/?", url.path, re.ASCII)
        if video:
            return "tiktok", f"https://www.tiktok.com/@{video[1]}/video/{video[2]}"
    if host in {"vm.tiktok.com", "vt.tiktok.com", "www.tiktok.com"}:
        pattern = (
            r"/t/([\w-]{4,24})/?" if host == "www.tiktok.com" else r"/([\w-]{4,24})/?"
        )
        if re.fullmatch(pattern, url.path, re.ASCII):
            return "tiktok", f"https://{host}{url.path}"

    raise MediaError(
        "Use a single YouTube video, Instagram Reel/video or TikTok video link."
    )


def allowed_host(host: str, platform: Platform) -> bool:
    return any(host == base or host.endswith(f".{base}") for base in EGRESS[platform])


def public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while chunk := await reader.read(65536):
        writer.write(chunk)
        await writer.drain()


async def tunnel(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    platform: Platform,
) -> None:
    upstream = None
    pumps: list[asyncio.Task[None]] = []
    established = False
    try:
        async with asyncio.timeout(12):
            headers = await reader.readuntil(b"\r\n\r\n")
            line = headers.split(b"\r\n", 1)[0].decode("ascii")
            match = re.fullmatch(r"CONNECT ([a-zA-Z0-9.-]+):443 HTTP/1\.[01]", line)
            if not match or not allowed_host(match[1].lower(), platform):
                raise ValueError
            addresses = await asyncio.get_running_loop().getaddrinfo(
                match[1],
                443,
                type=socket.SOCK_STREAM,
            )
            # Resolve once, reject non-public answers and dial the verified IP directly.
            if not addresses or not all(
                public_address(item[4][0]) for item in addresses
            ):
                raise ValueError
            for family, _, _, _, endpoint in addresses:
                try:
                    async with asyncio.timeout(3):
                        incoming, upstream = await asyncio.open_connection(
                            endpoint[0], 443, family=family
                        )
                    break
                except (OSError, TimeoutError):
                    continue
            if upstream is None:
                raise OSError
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        established = True
        pumps = [
            asyncio.create_task(relay(reader, upstream)),
            asyncio.create_task(relay(incoming, writer)),
        ]
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    except (
        OSError,
        TimeoutError,
        ValueError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
    ):
        if not established:
            with suppress(OSError):
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                await writer.drain()
    finally:
        for pump in pumps:
            pump.cancel()
        writer.close()
        if upstream is not None:
            upstream.close()
        await asyncio.gather(*pumps, return_exceptions=True)


@asynccontextmanager
async def platform_proxy(platform: Platform) -> AsyncIterator[str]:
    connections: set[asyncio.Task[None]] = set()

    def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(tunnel(reader, writer, platform))
        connections.add(task)
        task.add_done_callback(connections.discard)

    server = await asyncio.start_server(connect, "127.0.0.1", 0, limit=8192)
    try:
        async with server:
            yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    finally:
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
