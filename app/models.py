from __future__ import annotations
from typing import Literal, Optional
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: Literal["healthy", "degraded"]
    root_path: str
    root_exists: bool
    root_readable: bool
    version: str
    timestamp: str


class DirectoryEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size_bytes: Optional[int] = None
    modified: str


class ListResponse(BaseModel):
    path: str
    entries: list[DirectoryEntry]
    count: int


class FileMetadata(BaseModel):
    path: str
    name: str
    is_dir: Literal[False]
    size_bytes: int
    extension: str
    content_type: str
    modified: str


class DirectoryMetadata(BaseModel):
    path: str
    name: str
    is_dir: Literal[True]
    children_count: int
    modified: str


class ErrorResponse(BaseModel):
    detail: str


class WriteTestRequest(BaseModel):
    filename: str = Field(..., description="Filename to create under TakeoffAssistFiles/ (no path separators)")
    contents: str = Field(..., description="UTF-8 text content to write")


class WriteTestResponse(BaseModel):
    success: bool
    bytes_written: int
    read_back_verified: bool
    path: str = Field(..., description="Path relative to Jobs root, e.g. /TakeoffAssistFiles/smoke-test.txt")


class MkdirRequest(BaseModel):
    rel_path: str = Field(
        ...,
        description=(
            "Path relative to TakeoffAssistFiles/ to create, e.g. 'Bids/JobName' or "
            "'Bids/JobName/Plans'.  No leading slash.  No path traversal."
        ),
    )


class MkdirResult(BaseModel):
    path: str = Field(..., description="Absolute path relative to Jobs root, e.g. /TakeoffAssistFiles/Bids/JobName")
    created: bool
    already_existed: bool


class MkdirResponse(MkdirResult):
    pass


class ProjectFolderRequest(BaseModel):
    folder_name: str = Field(
        ...,
        description=(
            "Raw project name to normalise and use as the top-level folder name inside "
            "TakeoffAssistFiles/<zone>/Bids/.  Spaces are replaced with underscores; invalid "
            "SMB/macOS/Windows characters are stripped."
        ),
    )
    zone: Literal["dev", "prod"] = Field(
        "dev",
        description=(
            "Target zone subfolder within TakeoffAssistFiles/. "
            "'dev' for the Replit development environment; "
            "'prod' for the production pilot deployment. "
            "Determined by the API server's NAS_ZONE env var — never client-controlled."
        ),
    )
    bid_id: Optional[int] = Field(
        None,
        description="InboundBid database ID — written into the workspace manifest.",
    )
    project_name: Optional[str] = Field(
        None,
        description="Human-readable project name (pre-normalisation) — written into the workspace manifest.",
    )


class ProjectFolderResponse(BaseModel):
    normalized_name: str = Field(..., description="Normalised folder name actually used on disk")
    zone: str = Field(..., description="Zone that was written to ('dev' or 'prod')")
    dirs: list[MkdirResult] = Field(..., description="One entry per directory created or verified")
    manifest_written: bool = Field(..., description="True if manifest was newly created; False if it already existed or write failed")
    manifest_path: Optional[str] = Field(None, description="Path to the manifest file relative to Jobs root, or null if not written")
