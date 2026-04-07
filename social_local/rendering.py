from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
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
    lines.append(f"# {context.document_title}")
    lines.append("")
    lines.append(f"- Generated at: {format_dt(datetime.now().astimezone())}")
    lines.append(f"- Source: `{context.source_label}`")
    if context.database_name:
        lines.append(f"- IndexedDB database: `{context.database_name}`")
    if context.selected_filters:
        lines.append(
            "- Capture filter: "
            + ", ".join(f"`{name}`" for name in sorted(context.selected_filters))
        )
    lines.append(
        f"- {context.record_label_title} author: @{escape_inline(record.author_handle)}"
    )
    lines.append(
        f"- {context.record_label_title} created at: {format_dt(record.created_at)}"
    )
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
    lines.append(f"# {context.document_title}")
    lines.append("")
    lines.append(f"- Generated at: {format_dt(datetime.now().astimezone())}")
    lines.append(f"- Source: `{context.source_label}`")
    if context.database_name:
        lines.append(f"- IndexedDB database: `{context.database_name}`")
    if context.selected_filters:
        lines.append(
            "- Capture filter: "
            + ", ".join(f"`{name}`" for name in sorted(context.selected_filters))
        )
    lines.append(f"- {context.record_label_plural.title()} count: {len(records)}")

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

    lines.append(f"- {context.record_label_title} ID: `{record.record_id}`")
    lines.append(
        f"- Author: {escape_inline(record.author_name)} (@{escape_inline(record.author_handle)})"
    )
    if record.url:
        lines.append(f"- URL: {record.url}")
    if record.captured_by:
        lines.append(
            "- Captured by: "
            + ", ".join(f"`{name}`" for name in sorted(record.captured_by))
        )
    if record.first_captured_at:
        lines.append(f"- First captured at: {format_dt(record.first_captured_at)}")
    if record.stats_line:
        lines.append(f"- Stats: {record.stats_line}")
    if record.reply_url:
        lines.append(f"- In reply to: {record.reply_url}")
    if record.repost_source_handle and record.repost_source_url:
        repost_label = str(record.extra.get("repost_label") or "Repost source")
        lines.append(
            f"- {repost_label}: "
            f"@{escape_inline(record.repost_source_handle)} — {record.repost_source_url}"
        )

    lines.append("")
    lines.append(record.body_markdown or "_No text content available._")
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
    if media.local_markdown_path:
        link_label = escape_link_label(
            media.filename or Path(media.local_markdown_path).name
        )
        line = f"- {media.media_type}: [{link_label}]({media.local_markdown_path})"
        if media.url:
            line += f" · source: {media.url}"
    elif media.url:
        line = f"- {media.media_type}: {media.url}"
    else:
        line = f"- {media.media_type}"

    if media.alt_text:
        line += f" · alt: {escape_inline(media.alt_text)}"
    if media.download_error:
        line += f" · download failed: {escape_inline(media.download_error)}"
    return line


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
    lines.append(f"# {context.document_title}")
    lines.append("")
    lines.append(f"- Generated at: {format_dt(datetime.now().astimezone())}")
    lines.append(f"- Source: `{context.source_label}`")
    if context.database_name:
        lines.append(f"- IndexedDB database: `{context.database_name}`")
    if context.selected_filters:
        lines.append(
            "- Capture filter: "
            + ", ".join(f"`{name}`" for name in sorted(context.selected_filters))
        )
    lines.append(f"- Selected {context.record_label_plural} this run: {selected_count}")
    lines.append(f"- Newly written this run: {written_count}")
    lines.append(f"- Skipped by history: {skipped_count}")
    lines.append(f"- Archived {context.record_label_plural}: {len(visible_entries)}")

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
