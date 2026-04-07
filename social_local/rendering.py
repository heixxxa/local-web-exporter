from __future__ import annotations

import html
from dataclasses import replace
from datetime import datetime
from typing import Any, Sequence

from .models import ExportContext, ExportRecord, MediaAsset


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
