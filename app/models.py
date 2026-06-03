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
            "TakeoffAssistFiles/Bids/.  Spaces are replaced with underscores; invalid "
            "SMB/macOS/Windows characters are stripped."
        ),
    )


class ProjectFolderResponse(BaseModel):
    normalized_name: str = Field(..., description="Normalised folder name actually used on disk")
    dirs: list[MkdirResult] = Field(..., description="One entry per directory created or verified")
