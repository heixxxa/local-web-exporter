#!/usr/bin/env python3
"""Read twitter-web-exporter IndexedDB from Microsoft Edge via Playwright.

This script opens an Edge profile (or a lightweight copy of its IndexedDB data),
reads the userscript's local IndexedDB directly in the browser context, renders a
Markdown archive, and can optionally generate/run an aria2 input file for media.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.request import Request, urlopen

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import restore_indexeddb_to_markdown as restore

DEFAULT_MEDIA_FILENAME_PATTERN = "{screen_name}_{id}_{type}_{num}_{date}.{ext}"
INDEXEDDB_EVAL = r"""
async ({ dbPrefix, dbNames }) => {
  const openDatabase = (name) => new Promise((resolve, reject) => {
    const request = indexedDB.open(name);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error(`Failed to open ${name}`));
    request.onblocked = () => reject(new Error(`Opening ${name} was blocked`));
  });

  const readStore = (db, storeName) => new Promise((resolve, reject) => {
    if (!db.objectStoreNames.contains(storeName)) {
      resolve([]);
      return;
    }

    const tx = db.transaction(storeName, 'readonly');
    const store = tx.objectStore(storeName);
    const request = store.getAll();
    request.onsuccess = () => resolve(request.result || []);
    request.onerror = () => reject(request.error || new Error(`Failed to read ${storeName}`));
    tx.onabort = () => reject(tx.error || new Error(`Transaction aborted for ${storeName}`));
  });

  const explicitNames = Array.isArray(dbNames) ? dbNames.filter(Boolean) : [];
  let names = explicitNames;

  if (!names.length) {
    if (typeof indexedDB.databases === 'function') {
      const databases = await indexedDB.databases();
      names = databases
        .map((item) => item && item.name)
        .filter(Boolean)
        .filter((name) => String(name).startsWith(dbPrefix));
    } else {
      names = [dbPrefix];
    }
  }

  const results = [];
  for (const name of names) {
    try {
      const db = await openDatabase(name);
      const tweets = await readStore(db, 'tweets');
      const users = await readStore(db, 'users');
      const captures = await readStore(db, 'captures');
      results.push({
        name,
        objectStores: Array.from(db.objectStoreNames),
        tweets,
        users,
        captures,
      });
      db.close();
    } catch (error) {
      results.push({
        name,
        error: String(error),
      });
    }
  }

  return {
    origin: location.origin,
    url: location.href,
    databases: results,
  };
}
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Use Playwright + Microsoft Edge to read twitter-web-exporter IndexedDB "
            "directly and restore tweets into Markdown."
        )
    )
    parser.add_argument(
        "--output-md",
        "-o",
        required=True,
        help="Path to the index Markdown file to write.",
    )
    parser.add_argument(
        "--tweet-dir",
        help="Directory used to store one Markdown file per tweet.",
    )
    parser.add_argument(
        "--history-file",
        help="JSON file used to remember exported tweets and skip duplicates on future runs.",
    )
    parser.add_argument(
        "--output-json",
        help="Optional path to save the raw extracted IndexedDB payload as JSON.",
    )
    parser.add_argument(
        "--title",
        default="Twitter Export Restore (Playwright)",
        help="Document title written at the top of the Markdown file.",
    )
    parser.add_argument(
        "--extensions",
        help=(
            "Comma-separated capture extensions to keep, such as "
            "BookmarksModule,LikesModule,UserTweetsModule."
        ),
    )
    parser.add_argument(
        "--sort-by",
        choices=("created", "captured", "updated"),
        default="created",
        help="Sort tweets by original post time, first capture time, or local update time.",
    )
    parser.add_argument(
        "--descending",
        action="store_true",
        help="Sort newest-first instead of oldest-first.",
    )
    parser.add_argument(
        "--all-tweets",
        action="store_true",
        help="Export every tweet found in the tweets table, not just rows referenced by captures.",
    )
    parser.add_argument(
        "--edge-user-data-dir",
        default=str(default_edge_user_data_dir()),
        help="Microsoft Edge user data root. Defaults to the standard Windows Edge profile path.",
    )
    parser.add_argument(
        "--edge-profile-directory",
        default="Default",
        help="Edge profile directory under the user data root, such as Default or Profile 1.",
    )
    parser.add_argument(
        "--edge-executable-path",
        help="Optional full path to msedge.exe. If omitted, Playwright uses channel=msedge.",
    )
    parser.add_argument(
        "--origin",
        choices=("auto", "x.com", "twitter.com"),
        default="auto",
        help="Which site origin to probe for IndexedDB. auto tries both x.com and twitter.com.",
    )
    parser.add_argument(
        "--db-name",
        action="append",
        default=[],
        help="Exact IndexedDB database name to read. Can be repeated or contain comma-separated values.",
    )
    parser.add_argument(
        "--db-prefix",
        default=default_database_prefix(),
        help="Database name prefix used when auto-discovering IndexedDB databases.",
    )
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=15000,
        help="Page navigation timeout in milliseconds.",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Run Edge in headed mode for debugging instead of headless mode.",
    )
    parser.add_argument(
        "--no-copy-indexeddb",
        dest="copy_indexeddb",
        action="store_false",
        help=(
            "Use the live Edge profile directly. This is faster, but may fail if Edge is using the profile."
        ),
    )
    parser.set_defaults(copy_indexeddb=True)
    parser.add_argument(
        "--keep-temp-profile",
        action="store_true",
        help="Keep the temporary copied profile directory for debugging.",
    )
    parser.add_argument(
        "--ignore-history",
        action="store_true",
        help="Ignore the saved export history and regenerate tweet Markdown files.",
    )
    parser.add_argument(
        "--export-media",
        action="store_true",
        help="Download tweet media files and link them from each tweet Markdown file.",
    )
    parser.add_argument(
        "--aria2-input-file",
        help="Optional aria2 input file to generate for tweet media downloads.",
    )
    parser.add_argument(
        "--media-dir",
        help="Directory used for downloaded media files and aria2 output targets.",
    )
    parser.add_argument(
        "--media-filename-pattern",
        default=DEFAULT_MEDIA_FILENAME_PATTERN,
        help=(
            "Filename pattern for media downloads. Available tokens: {id}, {screen_name}, {name}, "
            "{index}, {num}, {date}, {time}, {type}, {ext}."
        ),
    )
    parser.add_argument(
        "--run-aria2",
        action="store_true",
        help="Run aria2 after generating the input file.",
    )
    parser.add_argument(
        "--aria2-bin",
        default="aria2c",
        help="aria2 executable name or absolute path.",
    )
    parser.add_argument(
        "--aria2-max-concurrent-downloads",
        type=int,
        default=8,
        help="Value passed to aria2 --max-concurrent-downloads when --run-aria2 is used.",
    )
    parser.add_argument(
        "--aria2-split",
        type=int,
        default=8,
        help="Value passed to aria2 --split when --run-aria2 is used.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    db_names = parse_multi_values(args.db_name)
    output_md = Path(args.output_md)
    output_root = output_md.parent
    tweet_dir = (
        Path(args.tweet_dir)
        if args.tweet_dir
        else output_md.with_name(f"{output_md.stem}_tweets")
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

    extracted = extract_indexeddb_payload(
        edge_user_data_dir=Path(args.edge_user_data_dir),
        edge_profile_directory=args.edge_profile_directory,
        edge_executable_path=Path(args.edge_executable_path)
        if args.edge_executable_path
        else None,
        origin=args.origin,
        db_prefix=args.db_prefix,
        db_names=db_names,
        timeout_ms=args.timeout_ms,
        headed=args.headed,
        copy_indexeddb=args.copy_indexeddb,
        keep_temp_profile=args.keep_temp_profile,
    )

    if output_json:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    aggregated = aggregate_databases(extracted.get("databases", []))
    selected_extensions = restore.parse_extensions(args.extensions)
    selected_tweets, captures_by_tweet = restore.select_tweets(
        aggregated["tweets"],
        aggregated["captures"],
        selected_extensions,
        args.all_tweets,
    )
    selected_tweets.sort(
        key=lambda tweet: restore.tweet_sort_key(
            tweet,
            captures_by_tweet.get(restore.get_tweet_id(tweet), []),
            args.sort_by,
        ),
        reverse=args.descending,
    )

    source_label = (
        "Microsoft Edge IndexedDB via Playwright "
        f"({extracted.get('origin') or 'unknown origin'})"
    )
    database_name = (
        ", ".join(aggregated["database_names"]) if aggregated["database_names"] else None
    )
    history = (
        empty_export_history()
        if args.ignore_history
        else load_export_history(history_path)
    )
    history_entries = history.setdefault("tweets", {})
    should_export_media = args.export_media or args.run_aria2
    should_prepare_media = (
        should_export_media or bool(args.aria2_input_file) or args.run_aria2
    )
    tweets_to_write: list[dict[str, Any]] = []
    skipped_by_history = 0

    for tweet in selected_tweets:
        tweet_id = restore.get_tweet_id(tweet)
        history_entry = history_entries.get(tweet_id)
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
            tweets_to_write.append(tweet)
        else:
            skipped_by_history += 1

    media_records = (
        build_media_records(tweets_to_write, args.media_filename_pattern)
        if tweets_to_write and should_prepare_media
        else []
    )
    assign_media_local_paths(media_records, media_dir)
    media_records_by_tweet = group_media_records_by_tweet(media_records)

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
            referer=origin_referer(extracted.get("origin")),
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
        print("No media URLs found in the tweets being exported; skipping aria2 output.")

    if media_records and args.export_media and not args.run_aria2:
        downloaded_count, reused_count, failed_count = download_media_files(
            media_records=media_records,
            media_dir=media_dir,
            referer=origin_referer(extracted.get("origin")),
        )
        print(
            "Media download summary: "
            f"{downloaded_count} downloaded, {reused_count} reused, {failed_count} failed."
        )

    output_root.mkdir(parents=True, exist_ok=True)
    tweet_dir.mkdir(parents=True, exist_ok=True)
    written_count = 0
    for tweet in tweets_to_write:
        tweet_id = restore.get_tweet_id(tweet)
        previous_entry = history_entries.get(tweet_id)
        tweet_path = tweet_dir / f"{tweet_id}.md"
        tweet_media_records = media_records_by_tweet.get(tweet_id)
        markdown = restore.render_single_tweet_markdown(
            tweet=tweet,
            captures=captures_by_tweet.get(tweet_id, []),
            input_path=source_label,
            document_title=args.title,
            database_name=database_name,
            selected_extensions=selected_extensions,
            media_records=prepare_media_records_for_markdown(
                tweet_media_records,
                tweet_path.parent,
            )
            if should_export_media and tweet_media_records is not None
            else None,
        )
        tweet_path.write_text(markdown, encoding="utf-8")
        history_entries[tweet_id] = build_history_entry(
            tweet=tweet,
            captures=captures_by_tweet.get(tweet_id, []),
            tweet_path=tweet_path,
            output_root=output_root,
            media_records=tweet_media_records,
            previous_entry=previous_entry,
            media_export_requested=should_export_media,
        )
        written_count += 1

    index_markdown = render_export_index(
        history_entries=list(history_entries.values()),
        output_root=output_root,
        input_path=source_label,
        document_title=args.title,
        database_name=database_name,
        selected_extensions=selected_extensions,
        selected_count=len(selected_tweets),
        written_count=written_count,
        skipped_count=skipped_by_history,
        sort_by=args.sort_by,
        descending=args.descending,
    )
    output_md.write_text(index_markdown, encoding="utf-8")
    save_export_history(history_path, history)
    print(f"Wrote tweet index to {output_md}")
    print(f"Wrote {written_count} tweet Markdown files to {tweet_dir}")
    print(f"Saved export history to {history_path}")

    return 0


def default_database_prefix() -> str:
    package_json = Path(__file__).resolve().parent.parent / "package.json"
    try:
        return str(json.loads(package_json.read_text(encoding="utf-8"))["name"])
    except Exception:
        return "twitter-web-exporter"


def default_edge_user_data_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return Path("Microsoft/Edge/User Data")
    return Path(local_appdata) / "Microsoft" / "Edge" / "User Data"


def parse_multi_values(values: Iterable[str] | None) -> list[str]:
    parsed: list[str] = []
    for value in values or []:
        for item in value.split(","):
            cleaned = item.strip()
            if cleaned:
                parsed.append(cleaned)
    return parsed


def ensure_playwright_import() -> tuple[Any, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run `pip install playwright` first."
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


def candidate_urls(origin: str) -> list[str]:
    if origin == "x.com":
        domains = ["x.com"]
    elif origin == "twitter.com":
        domains = ["twitter.com"]
    else:
        domains = ["x.com", "twitter.com"]

    urls: list[str] = []
    for domain in domains:
        urls.extend(
            [
                f"https://{domain}/robots.txt",
                f"https://{domain}/",
                f"https://{domain}/home",
            ]
        )
    return urls


@contextmanager
def maybe_copied_user_data_dir(
    source_root: Path,
    profile_directory: str,
    copy_indexeddb: bool,
    keep_temp_profile: bool,
):
    if not copy_indexeddb:
        yield source_root
        return

    with tempfile.TemporaryDirectory(prefix="twitter-web-exporter-edge-") as temp_dir:
        temp_root = Path(temp_dir)
        prepare_minimal_edge_profile_copy(source_root, temp_root, profile_directory)
        if keep_temp_profile:
            preserved_root = source_root.parent / f"{temp_root.name}-preserved"
            if preserved_root.exists():
                shutil.rmtree(preserved_root)
            shutil.copytree(temp_root, preserved_root)
            print(f"Preserved temporary profile at {preserved_root}")
        yield temp_root


def prepare_minimal_edge_profile_copy(
    source_root: Path, temp_root: Path, profile_directory: str
) -> None:
    if not source_root.exists():
        raise RuntimeError(f"Edge user data dir does not exist: {source_root}")

    source_profile = source_root / profile_directory
    if not source_profile.exists():
        raise RuntimeError(f"Edge profile directory does not exist: {source_profile}")

    temp_profile = temp_root / profile_directory
    temp_profile.mkdir(parents=True, exist_ok=True)

    local_state = source_root / "Local State"
    if local_state.exists():
        shutil.copy2(local_state, temp_root / "Local State")

    for item_name in ("IndexedDB", "Preferences"):
        source_item = source_profile / item_name
        target_item = temp_profile / item_name
        if not source_item.exists():
            continue
        if source_item.is_dir():
            shutil.copytree(source_item, target_item)
        else:
            shutil.copy2(source_item, target_item)


def extract_indexeddb_payload(
    *,
    edge_user_data_dir: Path,
    edge_profile_directory: str,
    edge_executable_path: Path | None,
    origin: str,
    db_prefix: str,
    db_names: list[str],
    timeout_ms: int,
    headed: bool,
    copy_indexeddb: bool,
    keep_temp_profile: bool,
) -> dict[str, Any]:
    sync_playwright, PlaywrightTimeoutError = ensure_playwright_import()
    errors: list[str] = []

    # Prefer a lightweight IndexedDB copy first so we do not touch the live Edge profile.
    # Fall back to the live profile only if needed.
    for use_copy in [True, False] if copy_indexeddb else [False]:
        try:
            with maybe_copied_user_data_dir(
                source_root=edge_user_data_dir,
                profile_directory=edge_profile_directory,
                copy_indexeddb=use_copy,
                keep_temp_profile=keep_temp_profile,
            ) as user_data_dir:
                with sync_playwright() as playwright:
                    launch_options: dict[str, Any] = {
                        "user_data_dir": str(user_data_dir),
                        "headless": not headed,
                        "ignore_https_errors": True,
                        "args": [f"--profile-directory={edge_profile_directory}"],
                    }
                    if edge_executable_path:
                        launch_options["executable_path"] = str(edge_executable_path)
                    else:
                        launch_options["channel"] = "msedge"

                    context = playwright.chromium.launch_persistent_context(
                        **launch_options
                    )
                    try:
                        page = context.pages[0] if context.pages else context.new_page()
                        attempts: list[dict[str, Any]] = []
                        for url in candidate_urls(origin):
                            try:
                                page.goto(
                                    url,
                                    wait_until="domcontentloaded",
                                    timeout=timeout_ms,
                                )
                                try:
                                    page.wait_for_load_state(
                                        "networkidle", timeout=min(timeout_ms, 4000)
                                    )
                                except PlaywrightTimeoutError:
                                    pass

                                result = page.evaluate(
                                    INDEXEDDB_EVAL,
                                    {
                                        "dbPrefix": db_prefix,
                                        "dbNames": db_names,
                                    },
                                )
                                attempts.append(
                                    {
                                        "requested_url": url,
                                        "final_url": page.url,
                                        "database_count": len(
                                            result.get("databases", [])
                                        ),
                                    }
                                )

                                databases = [
                                    database
                                    for database in result.get("databases", [])
                                    if isinstance(database, dict)
                                    and not database.get("error")
                                ]
                                if databases:
                                    return {
                                        "source": {
                                            "browser": "Microsoft Edge",
                                            "user_data_dir": str(edge_user_data_dir),
                                            "profile_directory": edge_profile_directory,
                                            "used_temporary_copy": use_copy,
                                            "requested_db_names": db_names,
                                            "db_prefix": db_prefix,
                                        },
                                        "origin": result.get("origin"),
                                        "url": result.get("url"),
                                        "attempts": attempts,
                                        "databases": databases,
                                    }
                            except Exception as exc:
                                attempts.append(
                                    {
                                        "requested_url": url,
                                        "error": str(exc),
                                    }
                                )
                        raise RuntimeError(
                            "No matching IndexedDB databases were found on x.com/twitter.com for this Edge profile."
                        )
                    finally:
                        context.close()
        except Exception as exc:
            mode = "copied IndexedDB profile" if use_copy else "live Edge profile"
            errors.append(f"{mode}: {exc}")

    raise RuntimeError(
        "Failed to read IndexedDB via Playwright.\n"
        + "\n".join(f"- {error}" for error in errors)
    )


def aggregate_databases(databases: list[dict[str, Any]]) -> dict[str, Any]:
    tweet_map: dict[str, dict[str, Any]] = {}
    captures: list[dict[str, Any]] = []
    database_names: list[str] = []

    for database in databases:
        name = str(database.get("name") or "")
        if name:
            database_names.append(name)

        for tweet in database.get("tweets", []):
            if not isinstance(tweet, dict):
                continue
            tweet_id = restore.get_tweet_id(tweet)
            if not tweet_id:
                continue

            current = tweet_map.get(tweet_id)
            if current is None or restore.get_updated_datetime(
                tweet
            ) > restore.get_updated_datetime(current):
                tweet_map[tweet_id] = tweet

        for capture in database.get("captures", []):
            if not isinstance(capture, dict):
                continue
            merged_capture = dict(capture)
            if name:
                merged_capture["__database_name__"] = name
            captures.append(merged_capture)

    return {
        "database_names": sorted(set(database_names)),
        "tweets": list(tweet_map.values()),
        "captures": captures,
    }


def empty_export_history() -> dict[str, Any]:
    return {"version": 1, "tweets": {}}


def load_export_history(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_export_history()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid export history file: {path}: {exc}") from exc

    if not isinstance(raw, dict):
        return empty_export_history()

    tweets = raw.get("tweets")
    raw["tweets"] = tweets if isinstance(tweets, dict) else {}
    raw["version"] = safe_int(raw.get("version"), 1)
    return raw


def save_export_history(path: Path, history: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    history["updated_at"] = datetime.now().astimezone().isoformat()
    path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


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


def assign_media_local_paths(
    media_records: list[dict[str, Any]],
    media_dir: Path,
) -> None:
    for record in media_records:
        record["local_path"] = str(media_dir / record["filename"])


def group_media_records_by_tweet(
    media_records: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in media_records:
        grouped.setdefault(str(record["tweet_id"]), []).append(record)
    return grouped


def download_media_files(
    *,
    media_records: list[dict[str, Any]],
    media_dir: Path,
    referer: str,
) -> tuple[int, int, int]:
    media_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    reused = 0
    failed = 0

    for record in media_records:
        target_path = Path(str(record.get("local_path") or media_dir / record["filename"]))
        temp_path = target_path.with_name(f"{target_path.name}.part")
        record["local_path"] = str(target_path)

        if target_path.exists() and target_path.stat().st_size > 0:
            record.pop("download_error", None)
            reused += 1
            continue

        request = Request(
            str(record["url"]),
            headers={
                "Referer": referer,
                "User-Agent": "Mozilla/5.0",
            },
        )
        try:
            with urlopen(request) as response, temp_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            temp_path.replace(target_path)
            record.pop("download_error", None)
            downloaded += 1
        except Exception as exc:
            failed += 1
            record["download_error"] = str(exc)
            if temp_path.exists():
                temp_path.unlink()

    return downloaded, reused, failed


def media_record_is_available(record: dict[str, Any]) -> bool:
    local_path = str(record.get("local_path") or "").strip()
    if not local_path or record.get("download_error"):
        return False
    return Path(local_path).exists()


def prepare_media_records_for_markdown(
    media_records: list[dict[str, Any]] | None,
    markdown_parent: Path,
) -> list[dict[str, Any]] | None:
    if media_records is None:
        return None

    prepared: list[dict[str, Any]] = []
    for record in media_records:
        prepared_record = dict(record)
        if media_record_is_available(record):
            prepared_record["local_markdown_path"] = Path(
                os.path.relpath(str(record["local_path"]), start=markdown_parent)
            ).as_posix()
        prepared.append(prepared_record)
    return prepared


def build_history_entry(
    *,
    tweet: dict[str, Any],
    captures: list[dict[str, Any]],
    tweet_path: Path,
    output_root: Path,
    media_records: list[dict[str, Any]] | None,
    previous_entry: dict[str, Any] | None,
    media_export_requested: bool,
) -> dict[str, Any]:
    if not isinstance(tweet, dict):
        raise TypeError("tweet must be a dictionary")

    previous_media_files = (
        previous_entry.get("media_files")
        if isinstance(previous_entry, dict)
        and isinstance(previous_entry.get("media_files"), list)
        else []
    )
    if media_records is None:
        media_count = (
            safe_int(previous_entry.get("media_count"), 0) if previous_entry else 0
        )
        if media_count == 0:
            media_count = len(restore.extract_tweet_media(tweet))
        media_files = previous_media_files
    else:
        media_count = len(media_records)
        media_files = [
            {
                "filename": str(record["filename"]),
                "path": make_stored_path(Path(str(record["local_path"])), output_root),
                "url": str(record["url"]),
                "type": str(record.get("type") or "media"),
            }
            for record in media_records
            if media_record_is_available(record)
        ]

    previous_media_exported = bool(previous_entry and previous_entry.get("media_exported"))
    media_exported_now = media_count == 0 or (
        media_export_requested and len(media_files) >= media_count
    )
    captured_at = restore.first_capture_datetime(captures)

    return {
        "tweet_id": restore.get_tweet_id(tweet),
        "markdown_path": make_stored_path(tweet_path, output_root),
        "screen_name": restore.get_tweet_screen_name(tweet) or "unknown",
        "display_name": (
            restore.get_tweet_display_name(tweet)
            or restore.get_tweet_screen_name(tweet)
            or "unknown"
        ),
        "url": restore.get_tweet_url(tweet),
        "created_at": restore.get_created_datetime(tweet).isoformat(),
        "updated_at": restore.get_updated_datetime(tweet).isoformat(),
        "first_captured_at": captured_at.isoformat() if captured_at else None,
        "exported_at": datetime.now().astimezone().isoformat(),
        "media_count": media_count,
        "media_exported": previous_media_exported or media_exported_now,
        "media_files": media_files,
    }


def render_export_index(
    *,
    history_entries: list[dict[str, Any]],
    output_root: Path,
    input_path: str,
    document_title: str,
    database_name: str | None,
    selected_extensions: set[str] | None,
    selected_count: int,
    written_count: int,
    skipped_count: int,
    sort_by: str,
    descending: bool,
) -> str:
    visible_entries = [
        entry
        for entry in history_entries
        if isinstance(entry, dict) and markdown_artifact_exists(entry, output_root)
    ]
    visible_entries.sort(
        key=lambda entry: history_entry_sort_key(entry, sort_by),
        reverse=descending,
    )

    lines: list[str] = []
    lines.append(f"# {document_title}")
    lines.append("")
    lines.append(f"- Generated at: {restore.format_dt(datetime.now().astimezone())}")
    lines.append(f"- Source: `{input_path}`")
    if database_name:
        lines.append(f"- IndexedDB database: `{database_name}`")
    if selected_extensions:
        lines.append(
            "- Capture filter: "
            + ", ".join(f"`{name}`" for name in sorted(selected_extensions))
        )
    lines.append(f"- Selected tweets this run: {selected_count}")
    lines.append(f"- Newly written this run: {written_count}")
    lines.append(f"- Skipped by history: {skipped_count}")
    lines.append(f"- Archived tweet count: {len(visible_entries)}")

    if not visible_entries:
        lines.append("")
        lines.append("_No archived tweets are available yet._")
        lines.append("")
        return "\n".join(lines)

    lines.append("")
    lines.append("## Archived Tweets")
    lines.append("")
    for entry in visible_entries:
        lines.append(render_index_entry(entry))

    lines.append("")
    return "\n".join(lines)


def history_entry_sort_key(entry: dict[str, Any], sort_by: str) -> tuple[float, str]:
    if sort_by == "captured":
        value = parse_history_datetime(entry.get("first_captured_at"))
    elif sort_by == "updated":
        value = parse_history_datetime(entry.get("updated_at"))
    else:
        value = parse_history_datetime(entry.get("created_at"))
    return (value.timestamp(), str(entry.get("tweet_id") or ""))


def parse_history_datetime(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.fromtimestamp(0).astimezone()


def render_index_entry(entry: dict[str, Any]) -> str:
    created_at = restore.format_dt(parse_history_datetime(entry.get("created_at")))
    screen_name = str(entry.get("screen_name") or "unknown")
    display_name = str(entry.get("display_name") or screen_name)
    tweet_id = str(entry.get("tweet_id") or "")
    markdown_path = str(entry.get("markdown_path") or "")
    media_count = safe_int(entry.get("media_count"), 0)
    media_status = ""
    if media_count > 0:
        media_status = (
            f" · {media_count} media"
            if entry.get("media_exported")
            else f" · {media_count} media pending"
        )

    label = restore.escape_link_label(f"{created_at} — @{screen_name}")
    line = (
        f"- [{label}]({markdown_path})"
        f" · {restore.escape_inline(display_name)}"
        f" · `{tweet_id}`"
    )
    url = str(entry.get("url") or "").strip()
    if url:
        line += f" · {url}"
    line += media_status
    return line


def build_media_records(
    tweets: list[dict[str, Any]],
    filename_pattern: str,
) -> list[dict[str, Any]]:
    records_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    used_filenames: set[str] = set()

    for tweet in tweets:
        media_items = restore.extract_tweet_media(tweet)
        if not media_items:
            continue

        tweet_id = restore.get_tweet_id(tweet)
        screen_name = restore.get_tweet_screen_name(tweet) or "unknown"
        display_name = restore.get_tweet_display_name(tweet) or screen_name
        created_at = restore.get_created_datetime(tweet)

        for index, media in enumerate(media_items):
            original_url = restore.get_media_original_url(media)
            if not original_url:
                continue
            media_type = str(media.get("type") or "media")
            extension = get_file_extension_from_url(original_url)
            filename = apply_media_filename_pattern(
                filename_pattern,
                {
                    "id": tweet_id,
                    "screen_name": screen_name,
                    "name": display_name,
                    "index": str(index),
                    "num": str(index + 1),
                    "date": created_at.strftime("%Y%m%d"),
                    "time": created_at.strftime("%H%M%S"),
                    "type": media_type,
                    "ext": extension,
                },
            )
            filename = uniquify_filename(filename, used_filenames)
            used_filenames.add(filename)
            records_by_key[(tweet_id, original_url)] = {
                "filename": filename,
                "url": original_url,
                "tweet_id": tweet_id,
                "type": media_type,
                "alt_text": str(media.get("ext_alt_text") or "").strip(),
            }

    return list(records_by_key.values())


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def write_aria2_input_file(
    *,
    media_records: list[dict[str, str]],
    output_path: Path,
    media_dir: Path,
    referer: str,
) -> None:
    lines: list[str] = []
    media_dir_str = str(media_dir)
    for record in media_records:
        lines.append(record["url"])
        lines.append(f"  dir={media_dir_str}")
        lines.append(f"  out={record['filename']}")
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


def origin_referer(origin: str | None) -> str:
    return origin or "https://x.com/"


if __name__ == "__main__":
    raise SystemExit(main())
