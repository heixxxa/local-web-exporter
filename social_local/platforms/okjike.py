from __future__ import annotations

import argparse
import re
from argparse import Namespace
from datetime import datetime
from typing import Any

from ..models import ExportRecord, MediaAsset, PlatformPayload, SelectionResult
from .base import PlatformAdapter


class OkjikeAdapter(PlatformAdapter):
    key = "jike"
    display_name = "OKJike"
    record_label_singular = "post"
    record_label_plural = "posts"
    supported_origins = ("web.okjike.com",)
    probe_paths = ("/", "/robots.txt", "/home")
    store_names = ("jike_posts", "jike_comments", "captures")

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        del parser

    def default_document_title(self) -> str:
        return "OKJike Export Restore"

    def default_db_prefix(self) -> str:
        return "jike-web-exporter"

    def aggregate_browser_databases(
        self,
        databases: list[dict[str, Any]],
    ) -> PlatformPayload:
        post_map: dict[str, dict[str, Any]] = {}
        comment_map: dict[str, dict[str, Any]] = {}
        captures: list[dict[str, Any]] = []
        database_names: list[str] = []

        for database in databases:
            name = str(database.get("name") or "").strip()
            if name:
                database_names.append(name)

            tables = (
                database.get("tables")
                if isinstance(database.get("tables"), dict)
                else {}
            )
            for post in tables.get("jike_posts", []):
                if not isinstance(post, dict):
                    continue
                post_id = get_post_id(post)
                if not post_id:
                    continue
                merged_post = dict(post)
                if name:
                    merged_post.setdefault("__database_name__", name)

                current = post_map.get(post_id)
                if current is None or get_post_updated_datetime(
                    merged_post
                ) > get_post_updated_datetime(current):
                    post_map[post_id] = merged_post

            for comment in tables.get("jike_comments", []):
                if not isinstance(comment, dict):
                    continue
                comment_id = get_comment_id(comment)
                if not comment_id:
                    continue
                merged_comment = dict(comment)
                if name:
                    merged_comment.setdefault("__database_name__", name)

                current_comment = comment_map.get(comment_id)
                if current_comment is None or get_comment_updated_datetime(
                    merged_comment
                ) > get_comment_updated_datetime(current_comment):
                    comment_map[comment_id] = merged_comment

            for capture in tables.get("captures", []):
                if not isinstance(capture, dict):
                    continue
                merged_capture = dict(capture)
                if name:
                    merged_capture.setdefault("__database_name__", name)
                captures.append(merged_capture)

        return PlatformPayload(
            database_names=sorted(set(database_names)),
            tables={
                "jike_posts": list(post_map.values()),
                "jike_comments": list(comment_map.values()),
                "captures": captures,
            },
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

        if isinstance(root, dict):
            posts = (
                root.get("jike_posts")
                if isinstance(root.get("jike_posts"), list)
                else []
            )
            comments = (
                root.get("jike_comments")
                if isinstance(root.get("jike_comments"), list)
                else []
            )
            captures = (
                root.get("captures") if isinstance(root.get("captures"), list) else []
            )
            if posts or comments or captures:
                return PlatformPayload(
                    database_names=[],
                    tables={
                        "jike_posts": [
                            item for item in posts if isinstance(item, dict)
                        ],
                        "jike_comments": [
                            item for item in comments if isinstance(item, dict)
                        ],
                        "captures": [
                            item for item in captures if isinstance(item, dict)
                        ],
                    },
                )

        if isinstance(root, list):
            rows = [row for row in root if isinstance(row, dict)]
            return PlatformPayload(database_names=[], tables={"jike_posts": rows})

        raise ValueError("Unsupported OKJike JSON payload format.")

    def select_records(
        self,
        payload: PlatformPayload,
        args: Namespace,
    ) -> SelectionResult:
        selected_extensions = parse_extensions(getattr(args, "extensions", None))
        author_filters = parse_csv_values(getattr(args, "author_handles", None))
        keyword_filters = parse_csv_values(getattr(args, "keywords", None))
        include_all_records = bool(getattr(args, "all_records", False))

        posts, captures_by_post = select_posts(
            payload.tables.get("jike_posts", []),
            payload.tables.get("captures", []),
            selected_extensions,
            include_all_records,
        )
        comments_by_post = group_comments_by_post(
            payload.tables.get("jike_comments", [])
        )

        records: list[ExportRecord] = []
        for post in posts:
            post_id = get_post_id(post)
            if not post_id:
                continue

            post_captures = captures_by_post.get(post_id, [])
            record = build_export_record(
                post,
                post_captures,
                comments_by_post.get(post_id, []),
            )
            if author_filters and record.author_handle.lower() not in author_filters:
                continue
            if keyword_filters and not record_matches_keywords(record, keyword_filters):
                continue
            records.append(record)

        selected_filters: list[str] = []
        if selected_extensions:
            selected_filters.extend(
                f"extension:{value}" for value in sorted(selected_extensions)
            )
        if author_filters:
            selected_filters.extend(
                f"author:{value}" for value in sorted(author_filters)
            )
        if keyword_filters:
            selected_filters.extend(
                f"keyword:{value}" for value in sorted(keyword_filters)
            )

        return SelectionResult(records=records, selected_filters=selected_filters)


SUPPORTED_CAPTURE_TYPES = {"jike_post", "post", None}


def parse_extensions(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    values = {item.strip() for item in raw.split(",") if item.strip()}
    return values or None


def parse_csv_values(raw: str | None) -> set[str]:
    if not raw:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def select_posts(
    posts: list[dict[str, Any]],
    captures: list[dict[str, Any]],
    selected_extensions: set[str] | None,
    include_all_posts: bool,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    post_map: dict[str, dict[str, Any]] = {}
    for post in posts:
        post_id = get_post_id(post)
        if not post_id:
            continue
        current = post_map.get(post_id)
        if current is None or get_post_updated_datetime(
            post
        ) > get_post_updated_datetime(current):
            post_map[post_id] = post

    captures_by_post: dict[str, list[dict[str, Any]]] = {}
    has_post_captures = False
    for capture in captures:
        if not isinstance(capture, dict):
            continue

        capture_type = str(capture.get("type") or "").strip().lower() or None
        if capture_type not in SUPPORTED_CAPTURE_TYPES:
            continue
        has_post_captures = True

        if selected_extensions and capture.get("extension") not in selected_extensions:
            continue

        post_id = str(capture.get("data_key") or "").strip()
        if not post_id:
            continue
        captures_by_post.setdefault(post_id, []).append(capture)

    if include_all_posts:
        return list(post_map.values()), captures_by_post

    if captures_by_post:
        selected_ids = [post_id for post_id in captures_by_post if post_id in post_map]
        return [post_map[post_id] for post_id in selected_ids], captures_by_post

    if has_post_captures or selected_extensions:
        return [], captures_by_post

    return list(post_map.values()), captures_by_post


def group_comments_by_post(
    comments: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        target_id = str(comment.get("target_id") or "").strip()
        if not target_id:
            continue
        grouped.setdefault(target_id, []).append(comment)

    for target_id in grouped:
        grouped[target_id].sort(
            key=lambda comment: get_comment_updated_datetime(comment)
        )
    return grouped


def build_export_record(
    post: dict[str, Any],
    captures: list[dict[str, Any]],
    comments: list[dict[str, Any]],
) -> ExportRecord:
    post_id = get_post_id(post)
    user = post.get("user") if isinstance(post.get("user"), dict) else {}
    author_handle = str(user.get("username") or "").strip() or "unknown"
    author_name = str(user.get("nickname") or "").strip() or author_handle

    media_assets = [
        MediaAsset(
            url=url,
            media_type="image",
        )
        for url in extract_post_pictures(post)
        if url
    ]

    record = ExportRecord(
        record_id=post_id,
        author_handle=author_handle,
        author_name=author_name,
        created_at=get_post_created_datetime(post),
        updated_at=get_post_updated_datetime(post),
        body_markdown=render_post_text(post),
        url=build_post_url(post_id),
        captured_by=sorted(
            {
                str(capture.get("extension"))
                for capture in captures
                if capture.get("extension")
            }
        ),
        first_captured_at=first_capture_datetime(captures),
        stats_line=build_stats_line(post),
        media=media_assets,
    )

    target_post = (
        post.get("target_post") if isinstance(post.get("target_post"), dict) else {}
    )
    target_post_id = get_post_id(target_post)
    target_user = (
        target_post.get("user") if isinstance(target_post.get("user"), dict) else {}
    )
    target_handle = str(target_user.get("username") or "").strip()
    if target_post_id and target_handle:
        record.repost_source_handle = target_handle
        record.repost_source_url = build_post_url(target_post_id)
        record.extra["repost_label"] = "Repost source"

    normalized_comments = [normalize_comment(comment) for comment in comments]
    normalized_comments = [
        comment for comment in normalized_comments if comment is not None
    ]
    if normalized_comments:
        record.extra["related_comments"] = normalized_comments

    return record


def normalize_comment(comment: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(comment, dict):
        return None

    user = comment.get("user") if isinstance(comment.get("user"), dict) else {}
    created_at = parse_jike_datetime(
        comment.get("created_at") or comment.get("upload_time")
    )

    return {
        "comment_id": get_comment_id(comment),
        "note_id": str(comment.get("target_id") or "").strip(),
        "author_handle": str(user.get("username") or "").strip() or "unknown",
        "author_name": str(user.get("nickname") or "").strip() or "unknown",
        "content": cleanup_text(str(comment.get("content") or "").strip()),
        "like_count": safe_int(comment.get("like_count"), 0),
        "upload_time": created_at,
    }


def record_matches_keywords(record: ExportRecord, keywords: set[str]) -> bool:
    text = "\n".join(
        [
            record.body_markdown,
            record.author_handle,
            record.author_name,
        ]
    ).lower()
    return any(keyword in text for keyword in keywords)


def render_post_text(post: dict[str, Any]) -> str:
    content = cleanup_text(str(post.get("content") or "").strip())
    link_info = post.get("link_info") if isinstance(post.get("link_info"), dict) else {}
    link_url = str(link_info.get("link_url") or "").strip()
    link_title = cleanup_text(str(link_info.get("title") or "").strip())

    lines: list[str] = []
    if content:
        lines.append(content)
    if link_url:
        lines.append("")
        if link_title:
            lines.append(f"Link: [{escape_link_label(link_title)}]({link_url})")
        else:
            lines.append(f"Link: {link_url}")

    return "\n".join(lines).strip() or "_No text content available._"


def build_stats_line(post: dict[str, Any]) -> str:
    return " · ".join(
        [
            f"❤ {safe_int(post.get('like_count'), 0)}",
            f"💬 {safe_int(post.get('comment_count'), 0)}",
            f"🔁 {safe_int(post.get('repost_count'), 0)}",
            f"📤 {safe_int(post.get('share_count'), 0)}",
        ]
    )


def extract_post_pictures(post: dict[str, Any]) -> list[str]:
    pictures = post.get("pictures")
    if not isinstance(pictures, list):
        return []

    urls: list[str] = []
    for item in pictures:
        if isinstance(item, str):
            url = normalize_url(item)
            if url:
                urls.append(url)
            continue

        if isinstance(item, dict):
            for key in (
                "pic_url",
                "middle_pic_url",
                "small_pic_url",
                "thumbnail_url",
                "url",
            ):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    urls.append(normalize_url(value))
                    break
    return [url for url in urls if url]


def normalize_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def build_post_url(post_id: str) -> str | None:
    if not post_id:
        return None
    return f"https://web.okjike.com/originalPost/{post_id}"


def first_capture_datetime(captures: list[dict[str, Any]]) -> datetime | None:
    timestamps = [
        capture.get("created_at")
        for capture in captures
        if capture.get("created_at") is not None
    ]
    if not timestamps:
        return None
    return parse_jike_datetime(min(timestamps))


def get_post_id(post: dict[str, Any]) -> str:
    if not isinstance(post, dict):
        return ""
    return str(
        post.get("post_id") or post.get("id") or post.get("message_id") or ""
    ).strip()


def get_comment_id(comment: dict[str, Any]) -> str:
    if not isinstance(comment, dict):
        return ""
    return str(comment.get("comment_id") or comment.get("id") or "").strip()


def get_post_created_datetime(post: dict[str, Any]) -> datetime:
    return parse_jike_datetime(post.get("created_at") or post.get("upload_time"))


def get_post_updated_datetime(post: dict[str, Any]) -> datetime:
    return parse_jike_datetime(
        post.get("edited_at") or post.get("created_at") or post.get("upload_time")
    )


def get_comment_updated_datetime(comment: dict[str, Any]) -> datetime:
    return parse_jike_datetime(comment.get("created_at") or comment.get("upload_time"))


def parse_jike_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone()

    if isinstance(value, (int, float)):
        # Jike uses ms timestamps in upload_time/captured_at.
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds).astimezone()

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return datetime.fromtimestamp(0).astimezone()

        if re.fullmatch(r"\d+", raw):
            number_value = float(raw)
            seconds = (
                number_value / 1000 if number_value > 10_000_000_000 else number_value
            )
            return datetime.fromtimestamp(seconds).astimezone()

        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone()
        except ValueError:
            pass

    return datetime.fromtimestamp(0).astimezone()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def cleanup_text(text: str) -> str:
    lines = [re.sub(r"[ \t]+$", "", line) for line in text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()


def escape_link_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("]", "\\]")
