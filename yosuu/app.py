import asyncio
import os
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import unquote
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Receive, Scope, Send

from .contracts import Inspection, JobRequest, JobView, LinkRequest, MediaError
from .media import Source, convert, download_source, inspect_file, inspect_link
from .platforms import canonical_link
from .processes import MAX_INPUT

TTL = 15 * 60
MAX_ENTRIES = 12
MAX_JOBS = 4
DIST = Path(__file__).resolve().parents[1] / "frontend" / "dist"


@dataclass
class StoredSource:
    source: Source
    folder: Path
    expires: float
    users: int = 0


@dataclass
class Job:
    view: JobView
    folder: Path
    filename: str
    expires: float
    task: asyncio.Task[None] | None = None
    file: Path | None = None
    downloads: int = 0


class JobDownload(FileResponse):
    def __init__(self, job: Job, file: Path):
        super().__init__(
            file,
            filename=job.filename,
            media_type="video/mp4" if file.suffix == ".mp4" else "audio/mpeg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
        self.job = job

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.job.downloads += 1
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.job.downloads -= 1


class Workbench:
    def __init__(self, root: Path):
        self.root = root
        self.sources: dict[str, StoredSource] = {}
        self.jobs: dict[str, Job] = {}
        self.inspections = 0
        self.worker = asyncio.Semaphore(1)
        self.platforms = {
            platform.strip().lower()
            for platform in os.environ.get("YOSUU_LINK_PLATFORMS", "").split(",")
        } & {"youtube", "instagram", "tiktok"}

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(15)
            now = monotonic()
            for key, entry in list(self.sources.items()):
                if entry.users == 0 and entry.expires <= now:
                    del self.sources[key]
                    shutil.rmtree(entry.folder, ignore_errors=True)
            for key, job in list(self.jobs.items()):
                if (
                    job.downloads == 0
                    and job.view.status in {"ready", "failed", "cancelled"}
                    and job.expires <= now
                ):
                    del self.jobs[key]
                    shutil.rmtree(job.folder, ignore_errors=True)

    @asynccontextmanager
    async def inspection(self) -> AsyncIterator[Path]:
        if self.inspections >= 2 or self.inspections + len(self.sources) >= MAX_ENTRIES:
            raise HTTPException(
                429,
                "The workbench is full. Wait for an inspection or for files to expire.",
            )
        folder = self.root / uuid4().hex
        folder.mkdir()
        self.inspections += 1
        try:
            yield folder
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        finally:
            self.inspections -= 1

    def store(self, source: Source, folder: Path) -> Inspection:
        self.sources[source.view.id] = StoredSource(source, folder, monotonic() + TTL)
        return source.view

    async def execute(self, entry: StoredSource, job: Job, request: JobRequest) -> None:
        try:
            async with self.worker:
                job.view.status = "running"
                file = entry.source.file
                if file is None:
                    job.view.stage = "Fetching source"
                    file = await download_source(
                        entry.source, request.format, request.quality_id, job.folder
                    )
                job.view.stage = "Converting"
                job.file = await convert(
                    file, request.format, request.quality_id, job.folder
                )
                job.view.status = "ready"
                job.view.stage = "Ready to download"
                job.view.download_url = f"/api/jobs/{job.view.id}/download"
        except asyncio.CancelledError:
            job.view.status = "cancelled"
            job.view.stage = "Cancelled"
            raise
        except MediaError as exc:
            job.view.status = "failed"
            job.view.stage = "Processing failed"
            job.view.error = str(exc)
        except Exception:
            job.view.status = "failed"
            job.view.stage = "Processing failed"
            job.view.error = "The media could not be processed."
        finally:
            entry.users -= 1
            job.expires = monotonic() + TTL
            for file in job.folder.iterdir():
                if file != job.file:
                    file.unlink(missing_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    with TemporaryDirectory(prefix="yosuu-") as directory:
        workbench = Workbench(Path(directory))
        app.state.workbench = workbench
        reaper = asyncio.create_task(workbench.reap())
        try:
            yield
        finally:
            tasks = [
                reaper,
                *(job.task for job in workbench.jobs.values() if job.task is not None),
            ]
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="Yosuu", lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=os.environ.get("YOSUU_HOSTS", "localhost,127.0.0.1,[::1]").split(","),
)


@app.middleware("http")
async def protect_browser_requests(request: Request, call_next):
    origin = request.headers.get("origin")
    if request.method in {"POST", "DELETE"} and (
        request.headers.get("sec-fetch-site") == "cross-site"
        or (
            origin is not None
            and origin
            not in {
                f"http://{request.headers['host']}",
                f"https://{request.headers['host']}",
            }
        )
    ):
        return JSONResponse(
            {"detail": "Use the Yosuu workbench to start a job."}, status_code=403
        )
    return await call_next(request)


@app.exception_handler(MediaError)
async def media_error(_request: Request, error: MediaError) -> JSONResponse:
    return JSONResponse({"detail": str(error)}, status_code=422)


@app.get("/api/config")
async def configuration(request: Request):
    return {
        "link_platforms": sorted(request.app.state.workbench.platforms),
        "max_upload_mib": MAX_INPUT // 1048576,
    }


@app.post("/api/inspect/link", response_model=Inspection)
async def link(body: LinkRequest, request: Request):
    platform, url = canonical_link(body.url)
    state: Workbench = request.app.state.workbench
    if platform not in state.platforms:
        raise HTTPException(
            403,
            "Link processing for this platform is disabled. Choose a media file instead.",
        )
    async with state.inspection() as folder:
        return state.store(await inspect_link(platform, url, folder), folder)


@app.post("/api/inspect/file", response_model=Inspection)
async def upload(request: Request):
    state: Workbench = request.app.state.workbench
    name = Path(
        unquote(request.headers.get("x-media-name", "Media")).replace("\\", "/")
    ).stem
    title = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:180] or "Media"
    async with state.inspection() as folder:
        file = folder / "upload"
        size = 0
        try:
            async with asyncio.timeout(90):
                with file.open("wb") as target:
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > MAX_INPUT:
                            raise HTTPException(413, "Use a file of 200 MiB or less.")
                        target.write(chunk)
        except TimeoutError as exc:
            raise HTTPException(408, "Upload timed out. Try a smaller file.") from exc
        if size == 0:
            raise MediaError("Choose a media file first.")
        return state.store(await inspect_file(file, title), folder)


@app.post("/api/jobs", response_model=JobView, status_code=202)
async def create_job(body: JobRequest, request: Request):
    state: Workbench = request.app.state.workbench
    entry = state.sources.get(body.source_id)
    if entry is None or entry.expires <= monotonic():
        raise HTTPException(404, "This source expired. Inspect it again.")
    options = (
        entry.source.view.video if body.format == "mp4" else entry.source.view.audio
    )
    if body.quality_id not in {quality.id for quality in options}:
        raise MediaError("Choose a quality available for this source.")
    active = sum(
        job.view.status in {"queued", "running"} for job in state.jobs.values()
    )
    if active >= MAX_JOBS or len(state.jobs) >= MAX_ENTRIES:
        raise HTTPException(
            429, "The queue is full. Try again after a job finishes or expires."
        )
    key = uuid4().hex
    folder = state.root / key
    folder.mkdir()
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", entry.source.view.title).strip(
        " ."
    )[:100]
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", title, flags=re.IGNORECASE):
        title = f"Yosuu-{title}"
    job = Job(
        JobView(id=key, status="queued", stage="In queue"),
        folder,
        f"{title or 'media'}.{body.format}",
        0,
    )
    state.jobs[key] = job
    entry.users += 1
    entry.expires = monotonic() + TTL
    job.task = asyncio.create_task(state.execute(entry, job, body))
    await asyncio.sleep(0)
    return job.view


def get_job(request: Request, key: str) -> Job:
    job = request.app.state.workbench.jobs.get(key)
    if job is None or (
        job.view.status in {"ready", "failed", "cancelled"}
        and job.expires <= monotonic()
    ):
        raise HTTPException(404, "This job expired. Start a new one.")
    return job


@app.get("/api/jobs/{key}", response_model=JobView)
async def job_status(key: str, request: Request):
    return get_job(request, key).view


@app.delete("/api/jobs/{key}", response_model=JobView)
async def cancel_job(key: str, request: Request):
    job = get_job(request, key)
    if job.task and job.view.status in {"queued", "running"}:
        if not job.task.cancelling():
            job.task.cancel()
        await asyncio.gather(asyncio.shield(job.task), return_exceptions=True)
    return job.view


@app.get("/api/jobs/{key}/download")
async def download(key: str, request: Request):
    job = get_job(request, key)
    if job.view.status != "ready" or job.file is None:
        raise HTTPException(409, "The file is not ready yet.")
    return JobDownload(job, job.file)


if DIST.is_dir():
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
