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


from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from urllib.parse import parse_qs, urlparse


class PlatformAdapter(ABC):
    key = ""
    display_name = ""
    record_label_singular = "record"
    record_label_plural = "records"
    supported_origins: tuple[str, ...] = ()
    probe_paths: tuple[str, ...] = ("/robots.txt", "/", "/home")
    store_names: tuple[str, ...] = ()

    def add_arguments(self, parser: ArgumentParser) -> None:
        del parser

    def browser_read_options(
        self, args: Namespace, existing_ids: set[str]
    ) -> dict[str, Any]:
        return {}

    def default_document_title(self) -> str:
        return f"{self.display_name} Export Restore"

    def default_db_prefix(self) -> str:
        return self.key

    def resolve_document_title(self, value: str | None) -> str:
        return value or self.default_document_title()

    def resolve_db_prefix(self, value: str | None) -> str:
        return value or self.default_db_prefix()

    def iter_probe_urls(self, origin: str | None) -> list[str]:
        domains = self._resolve_origins(origin)
        urls: list[str] = []
        for domain in domains:
            if "://" in domain:
                parsed = urlparse(domain)
                base = f"{parsed.scheme}://{parsed.netloc}"
                if parsed.path and parsed.path not in {"", "/"}:
                    urls.append(domain.rstrip("/"))
            else:
                base = f"https://{domain.strip('/')}"

            for path in self.probe_paths:
                if path == "/":
                    urls.append(base + "/")
                else:
                    urls.append(base + path)
        return dedupe_preserve_order(urls)

    def build_context(
        self,
        *,
        document_title: str,
        source_label: str,
        database_name: str | None,
        selected_filters: list[str],
    ) -> ExportContext:
        return ExportContext(
            platform_key=self.key,
            platform_name=self.display_name,
            record_label_singular=self.record_label_singular,
            record_label_plural=self.record_label_plural,
            document_title=document_title,
            source_label=source_label,
            database_name=database_name,
            selected_filters=selected_filters,
        )

    @abstractmethod
    def aggregate_browser_databases(
        self,
        databases: list[dict[str, Any]],
    ) -> PlatformPayload:
        raise NotImplementedError

    @abstractmethod
    def load_export_payload(self, root: Any) -> PlatformPayload:
        raise NotImplementedError

    @abstractmethod
    def select_records(
        self,
        payload: PlatformPayload,
        args: Namespace,
    ) -> SelectionResult:
        raise NotImplementedError

    def _resolve_origins(self, origin: str | None) -> list[str]:
        if not origin or origin == "auto":
            return list(self.supported_origins)
        return [origin]


def dedupe_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


import html
from collections.abc import Sequence
from dataclasses import replace


def format_dt(value: datetime | None) -> str:
    if not value:
        return "1970-01-01 00:00:00"
    return value.strftime("%Y-%m-%d %H:%M:%S %z")


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.fromtimestamp(0).astimezone()


def record_sort_key(record: ExportRecord, sort_by: str) -> tuple[float, str]:
    if sort_by == "captured":
        target = record.first_captured_at
    elif sort_by == "updated":
        target = record.updated_at
    else:
        target = record.created_at
    return (target.timestamp() if target else 0.0, record.record_id)


def render_single_record_markdown(
    *,
    context: ExportContext,
    record: ExportRecord,
    media_records: Sequence[MediaAsset] | None = None,
) -> str:
    lines: list[str] = []
    metadata: dict[str, Any] = {
        "document_title": context.document_title,
        "generated_at": format_dt(datetime.now().astimezone()),
        "source": context.source_label,
        "record_type": context.record_label_singular,
        "record_id": record.record_id,
        "author_handle": record.author_handle,
        "author_name": record.author_name,
        "created_at": format_dt(record.created_at),
    }
    if context.database_name:
        metadata["indexeddb_database"] = context.database_name
    if context.selected_filters:
        metadata["capture_filter"] = sorted(context.selected_filters)
    if record.url:
        metadata["url"] = record.url
    if record.captured_by:
        metadata["captured_by"] = sorted(record.captured_by)
    if record.first_captured_at:
        metadata["first_captured_at"] = format_dt(record.first_captured_at)
    if record.reply_url:
        metadata["reply_url"] = record.reply_url
    lines.extend(render_yaml_front_matter(metadata))
    lines.append("")
    lines.extend(
        render_record_lines(
            context=context,
            record=record,
            heading_level=1,
            media_records=media_records,
        )
    )
    lines.append("")
    return "\n".join(lines)


def render_records_markdown(
    *,
    context: ExportContext,
    records: Sequence[ExportRecord],
) -> str:
    lines: list[str] = []
    metadata: dict[str, Any] = {
        "document_title": context.document_title,
        "generated_at": format_dt(datetime.now().astimezone()),
        "source": context.source_label,
        "record_type": context.record_label_plural,
        "record_count": len(records),
    }
    if context.database_name:
        metadata["indexeddb_database"] = context.database_name
    if context.selected_filters:
        metadata["capture_filter"] = sorted(context.selected_filters)
    lines.extend(render_yaml_front_matter(metadata))

    if not records:
        lines.append("")
        lines.append("_No records matched the current filter._")
        lines.append("")
        return "\n".join(lines)

    for index, record in enumerate(records, start=1):
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.extend(
            render_record_lines(
                context=context,
                record=record,
                heading_level=2,
                heading_text=f"{index}. {format_dt(record.created_at)} — @{record.author_handle}",
            )
        )

    lines.append("")
    return "\n".join(lines)


def render_record_lines(
    *,
    context: ExportContext,
    record: ExportRecord,
    heading_level: int,
    heading_text: str | None = None,
    media_records: Sequence[MediaAsset] | None = None,
) -> list[str]:
    lines: list[str] = []
    if heading_text:
        lines.extend([f"{'#' * heading_level} {heading_text}", ""])

    lines.append(record.body_markdown or "_No text content available._")
    if record.stats_line:
        lines.append("")
        lines.append(f"> Stats: {record.stats_line}")
    if record.repost_source_handle and record.repost_source_url:
        repost_label = str(record.extra.get("repost_label") or "Repost source")
        lines.append("")
        lines.append(
            f"> {repost_label}: "
            f"@{escape_inline(record.repost_source_handle)} - {record.repost_source_url}"
        )
    lines.extend(render_related_comments_lines(record, heading_level + 1))
    lines.extend(render_media_lines(media_records or record.media, heading_level + 1))

    if record.quote:
        lines.append("")
        quote_heading = str(record.extra.get("quote_heading") or "Quoted Post")
        lines.append(f"{'#' * (heading_level + 1)} {quote_heading}")
        lines.append("")
        header = f"> @{record.quote.author_handle}"
        if record.quote.created_at:
            header += f" · {format_dt(record.quote.created_at)}"
        lines.append(header)
        lines.append(">")
        for quote_line in (
            record.quote.text or "No text content available."
        ).splitlines() or [""]:
            lines.append(f"> {quote_line}")
        if record.quote.url:
            lines.append(">")
            lines.append(f"> {record.quote.url}")

    return lines


def render_related_comments_lines(
    record: ExportRecord,
    heading_level: int,
) -> list[str]:
    raw_comments = record.extra.get("related_comments")
    if not isinstance(raw_comments, list) or not raw_comments:
        return []

    lines = ["", f"{'#' * heading_level} Comments", ""]
    for comment in raw_comments:
        if not isinstance(comment, dict):
            continue

        handle = escape_inline(str(comment.get("author_handle") or "unknown"))
        name = escape_inline(str(comment.get("author_name") or handle))
        content = str(comment.get("content") or "").strip()
        like_count = safe_int(comment.get("like_count"), 0)

        timestamp = comment.get("upload_time")
        time_text = (
            format_dt(timestamp)
            if isinstance(timestamp, datetime)
            else "1970-01-01 00:00:00"
        )

        lines.append(f"- {name} (@{handle}) · {time_text} · ❤ {like_count}")
        if content:
            for line in content.splitlines() or [""]:
                lines.append(f"  {line}")
        else:
            lines.append("  _No text content available._")

    return lines


def render_media_lines(
    media_records: Sequence[MediaAsset],
    heading_level: int,
) -> list[str]:
    if not media_records:
        return []

    lines = ["", f"{'#' * heading_level} Media", ""]
    for media in media_records:
        lines.append(format_media_asset(media))
    return lines


def format_media_asset(media: MediaAsset) -> str:
    media_kind = media.media_type.lower()
    media_label = html.escape(media.media_type)
    display_src = media.local_markdown_path or media.url
    escaped_src = html.escape(display_src) if display_src else ""
    alt_text = html.escape(media.alt_text or media.filename or media.media_type)
    parts: list[str] = [f'<figure class="media-item" data-media-type="{media_label}">']

    if display_src and "video" in media_kind:
        parts.append(f'  <video controls src="{escaped_src}"></video>')
    elif display_src and (
        "image" in media_kind or "photo" in media_kind or "gif" in media_kind
    ):
        parts.append(f'  <img src="{escaped_src}" alt="{alt_text}" loading="lazy" />')
    elif display_src:
        parts.append(
            f'  <a href="{escaped_src}" target="_blank" rel="noopener noreferrer">{alt_text}</a>'
        )
    else:
        parts.append(f"  <figcaption>{media_label}</figcaption>")

    metadata: list[str] = []
    if media.url and media.local_markdown_path:
        metadata.append(
            f'source: <a href="{html.escape(media.url)}" target="_blank" rel="noopener noreferrer">{html.escape(media.url)}</a>'
        )
    if media.alt_text:
        metadata.append(f"alt: {html.escape(media.alt_text)}")
    if media.download_error:
        metadata.append(f"download failed: {html.escape(media.download_error)}")
    if metadata:
        parts.append(f"  <figcaption>{' | '.join(metadata)}</figcaption>")

    parts.append("</figure>")
    return "\n".join(parts)


def render_yaml_front_matter(metadata: dict[str, Any]) -> list[str]:
    lines = ["---"]
    for key, value in metadata.items():
        lines.extend(render_yaml_key_value(key, value))
    lines.append("---")
    return lines


def render_yaml_key_value(key: str, value: Any) -> list[str]:
    if isinstance(value, list):
        if not value:
            return [f"{key}: []"]
        lines = [f"{key}:"]
        for item in value:
            lines.append(f"  - {yaml_scalar(item)}")
        return lines
    return [f"{key}: {yaml_scalar(value)}"]


def yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)

    text = str(value).replace("\r\n", "\\n").replace("\n", "\\n")
    escaped = text.replace("'", "''")
    return f"'{escaped}'"


def with_local_markdown_path(media: MediaAsset, markdown_path: str) -> MediaAsset:
    return replace(media, local_markdown_path=markdown_path)


def render_export_index(
    *,
    context: ExportContext,
    history_entries: list[dict[str, Any]],
    selected_count: int,
    written_count: int,
    skipped_count: int,
    sort_by: str,
    descending: bool,
) -> str:
    visible_entries = [entry for entry in history_entries if isinstance(entry, dict)]
    visible_entries.sort(
        key=lambda entry: history_entry_sort_key(entry, sort_by),
        reverse=descending,
    )

    lines: list[str] = []
    metadata: dict[str, Any] = {
        "document_title": context.document_title,
        "generated_at": format_dt(datetime.now().astimezone()),
        "source": context.source_label,
        "record_type": context.record_label_plural,
        "selected_this_run": selected_count,
        "newly_written_this_run": written_count,
        "skipped_by_history": skipped_count,
        "archived_total": len(visible_entries),
    }
    if context.database_name:
        metadata["indexeddb_database"] = context.database_name
    if context.selected_filters:
        metadata["capture_filter"] = sorted(context.selected_filters)
    lines.extend(render_yaml_front_matter(metadata))

    if not visible_entries:
        lines.append("")
        lines.append("_No archived content is available yet._")
        lines.append("")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"## Archived {context.record_label_plural.title()}")
    lines.append("")
    for entry in visible_entries:
        lines.append(render_index_entry(entry))

    lines.append("")
    return "\n".join(lines)


def history_entry_sort_key(entry: dict[str, Any], sort_by: str) -> tuple[float, str]:
    if sort_by == "captured":
        value = parse_datetime(entry.get("first_captured_at"))
    elif sort_by == "updated":
        value = parse_datetime(entry.get("updated_at"))
    else:
        value = parse_datetime(entry.get("created_at"))
    return (value.timestamp(), str(entry.get("record_id") or ""))


def render_index_entry(entry: dict[str, Any]) -> str:
    created_at = format_dt(parse_datetime(entry.get("created_at")))
    author_handle = str(entry.get("author_handle") or "unknown")
    author_name = str(entry.get("author_name") or author_handle)
    record_id = str(entry.get("record_id") or "")
    markdown_path = str(entry.get("markdown_path") or "")
    media_count = safe_int(entry.get("media_count"), 0)
    media_status = ""
    if media_count > 0:
        media_status = (
            f" · {media_count} media"
            if entry.get("media_exported")
            else f" · {media_count} media pending"
        )

    label = escape_link_label(f"{created_at} — @{author_handle}")
    line = (
        f"- [{label}]({markdown_path}) · {escape_inline(author_name)} · `{record_id}`"
    )
    url = str(entry.get("url") or "").strip()
    if url:
        line += f" · {url}"
    line += media_status
    return line


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def escape_link_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("]", "\\]")


def escape_inline(value: str) -> str:
    return value.replace("\n", " ").strip()


import json
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from pathlib import Path
from urllib.request import Request, urlopen

from .browser import extract_indexeddb_payload

DEFAULT_MEDIA_FILENAME_PATTERN = "{screen_name}_{id}_{type}_{num}_{date}.{ext}"

LEGACY_PLATFORM_ALIASES: dict[str, str] = {
    "twitter": "x",
    "xiaohongshu": "xhs",
    "okjike": "jike",
}

PLATFORM_OUTPUT_FOLDER_NAMES: dict[str, str] = {
    "x": "x",
    "xhs": "xiaohongshu",
    "jike": "okjike",
}


def parse_multi_values(values: Iterable[str] | None) -> list[str]:
    parsed: list[str] = []
    for value in values or []:
        for item in value.split(","):
            cleaned = item.strip()
            if cleaned:
                parsed.append(cleaned)
    return parsed


def resolve_output_paths(
    adapter: PlatformAdapter, args: Any
) -> tuple[Path, Path, Path, Path]:
    output_arg_raw = str(args.output_md or "")
    output_arg = Path(output_arg_raw)

    # If -o points to a directory-like path, create a platform folder and default file names in it.
    is_directory_mode = (
        output_arg_raw.endswith(("/", "\\")) or output_arg.suffix.lower() != ".md"
    )

    if is_directory_mode:
        platform_folder = PLATFORM_OUTPUT_FOLDER_NAMES.get(adapter.key, adapter.key)
        target_dir = output_arg / platform_folder
        output_md = target_dir / f"{platform_folder}.md"
        history_path = target_dir / f"{platform_folder}.history.json"
        record_dir = target_dir / f"{platform_folder}_{adapter.record_label_plural}"
        media_dir = target_dir / f"{platform_folder}_media"
        return output_md, history_path, record_dir, media_dir

    output_md = output_arg
    history_path = output_md.with_suffix(".history.json")
    record_dir = output_md.with_name(f"{output_md.stem}_{adapter.record_label_plural}")
    media_dir = output_md.with_name(f"{output_md.stem}_media")
    return output_md, history_path, record_dir, media_dir


def run_indexeddb_export(adapter: PlatformAdapter, args: Any) -> int:
    output_md, default_history_path, default_record_dir, default_media_dir = (
        resolve_output_paths(adapter, args)
    )
    output_root = output_md.parent
    record_dir_arg = getattr(args, "record_dir", None) or getattr(
        args, "tweet_dir", None
    )
    record_dir = Path(record_dir_arg) if record_dir_arg else default_record_dir
    history_path = (
        Path(args.history_file) if args.history_file else default_history_path
    )
    media_dir = Path(args.media_dir) if args.media_dir else default_media_dir
    output_json = Path(args.output_json) if args.output_json else None
    document_title = adapter.resolve_document_title(args.title)
    db_prefix = adapter.resolve_db_prefix(args.db_prefix)
    db_names = parse_multi_values(args.db_name)

    history = (
        empty_export_history()
        if args.ignore_history
        else load_export_history(history_path)
    )
    history_entries = history.setdefault("records", {})
    should_export_media = args.export_media or args.run_aria2
    should_prepare_media = should_export_media or bool(args.aria2_input_file)
    existing_ids = {
        key.partition(":")[2]
        for key, entry in history_entries.items()
        if isinstance(entry, dict)
        and entry.get("platform") == adapter.key
        and markdown_artifact_exists(entry, output_root)
        and (
            not should_export_media
            or (entry.get("media_exported") and media_artifacts_exist(entry, output_root))
        )
    }

    extracted = extract_indexeddb_payload(
        adapter=adapter,
        edge_user_data_dir=Path(args.edge_user_data_dir),
        edge_profile_directory=args.edge_profile_directory,
        edge_executable_path=Path(args.edge_executable_path)
        if args.edge_executable_path
        else None,
        origin=args.origin,
        db_prefix=db_prefix,
        db_names=db_names,
        timeout_ms=args.timeout_ms,
        headed=args.headed,
        copy_indexeddb=args.copy_indexeddb,
        keep_temp_profile=args.keep_temp_profile,
        read_options=adapter.browser_read_options(args, existing_ids)
        if not output_json else {},
    )

    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(extracted, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    payload = adapter.aggregate_browser_databases(extracted.get("databases", []))
    selection = adapter.select_records(payload, args)
    records = list(selection.records)
    records.sort(
        key=lambda record: record_sort_key(record, args.sort_by),
        reverse=args.descending,
    )

    database_name = (
        ", ".join(payload.database_names) if payload.database_names else None
    )
    source_label = (
        "Microsoft Edge IndexedDB via Playwright "
        f"({extracted.get('origin') or 'unknown origin'})"
    )
    context = adapter.build_context(
        document_title=document_title,
        source_label=source_label,
        database_name=database_name,
        selected_filters=list(selection.selected_filters),
    )

    records_to_write: list[ExportRecord] = []
    skipped_by_history = len(extracted.get("skipped_ids", []))

    for record in records:
        history_key = build_history_key(adapter, record.record_id)
        history_entry = history_entries.get(history_key)
        needs_markdown = (
            args.ignore_history
            or history_entry is None
            or not markdown_artifact_exists(history_entry, output_root)
        )
        needs_media = should_export_media and (
            args.ignore_history
            or history_entry is None
            or not bool(history_entry.get("media_exported"))
            or not media_artifacts_exist(history_entry, output_root)
        )
        if needs_media:
            needs_markdown = True

        if needs_markdown or needs_media:
            records_to_write.append(record)
        else:
            skipped_by_history += 1

    media_records = (
        build_media_records(
            records_to_write,
            args.media_filename_pattern or DEFAULT_MEDIA_FILENAME_PATTERN,
        )
        if records_to_write and should_prepare_media
        else []
    )
    assign_media_local_paths(media_records, media_dir)
    media_records_by_record = group_media_records_by_record(media_records)

    if media_records and (args.aria2_input_file or args.run_aria2):
        aria2_input_path = (
            Path(args.aria2_input_file)
            if args.aria2_input_file
            else output_md.with_suffix(".aria2.txt")
        )
        media_dir.mkdir(parents=True, exist_ok=True)
        write_aria2_input_file(
            media_records=media_records,
            output_path=aria2_input_path,
            media_dir=media_dir,
            referer=origin_referer(extracted.get("origin"), adapter),
        )
        print(f"Wrote {len(media_records)} media URLs to {aria2_input_path}")
        if args.run_aria2:
            run_aria2(
                aria2_bin=args.aria2_bin,
                input_file=aria2_input_path,
                max_concurrent_downloads=args.aria2_max_concurrent_downloads,
                split=args.aria2_split,
            )
    elif (args.aria2_input_file or args.run_aria2) and not media_records:
        print(
            "No media URLs found in the records being exported; skipping aria2 output."
        )

    if media_records and args.export_media and not args.run_aria2:
        downloaded_count, reused_count, failed_count = download_media_files(
            media_records=media_records,
            media_dir=media_dir,
            referer=origin_referer(extracted.get("origin"), adapter),
        )
        print(
            "Media download summary: "
            f"{downloaded_count} downloaded, {reused_count} reused, {failed_count} failed."
        )

    output_root.mkdir(parents=True, exist_ok=True)
    record_dir.mkdir(parents=True, exist_ok=True)
    written_count = 0
    for record in records_to_write:
        history_key = build_history_key(adapter, record.record_id)
        previous_entry = history_entries.get(history_key)
        record_path = record_dir / f"{record.record_id}.md"
        record_media = media_records_by_record.get(record.record_id)
        markdown = render_single_record_markdown(
            context=context,
            record=record,
            media_records=prepare_media_records_for_markdown(
                record_media, record_path.parent
            )
            if should_export_media and record_media is not None
            else None,
        )
        record_path.write_text(markdown, encoding="utf-8")
        history_entries[history_key] = build_history_entry(
            adapter=adapter,
            record=record,
            record_path=record_path,
            output_root=output_root,
            media_records=record_media,
            previous_entry=previous_entry,
            media_export_requested=should_export_media,
        )
        written_count += 1

    platform_history_entries = [
        entry
        for entry in history_entries.values()
        if isinstance(entry, dict)
        and entry.get("platform") == adapter.key
        and markdown_artifact_exists(entry, output_root)
    ]
    index_markdown = render_export_index(
        context=context,
        history_entries=platform_history_entries,
        selected_count=len(records) + len(extracted.get("skipped_ids", [])),
        written_count=written_count,
        skipped_count=skipped_by_history,
        sort_by=args.sort_by,
        descending=args.descending,
    )
    output_md.write_text(index_markdown, encoding="utf-8")
    if written_count or not history_path.exists() or args.ignore_history:
        save_export_history(history_path, history)
        print(f"Saved export history to {history_path}")
    print(f"Wrote {adapter.record_label_singular} index to {output_md}")
    print(f"Wrote {written_count} {adapter.record_label_plural} to {record_dir}")
    return 0


def empty_export_history() -> dict[str, Any]:
    return {"version": 2, "records": {}}


def load_export_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_export_history()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid export history file: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        return empty_export_history()

    if isinstance(raw.get("records"), dict):
        migrated = empty_export_history()
        for old_key, entry in raw["records"].items():
            if not isinstance(entry, dict):
                continue
            old_platform, _, old_record_id = str(old_key).partition(":")
            record_id = old_record_id or str(entry.get("record_id") or "")
            if not record_id:
                continue

            entry_copy = dict(entry)
            entry_platform = str(entry_copy.get("platform") or old_platform or "")
            normalized_platform = normalize_platform_key(entry_platform)
            entry_copy["platform"] = normalized_platform
            migrated["records"][
                build_history_key_from_parts(normalized_platform, record_id)
            ] = entry_copy
        migrated["version"] = safe_int(raw.get("version"), 2)
        return migrated

    migrated = empty_export_history()
    legacy_tweets = raw.get("tweets")
    if isinstance(legacy_tweets, dict):
        for tweet_id, entry in legacy_tweets.items():
            if not isinstance(entry, dict):
                continue
            migrated_entry = dict(entry)
            migrated_entry.setdefault("platform", "x")
            migrated_entry.setdefault(
                "record_id", migrated_entry.get("tweet_id") or str(tweet_id)
            )
            migrated_entry.setdefault(
                "author_handle", migrated_entry.get("screen_name")
            )
            migrated_entry.setdefault("author_name", migrated_entry.get("display_name"))
            migrated_entry.pop("tweet_id", None)
            migrated["records"][build_history_key_from_parts("x", str(tweet_id))] = (
                migrated_entry
            )
    return migrated


def save_export_history(path: Path, history: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    history["updated_at"] = datetime.now().astimezone().isoformat()
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def build_history_key(adapter: PlatformAdapter, record_id: str) -> str:
    return build_history_key_from_parts(adapter.key, record_id)


def build_history_key_from_parts(platform: str, record_id: str) -> str:
    return f"{normalize_platform_key(platform)}:{record_id}"


def normalize_platform_key(platform: str) -> str:
    normalized = str(platform or "").strip()
    return LEGACY_PLATFORM_ALIASES.get(normalized, normalized)


def markdown_artifact_exists(entry: dict[str, Any], output_root: Path) -> bool:
    markdown_path = str(entry.get("markdown_path") or "").strip()
    if not markdown_path:
        return False
    return resolve_stored_path(markdown_path, output_root).exists()


def media_artifacts_exist(entry: dict[str, Any], output_root: Path) -> bool:
    media_count = safe_int(entry.get("media_count"), 0)
    if media_count <= 0:
        return True

    media_files = entry.get("media_files")
    if not isinstance(media_files, list) or len(media_files) < media_count:
        return False

    for media_file in media_files:
        if not isinstance(media_file, dict):
            return False
        stored_path = str(media_file.get("path") or "").strip()
        if not stored_path:
            return False
        if not resolve_stored_path(stored_path, output_root).exists():
            return False
    return True


def resolve_stored_path(value: str, output_root: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return output_root / path


def make_stored_path(path: Path, output_root: Path) -> str:
    try:
        return Path(os.path.relpath(path, start=output_root)).as_posix()
    except ValueError:
        return str(path.resolve())


def build_media_records(
    records: Sequence[ExportRecord],
    filename_pattern: str,
) -> list[MediaAsset]:
    records_by_key: dict[tuple[str, str], MediaAsset] = {}
    used_filenames: set[str] = set()

    for record in records:
        for index, media in enumerate(record.media):
            if not media.url:
                continue

            extension = get_file_extension_from_url(
                media.url,
                default="mp4" if media.media_type == "video" else "jpg",
            )
            filename = apply_media_filename_pattern(
                filename_pattern,
                {
                    "id": record.record_id,
                    "author_handle": record.author_handle,
                    "author_name": record.author_name,
                    "screen_name": record.author_handle,
                    "name": record.author_name,
                    "index": str(index),
                    "num": str(index + 1),
                    "date": record.created_at.strftime("%Y%m%d"),
                    "time": record.created_at.strftime("%H%M%S"),
                    "type": media.media_type,
                    "ext": extension,
                },
            )
            filename = uniquify_filename(filename, used_filenames)
            used_filenames.add(filename)
            media_copy = replace(
                media,
                filename=filename,
                extra={
                    **media.extra,
                    "record_id": record.record_id,
                },
            )
            records_by_key[(record.record_id, media.url)] = media_copy

    return list(records_by_key.values())


def get_file_extension_from_url(url: str, default: str = "jpg") -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query_format = next(
        (
            values[0]
            for key in ("format", "image_format", "ext")
            if (values := query.get(key))
        ),
        "",
    )
    path_extension = Path(parsed.path).suffix.removeprefix(".")
    extension = (query_format or path_extension).lower()
    if re.fullmatch(r"[a-z0-9]{1,8}", extension):
        return extension
    return default


def apply_media_filename_pattern(pattern: str, values: dict[str, str]) -> str:
    filename = pattern
    for key, value in values.items():
        filename = filename.replace(f"{{{key}}}", value)
    return sanitize_filename(filename)


def sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", filename).strip()
    cleaned = cleaned.rstrip(" .")
    return cleaned or "media"


def uniquify_filename(filename: str, used_filenames: set[str]) -> str:
    if filename not in used_filenames:
        return filename

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 2
    while True:
        candidate = f"{stem}-{counter}{suffix}"
        if candidate not in used_filenames:
            return candidate
        counter += 1


def assign_media_local_paths(
    media_records: Sequence[MediaAsset],
    media_dir: Path,
) -> None:
    for media in media_records:
        media.local_path = str(media_dir / media.filename)


def group_media_records_by_record(
    media_records: Sequence[MediaAsset],
) -> dict[str, list[MediaAsset]]:
    grouped: dict[str, list[MediaAsset]] = {}
    for media in media_records:
        record_id = str(media.extra.get("record_id") or "").strip()
        if not record_id:
            continue
        grouped.setdefault(record_id, []).append(media)
    return grouped


def download_media_files(
    *,
    media_records: Sequence[MediaAsset],
    media_dir: Path,
    referer: str,
) -> tuple[int, int, int]:
    media_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    reused = 0
    failed = 0

    for media in media_records:
        target_path = Path(media.local_path or str(media_dir / media.filename))
        temp_path = target_path.with_name(f"{target_path.name}.part")
        media.local_path = str(target_path)

        if target_path.exists() and target_path.stat().st_size > 0:
            media.download_error = ""
            reused += 1
            continue

        request = Request(
            media.url,
            headers={
                "Referer": referer,
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urlopen(request) as response, temp_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            temp_path.replace(target_path)
            media.download_error = ""
            downloaded += 1
        except Exception as exc:
            failed += 1
            media.download_error = str(exc)
            if temp_path.exists():
                temp_path.unlink()

    return downloaded, reused, failed


def media_record_is_available(media: MediaAsset) -> bool:
    if not media.local_path or media.download_error:
        return False
    return Path(media.local_path).exists()


def prepare_media_records_for_markdown(
    media_records: Sequence[MediaAsset] | None,
    markdown_parent: Path,
) -> list[MediaAsset] | None:
    if media_records is None:
        return None

    prepared: list[MediaAsset] = []
    for media in media_records:
        if media_record_is_available(media):
            prepared.append(
                replace(
                    media,
                    local_markdown_path=Path(
                        os.path.relpath(media.local_path, start=markdown_parent)
                    ).as_posix(),
                )
            )
        else:
            prepared.append(replace(media))
    return prepared


def build_history_entry(
    *,
    adapter: PlatformAdapter,
    record: ExportRecord,
    record_path: Path,
    output_root: Path,
    media_records: Sequence[MediaAsset] | None,
    previous_entry: dict[str, Any] | None,
    media_export_requested: bool,
) -> dict[str, Any]:
    previous_media_files = (
        previous_entry.get("media_files")
        if isinstance(previous_entry, dict)
        and isinstance(previous_entry.get("media_files"), list)
        else []
    )
    if media_records is None:
        media_count = (
            safe_int(previous_entry.get("media_count"), 0)
            if previous_entry
            else len(record.media)
        )
        media_files = previous_media_files
    else:
        media_count = len(media_records)
        media_files = [
            {
                "filename": media.filename,
                "path": make_stored_path(Path(media.local_path), output_root),
                "url": media.url,
                "type": media.media_type,
            }
            for media in media_records
            if media_record_is_available(media)
        ]

    previous_media_exported = bool(
        previous_entry and previous_entry.get("media_exported")
    )
    media_exported_now = media_count == 0 or (
        media_export_requested and len(media_files) >= media_count
    )
    return {
        "platform": adapter.key,
        "record_id": record.record_id,
        "markdown_path": make_stored_path(record_path, output_root),
        "author_handle": record.author_handle,
        "author_name": record.author_name,
        "url": record.url,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "first_captured_at": record.first_captured_at.isoformat()
        if record.first_captured_at
        else None,
        "exported_at": datetime.now().astimezone().isoformat(),
        "media_count": media_count,
        "media_exported": previous_media_exported or media_exported_now,
        "media_files": media_files,
    }


def write_aria2_input_file(
    *,
    media_records: Sequence[MediaAsset],
    output_path: Path,
    media_dir: Path,
    referer: str,
) -> None:
    lines: list[str] = []
    media_dir_str = str(media_dir)
    for media in media_records:
        lines.append(media.url)
        lines.append(f"  dir={media_dir_str}")
        lines.append(f"  out={media.filename}")
        lines.append(f"  referer={referer}")
        lines.append("  continue=true")
        lines.append("  auto-file-renaming=false")
        lines.append("")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")


def run_aria2(
    *,
    aria2_bin: str,
    input_file: Path,
    max_concurrent_downloads: int,
    split: int,
) -> None:
    command = [
        aria2_bin,
        f"--input-file={input_file}",
        f"--max-concurrent-downloads={max_concurrent_downloads}",
        f"--split={split}",
        "--continue=true",
        "--auto-file-renaming=false",
    ]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"aria2 executable not found: {aria2_bin}") from exc


def origin_referer(origin: str | None, adapter: PlatformAdapter) -> str:
    if origin:
        return origin.rstrip("/") + "/"
    if adapter.supported_origins:
        return f"https://{adapter.supported_origins[0]}/"
    return "https://example.com/"
