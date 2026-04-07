from __future__ import annotations

import argparse
import re
from argparse import Namespace
from datetime import datetime, timezone
from typing import Any

from ..models import ExportRecord, MediaAsset, PlatformPayload, SelectionResult
from .base import PlatformAdapter


class XiaohongshuAdapter(PlatformAdapter):
    key = "xhs"
    display_name = "Xiaohongshu"
    record_label_singular = "post"
    record_label_plural = "posts"
    supported_origins = ("www.xiaohongshu.com", "xiaohongshu.com")
    probe_paths = ("/explore", "/", "/robots.txt")
    store_names = (
        "note",
        "notes",
        "noteCache",
        "note_cache",
        "noteCard",
        "note_card",
        "noteDetail",
        "note_detail",
        "explore",
        "feeds",
        "feed",
        "search",
        "user",
        "users",
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--author-handles",
            help="Comma-separated Xiaohongshu author IDs to keep.",
        )
        parser.add_argument(
            "--keywords",
            help="Comma-separated keywords to filter post text/title.",
        )

    def default_document_title(self) -> str:
        return "Xiaohongshu Export Restore"

    def default_db_prefix(self) -> str:
        return ""

    def aggregate_browser_databases(
        self,
        databases: list[dict[str, Any]],
    ) -> PlatformPayload:
        database_names: list[str] = []
        tables: dict[str, list[dict[str, Any]]] = {}

        for database in databases:
            name = str(database.get("name") or "").strip()
            if name:
                database_names.append(name)

            source_tables = database.get("tables")
            if not isinstance(source_tables, dict):
                continue

            for table_name, rows in source_tables.items():
                if not isinstance(rows, list):
                    continue
                normalized_rows = [row for row in rows if isinstance(row, dict)]
                if not normalized_rows:
                    continue
                materialized = []
                for row in normalized_rows:
                    merged = dict(row)
                    if name:
                        merged.setdefault("__database_name__", name)
                    merged.setdefault("__table_name__", str(table_name))
                    materialized.append(merged)
                tables.setdefault(str(table_name), []).extend(materialized)

        return PlatformPayload(
            database_names=sorted(set(database_names)),
            tables=tables,
            metadata={"table_names": sorted(tables.keys())},
        )

    def load_export_payload(self, root: Any) -> PlatformPayload:
        if isinstance(root, dict) and isinstance(root.get("databases"), list):
            return self.aggregate_browser_databases(root["databases"])

        if isinstance(root, dict) and isinstance(root.get("tables"), dict):
            database_name = str(root.get("database_name") or "").strip()
            database_names = [database_name] if database_name else []
            tables: dict[str, list[dict[str, Any]]] = {}
            for table_name, rows in root["tables"].items():
                if isinstance(rows, list):
                    tables[str(table_name)] = [
                        row for row in rows if isinstance(row, dict)
                    ]
            return PlatformPayload(database_names=database_names, tables=tables)

        if isinstance(root, list):
            rows = [row for row in root if isinstance(row, dict)]
            return PlatformPayload(database_names=[], tables={"notes": rows})

        raise ValueError("Unsupported Xiaohongshu JSON payload format.")

    def select_records(
        self,
        payload: PlatformPayload,
        args: Namespace,
    ) -> SelectionResult:
        author_filters = parse_csv_values(getattr(args, "author_handles", None))
        keyword_filters = parse_csv_values(getattr(args, "keywords", None))
        include_all_records = bool(getattr(args, "all_records", False))

        note_candidates = extract_note_candidates(payload.tables)
        comments_by_note = extract_comments_by_note(payload.tables)
        records_by_id: dict[str, ExportRecord] = {}
        captured_by_map: dict[str, set[str]] = {}

        for note, source_table in note_candidates:
            record_id = get_note_id(note)
            if not record_id:
                continue

            record = build_export_record(note)
            if not include_all_records and not record.body_markdown.strip():
                continue
            if author_filters and record.author_handle not in author_filters:
                continue
            if keyword_filters and not record_matches_keywords(record, keyword_filters):
                continue

            captured_by_map.setdefault(record_id, set()).add(source_table)
            current = records_by_id.get(record_id)
            if current is None:
                records_by_id[record_id] = record
                continue

            candidate_score = record_quality_score(record)
            current_score = record_quality_score(current)
            if candidate_score > current_score or (
                candidate_score == current_score
                and record.updated_at > current.updated_at
            ):
                records_by_id[record_id] = record

        records = list(records_by_id.values())
        for record in records:
            record.captured_by = sorted(captured_by_map.get(record.record_id, set()))
            related_comments = comments_by_note.get(record.record_id, [])
            if related_comments:
                record.extra["related_comments"] = related_comments

        selected_filters = []
        if author_filters:
            selected_filters.extend(
                f"author:{value}" for value in sorted(author_filters)
            )
        if keyword_filters:
            selected_filters.extend(
                f"keyword:{value}" for value in sorted(keyword_filters)
            )

        return SelectionResult(records=records, selected_filters=selected_filters)


def parse_csv_values(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def extract_note_candidates(
    tables: dict[str, list[dict[str, Any]]],
) -> list[tuple[dict[str, Any], str]]:
    candidates: list[tuple[dict[str, Any], str]] = []
    for table_name, rows in tables.items():
        normalized_table_name = table_name.lower()
        if is_comment_table_name(normalized_table_name):
            continue
        for row in rows:
            for note in iter_note_like_dicts(row):
                if looks_like_comment(note):
                    continue
                note_id = get_note_id(note)
                if note_id:
                    candidates.append((note, table_name))
    return candidates


def extract_comments_by_note(
    tables: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    comments_by_note: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()

    for table_name, rows in tables.items():
        normalized_table_name = table_name.lower()
        table_is_comment = is_comment_table_name(normalized_table_name)
        for row in rows:
            for comment in iter_comment_like_dicts(row):
                if not table_is_comment and not looks_like_comment(comment):
                    continue
                normalized = normalize_comment(comment)
                if not normalized:
                    continue

                note_id = str(normalized.get("note_id") or "").strip()
                comment_key = str(normalized.get("comment_id") or "").strip()
                if not comment_key:
                    content_key = str(normalized.get("content") or "").strip()
                    comment_key = f"content:{content_key}"
                unique_key = (note_id, comment_key)
                if unique_key in seen:
                    continue
                seen.add(unique_key)
                comments_by_note.setdefault(note_id, []).append(normalized)

    for note_id, items in comments_by_note.items():
        items.sort(
            key=lambda comment: comment_datetime_sort_key(comment.get("upload_time"))
        )

    return comments_by_note


def is_comment_table_name(table_name: str) -> bool:
    return any(marker in table_name for marker in ("comment", "reply", "capture"))


def iter_note_like_dicts(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    found: list[dict[str, Any]] = []

    if isinstance(value, dict):
        if looks_like_note(value):
            found.append(value)

        for key in (
            "note",
            "note_card",
            "noteCard",
            "note_detail",
            "noteDetail",
            "item",
            "items",
            "data",
            "value",
            "record",
            "list",
        ):
            if key in value:
                found.extend(iter_note_like_dicts(value.get(key), depth + 1))

        for nested in value.values():
            if isinstance(nested, (dict, list, tuple)):
                found.extend(iter_note_like_dicts(nested, depth + 1))

    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(iter_note_like_dicts(item, depth + 1))

    return found


def iter_comment_like_dicts(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    found: list[dict[str, Any]] = []

    if isinstance(value, dict):
        if looks_like_comment(value):
            found.append(value)

        for key in (
            "comment",
            "comments",
            "comment_list",
            "reply",
            "replies",
            "sub_comments",
            "subComments",
            "item",
            "items",
            "data",
            "value",
            "record",
            "list",
        ):
            if key in value:
                found.extend(iter_comment_like_dicts(value.get(key), depth + 1))

        for nested in value.values():
            if isinstance(nested, (dict, list, tuple)):
                found.extend(iter_comment_like_dicts(nested, depth + 1))

    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(iter_comment_like_dicts(item, depth + 1))

    return found


def looks_like_note(value: dict[str, Any]) -> bool:
    if not get_note_id(value):
        return False
    if looks_like_comment(value):
        return False

    key_set = set(value.keys())
    marker_keys = {
        "title",
        "display_title",
        "desc",
        "description",
        "user_info",
        "author",
        "image_list",
        "images_list",
        "video",
        "interact_info",
        "tag_list",
        "share_link",
        "share_url",
    }
    return bool(key_set.intersection(marker_keys))


def looks_like_comment(value: dict[str, Any]) -> bool:
    key_set = set(value.keys())
    if "comment_id" in key_set:
        return True
    if {"content", "user_info", "like_count"}.issubset(key_set):
        return True
    if {"content", "pictures", "user_info"}.issubset(key_set):
        return True
    return False


def normalize_comment(comment: dict[str, Any]) -> dict[str, Any] | None:
    note_id = str(comment.get("note_id") or "").strip()
    if not note_id:
        note_id = get_note_id(comment)
    if not note_id:
        return None

    content = str(comment.get("content") or comment.get("text") or "").strip()
    author = extract_author(comment)
    upload_time = parse_xhs_datetime(
        comment.get("create_time")
        or comment.get("upload_time")
        or comment.get("time")
        or comment.get("updated_at")
    )

    return {
        "note_id": note_id,
        "comment_id": str(comment.get("comment_id") or comment.get("id") or "").strip(),
        "author_handle": author["handle"],
        "author_name": author["name"],
        "content": cleanup_text(content),
        "like_count": first_number(
            comment.get("like_count"), comment.get("liked_count")
        )
        or 0,
        "upload_time": upload_time,
    }


def comment_datetime_sort_key(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return 0.0


def build_export_record(note: dict[str, Any]) -> ExportRecord:
    record_id = get_note_id(note) or "unknown"
    author = extract_author(note)
    media_assets = extract_media_assets(note)
    created_at = parse_xhs_datetime(
        note.get("time")
        or note.get("create_time")
        or note.get("publish_time")
        or note.get("created_at")
    )
    updated_at = parse_xhs_datetime(
        note.get("last_update_time")
        or note.get("update_time")
        or note.get("updated_at")
        or note.get("time")
    )

    return ExportRecord(
        record_id=record_id,
        author_handle=author["handle"],
        author_name=author["name"],
        created_at=created_at,
        updated_at=updated_at,
        body_markdown=build_body_markdown(note),
        url=get_note_url(note, record_id),
        stats_line=build_stats_line(note),
        media=media_assets,
    )


def extract_author(note: dict[str, Any]) -> dict[str, str]:
    user = note.get("user") if isinstance(note.get("user"), dict) else None
    if not user:
        user = (
            note.get("user_info") if isinstance(note.get("user_info"), dict) else None
        )
    if not user:
        user = note.get("author") if isinstance(note.get("author"), dict) else None
    if not user:
        user = {}

    handle = (
        str(user.get("user_id") or user.get("userId") or user.get("id") or "unknown")
        .strip()
        .lower()
    )
    name = str(user.get("nickname") or user.get("name") or handle or "unknown").strip()
    return {
        "handle": handle or "unknown",
        "name": name or handle or "unknown",
    }


def build_body_markdown(note: dict[str, Any]) -> str:
    title = str(note.get("title") or "").strip()
    desc = str(
        note.get("desc") or note.get("description") or note.get("content") or ""
    ).strip()

    parts = [part for part in (title, desc) if part]
    text = "\n\n".join(parts)

    tag_values = extract_tags(note)
    if tag_values:
        text += "\n\n" + " ".join(f"#{tag}" for tag in tag_values)

    return cleanup_text(text)


def extract_tags(note: dict[str, Any]) -> list[str]:
    values = note.get("tag_list")
    if not isinstance(values, list):
        return []
    tags: list[str] = []
    for value in values:
        if isinstance(value, str):
            cleaned = value.strip()
        elif isinstance(value, dict):
            cleaned = str(value.get("name") or value.get("tag_name") or "").strip()
        else:
            cleaned = ""
        if cleaned:
            tags.append(cleaned)
    return tags


def extract_media_assets(note: dict[str, Any]) -> list[MediaAsset]:
    assets: list[MediaAsset] = []

    for image in extract_images(note):
        url = extract_media_url(image)
        if url:
            assets.append(MediaAsset(url=url, media_type="image"))

    video = note.get("video") if isinstance(note.get("video"), dict) else {}
    video_url = extract_media_url(video)
    if video_url:
        assets.append(MediaAsset(url=video_url, media_type="video"))

    return dedupe_media_assets(assets)


def extract_images(note: dict[str, Any]) -> list[Any]:
    values = note.get("images_list")
    if isinstance(values, list):
        return values
    values = note.get("image_list")
    if isinstance(values, list):
        return values
    values = note.get("image_urls")
    if isinstance(values, list):
        return values
    return []


def extract_media_url(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""

    for key in (
        "url_default",
        "url",
        "url_pre",
        "master_url",
        "url_size_large",
        "origin_video_key",
        "gif_url",
    ):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    url_list = value.get("url_list")
    if isinstance(url_list, list):
        for item in url_list:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def dedupe_media_assets(values: list[MediaAsset]) -> list[MediaAsset]:
    unique: list[MediaAsset] = []
    seen: set[str] = set()
    for value in values:
        if not value.url or value.url in seen:
            continue
        seen.add(value.url)
        unique.append(value)
    return unique


def get_note_id(note: dict[str, Any]) -> str:
    for key in ("note_id", "noteId", "item_id"):
        candidate = note.get(key)
        if candidate is None:
            continue
        text = str(candidate).strip()
        if text:
            return text

    share_link = str(note.get("share_link") or note.get("share_url") or "").strip()
    if share_link:
        match = re.search(r"/explore/([A-Za-z0-9_-]+)", share_link)
        if match:
            return match.group(1)
    return ""


def get_note_url(note: dict[str, Any], note_id: str) -> str | None:
    for key in ("share_link", "share_url", "url"):
        value = note.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if note_id and note_id != "unknown":
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    return None


def build_stats_line(note: dict[str, Any]) -> str | None:
    raw_info = note.get("interact_info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}

    like_count = first_number(
        info.get("liked_count"),
        info.get("like_count"),
        note.get("liked_count"),
        note.get("like_count"),
    )
    collect_count = first_number(
        info.get("collected_count"),
        info.get("collect_count"),
        note.get("collected_count"),
        note.get("collect_count"),
    )
    comment_count = first_number(
        info.get("comment_count"),
        note.get("comment_count"),
    )
    share_count = first_number(
        info.get("share_count"),
        note.get("share_count"),
    )

    if (
        like_count is None
        and collect_count is None
        and comment_count is None
        and share_count is None
    ):
        return None

    parts = [
        f"❤ {like_count or 0}",
        f"⭐ {collect_count or 0}",
        f"💬 {comment_count or 0}",
        f"↗ {share_count or 0}",
    ]
    return " · ".join(parts)


def first_number(*values: Any) -> int | None:
    for value in values:
        number = to_int(value)
        if number is not None:
            return number
    return None


def to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def parse_xhs_datetime(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        try:
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()
        except (OverflowError, OSError, ValueError):
            return epoch_datetime()

    if isinstance(value, str):
        raw = value.strip()
        if raw:
            if raw.isdigit():
                return parse_xhs_datetime(int(raw))
            iso_candidate = raw.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(iso_candidate).astimezone()
            except (ValueError, OSError):
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                try:
                    return datetime.strptime(raw, fmt).astimezone()
                except (ValueError, OSError):
                    continue

    return epoch_datetime()


def epoch_datetime() -> datetime:
    try:
        return datetime.fromtimestamp(0, tz=timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def cleanup_text(text: str) -> str:
    lines = [re.sub(r"[ \t]+$", "", line) for line in text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def record_matches_keywords(record: ExportRecord, keyword_filters: set[str]) -> bool:
    blob = "\n".join(
        (record.body_markdown, record.author_name, record.author_handle)
    ).lower()
    return any(keyword in blob for keyword in keyword_filters)


def record_quality_score(record: ExportRecord) -> int:
    score = 0
    body = record.body_markdown.strip()
    if body:
        score += 1
    if len(body) >= 20:
        score += 1
    if record.stats_line:
        score += 1
    if record.media:
        score += 1
    if record.url and "/explore/" in record.url:
        score += 1
    return score
