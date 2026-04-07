from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.request import Request, urlopen

from .browser import extract_indexeddb_payload
from .models import ExportRecord, MediaAsset
from .platforms.base import PlatformAdapter
from .rendering import (
    record_sort_key,
    render_export_index,
    render_single_record_markdown,
    safe_int,
)

DEFAULT_MEDIA_FILENAME_PATTERN = "{screen_name}_{id}_{type}_{num}_{date}.{ext}"

LEGACY_PLATFORM_ALIASES: dict[str, str] = {
    "twitter": "x",
    "xiaohongshu": "xhs",
    "okjike": "jike",
}


def parse_multi_values(values: Iterable[str] | None) -> list[str]:
    parsed: list[str] = []
    for value in values or []:
        for item in value.split(","):
            cleaned = item.strip()
            if cleaned:
                parsed.append(cleaned)
    return parsed


def run_indexeddb_export(adapter: PlatformAdapter, args: Any) -> int:
    output_md = Path(args.output_md)
    output_root = output_md.parent
    record_dir_arg = getattr(args, "record_dir", None) or getattr(args, "tweet_dir", None)
    record_dir = (
        Path(record_dir_arg)
        if record_dir_arg
        else output_md.with_name(f"{output_md.stem}_{adapter.record_label_plural}")
    )
    history_path = (
        Path(args.history_file)
        if args.history_file
        else output_md.with_suffix(".history.json")
    )
    media_dir = (
        Path(args.media_dir)
        if args.media_dir
        else output_md.with_name(f"{output_md.stem}_media")
    )
    output_json = Path(args.output_json) if args.output_json else None
    document_title = adapter.resolve_document_title(args.title)
    db_prefix = adapter.resolve_db_prefix(args.db_prefix)
    db_names = parse_multi_values(args.db_name)

    extracted = extract_indexeddb_payload(
        adapter=adapter,
        edge_user_data_dir=Path(args.edge_user_data_dir),
        edge_profile_directory=args.edge_profile_directory,
        edge_executable_path=Path(args.edge_executable_path) if args.edge_executable_path else None,
        origin=args.origin,
        db_prefix=db_prefix,
        db_names=db_names,
        timeout_ms=args.timeout_ms,
        headed=args.headed,
        copy_indexeddb=args.copy_indexeddb,
        keep_temp_profile=args.keep_temp_profile,
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

    database_name = ", ".join(payload.database_names) if payload.database_names else None
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

    history = empty_export_history() if args.ignore_history else load_export_history(history_path)
    history_entries = history.setdefault("records", {})
    should_export_media = args.export_media or args.run_aria2
    should_prepare_media = should_export_media or bool(args.aria2_input_file) or args.run_aria2
    records_to_write: list[ExportRecord] = []
    skipped_by_history = 0

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
        build_media_records(records_to_write, args.media_filename_pattern or DEFAULT_MEDIA_FILENAME_PATTERN)
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
        print("No media URLs found in the records being exported; skipping aria2 output.")

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
            media_records=prepare_media_records_for_markdown(record_media, record_path.parent)
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
        selected_count=len(records),
        written_count=written_count,
        skipped_count=skipped_by_history,
        sort_by=args.sort_by,
        descending=args.descending,
    )
    output_md.write_text(index_markdown, encoding="utf-8")
    save_export_history(history_path, history)
    print(f"Wrote {adapter.record_label_singular} index to {output_md}")
    print(f"Wrote {written_count} {adapter.record_label_plural} to {record_dir}")
    print(f"Saved export history to {history_path}")
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
            migrated["records"][build_history_key_from_parts(normalized_platform, record_id)] = entry_copy
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
            migrated_entry.setdefault("record_id", migrated_entry.get("tweet_id") or str(tweet_id))
            migrated_entry.setdefault("author_handle", migrated_entry.get("screen_name"))
            migrated_entry.setdefault("author_name", migrated_entry.get("display_name"))
            migrated_entry.pop("tweet_id", None)
            migrated["records"][build_history_key_from_parts("x", str(tweet_id))] = migrated_entry
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

            extension = get_file_extension_from_url(media.url)
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


def get_file_extension_from_url(url: str) -> str:
    match = re.search(r"format=(\w+)|\.(\w+)$|\.(\w+)\?.+$", url)
    return match.group(1) or match.group(2) or match.group(3) or "jpg"


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
        if isinstance(previous_entry, dict) and isinstance(previous_entry.get("media_files"), list)
        else []
    )
    if media_records is None:
        media_count = safe_int(previous_entry.get("media_count"), 0) if previous_entry else len(record.media)
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

    previous_media_exported = bool(previous_entry and previous_entry.get("media_exported"))
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
        "first_captured_at": record.first_captured_at.isoformat() if record.first_captured_at else None,
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
