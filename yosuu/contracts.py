from typing import Literal

from pydantic import BaseModel, Field

Platform = Literal["youtube", "instagram", "tiktok"]
OutputFormat = Literal["mp4", "mp3"]
JobStatus = Literal["queued", "running", "ready", "failed", "cancelled"]


class MediaError(Exception):
    pass


class LinkRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    authorized: Literal[True]


class Quality(BaseModel):
    id: str
    label: str
    detail: str


class Inspection(BaseModel):
    id: str
    title: str
    source: Platform | Literal["file"]
    duration: float | None
    video: list[Quality]
    audio: list[Quality]


class JobRequest(BaseModel):
    source_id: str
    format: OutputFormat
    quality_id: str


class JobView(BaseModel):
    id: str
    status: JobStatus
    stage: str
    error: str | None = None
    download_url: str | None = None
