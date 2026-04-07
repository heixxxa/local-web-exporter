from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class MediaAsset:
    url: str
    media_type: str = "media"
    alt_text: str = ""
    filename: str = ""
    local_path: str = ""
    local_markdown_path: str = ""
    download_error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class QuoteRecord:
    author_handle: str
    text: str
    created_at: datetime | None = None
    url: str | None = None


@dataclass(slots=True)
class ExportRecord:
    record_id: str
    author_handle: str
    author_name: str
    created_at: datetime
    updated_at: datetime
    body_markdown: str
    url: str | None = None
    captured_by: list[str] = field(default_factory=list)
    first_captured_at: datetime | None = None
    stats_line: str | None = None
    reply_url: str | None = None
    repost_source_handle: str | None = None
    repost_source_url: str | None = None
    quote: QuoteRecord | None = None
    media: list[MediaAsset] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlatformPayload:
    database_names: list[str]
    tables: dict[str, list[dict[str, Any]]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SelectionResult:
    records: list[ExportRecord]
    selected_filters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExportContext:
    platform_key: str
    platform_name: str
    record_label_singular: str
    record_label_plural: str
    document_title: str
    source_label: str
    database_name: str | None
    selected_filters: list[str]

    @property
    def record_label_title(self) -> str:
        return self.record_label_singular.title()
