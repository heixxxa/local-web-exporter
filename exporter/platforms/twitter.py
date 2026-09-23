from __future__ import annotations

import argparse
import json
import re
import sys
from argparse import Namespace
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from ..core import (
    ExportRecord,
    MediaAsset,
    PlatformAdapter,
    PlatformPayload,
    QuoteRecord,
    SelectionResult,
    format_dt,
    render_record_lines,
    render_records_markdown,
    render_single_record_markdown,
)

SUPPORTED_CAPTURE_TYPES = {"tweet", None}


class TwitterAdapter(PlatformAdapter):
    key = "x"
    display_name = "X"
    record_label_singular = "tweet"
    record_label_plural = "tweets"
    supported_origins = ("x.com", "twitter.com")
    store_names = ("tweets", "captures")

    def browser_read_options(
        self, args: Namespace, existing_ids: set[str]
    ) -> dict[str, Any]:
        return {
            "kind": "twitter",
            "extensions": sorted(parse_extensions(args.extensions) or []),
            "existingIds": sorted(existing_ids),
        }

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--extensions",
            help=(
                "Comma-separated capture extensions to keep, such as "
                "BookmarksModule,LikesModule,UserTweetsModule."
            ),
        )

    def default_document_title(self) -> str:
        return "X Export Restore"

    def default_db_prefix(self) -> str:
        package_json = Path(__file__).resolve().parents[2] / "package.json"
        try:
            return str(json.loads(package_json.read_text(encoding="utf-8"))["name"])
        except Exception:
            return "twitter-web-exporter"

    def aggregate_browser_databases(
        self,
        databases: list[dict[str, Any]],
    ) -> PlatformPayload:
        tweet_map: dict[str, dict[str, Any]] = {}
        captures: list[dict[str, Any]] = []
        database_names: list[str] = []

        for database in databases:
            name = str(database.get("name") or "")
            if name:
                database_names.append(name)

            tables = (
                database.get("tables")
                if isinstance(database.get("tables"), dict)
                else {}
            )
            for tweet in tables.get("tweets", []):
                if not isinstance(tweet, dict):
                    continue
                tweet_id = get_tweet_id(tweet)
                if not tweet_id:
                    continue

                current = tweet_map.get(tweet_id)
                if current is None or get_updated_datetime(
                    tweet
                ) > get_updated_datetime(current):
                    tweet_map[tweet_id] = tweet

            for capture in tables.get("captures", []):
                if not isinstance(capture, dict):
                    continue
                merged_capture = dict(capture)
                if name:
                    merged_capture["__database_name__"] = name
                captures.append(merged_capture)

        return PlatformPayload(
            database_names=sorted(set(database_names)),
            tables={
                "tweets": list(tweet_map.values()),
                "captures": captures,
            },
        )

    def load_export_payload(self, root: Any) -> PlatformPayload:
        payload = load_payload(root)
        database_name = payload.get("database_name")
        database_names = [str(database_name)] if database_name else []
        return PlatformPayload(
            database_names=database_names,
            tables={
                "tweets": list(payload["tweets"]),
                "captures": list(payload["captures"]),
            },
        )

    def select_records(
        self,
        payload: PlatformPayload,
        args: Namespace,
    ) -> SelectionResult:
        selected_extensions = parse_extensions(getattr(args, "extensions", None))
        tweets, captures_by_tweet = select_tweets(
            payload.tables.get("tweets", []),
            payload.tables.get("captures", []),
            selected_extensions,
        )
        records = [
            build_export_record(tweet, captures_by_tweet.get(get_tweet_id(tweet), []))
            for tweet in tweets
        ]
        return SelectionResult(
            records=records,
            selected_filters=sorted(selected_extensions) if selected_extensions else [],
        )


def build_export_record(
    tweet: dict[str, Any],
    captures: list[dict[str, Any]],
) -> ExportRecord:
    screen_name = get_tweet_screen_name(tweet) or "unknown"
    display_name = get_tweet_display_name(tweet) or screen_name
    retweet = extract_retweeted_tweet(tweet)
    quote = extract_quoted_tweet(tweet)
    quote_record = None
    if quote:
        quote_record = QuoteRecord(
            author_handle=get_tweet_screen_name(quote) or "unknown",
            text=render_tweet_text(quote) or "No text content available.",
            created_at=get_created_datetime(quote),
            url=get_tweet_url(quote),
        )

    media_assets = [
        MediaAsset(
            url=get_media_original_url(media),
            media_type=str(media.get("type") or "media"),
            alt_text=str(media.get("ext_alt_text") or "").strip(),
        )
        for media in extract_tweet_media(tweet)
        if get_media_original_url(media)
    ]

    extensions = sorted(
        {
            str(capture.get("extension"))
            for capture in captures
            if capture.get("extension")
        }
    )
    retweet_url = get_tweet_url(retweet) if retweet else None
    retweet_screen_name = get_tweet_screen_name(retweet) if retweet else None

    return ExportRecord(
        record_id=get_tweet_id(tweet),
        author_handle=screen_name,
        author_name=display_name,
        created_at=get_created_datetime(tweet),
        updated_at=get_updated_datetime(tweet),
        body_markdown=render_tweet_text(tweet),
        url=get_tweet_url(tweet),
        captured_by=extensions,
        first_captured_at=first_capture_datetime(captures),
        stats_line=build_stats_line(tweet),
        reply_url=get_reply_url(tweet),
        repost_source_handle=retweet_screen_name
        if retweet_url and retweet_screen_name
        else None,
        repost_source_url=retweet_url,
        quote=quote_record,
        media=media_assets,
        extra={
            "repost_label": "Retweet source",
            "quote_heading": "Quoted Tweet",
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Convert twitter-web-exporter IndexedDB exports (Dexie JSON) into a readable "
            "Markdown archive."
        )
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="Path to the exported JSON file created by the script's `Export DB` button.",
    )
    parser.add_argument(
        "--output",
        "-o",
        required=True,
        help="Path to the Markdown file to write.",
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
        "--title",
        default="Twitter Export Restore",
        help="Document title written at the top of the Markdown file.",
    )
    return parser


def legacy_main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)

    try:
        root = json.loads(input_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON input: {exc}", file=sys.stderr)
        return 1

    try:
        payload = load_payload(root)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    selected_extensions = parse_extensions(args.extensions)
    tweets, captures_by_tweet = select_tweets(
        payload["tweets"],
        payload["captures"],
        selected_extensions,
    )
    tweets.sort(
        key=lambda tweet: tweet_sort_key(
            tweet,
            captures_by_tweet.get(get_tweet_id(tweet), []),
            args.sort_by,
        ),
        reverse=args.descending,
    )

    markdown = render_markdown(
        tweets=tweets,
        captures_by_tweet=captures_by_tweet,
        input_path=input_path,
        document_title=args.title,
        database_name=payload.get("database_name"),
        selected_extensions=selected_extensions,
    )
    output_path.write_text(markdown, encoding="utf-8")
    print(f"Wrote {len(tweets)} tweets to {output_path}")
    return 0


def load_payload(root: Any) -> dict[str, Any]:
    if isinstance(root, dict) and root.get("formatName") == "dexie":
        return parse_dexie_export(root)

    if isinstance(root, list):
        tweets = [row for row in root if looks_like_tweet(row)]
        if not tweets:
            raise ValueError(
                "JSON array detected, but it does not contain raw tweet objects."
            )
        return {"tweets": tweets, "captures": [], "database_name": None}

    if isinstance(root, dict) and isinstance(root.get("tweets"), list):
        tweets = [row for row in root["tweets"] if looks_like_tweet(row)]
        captures = (
            root.get("captures") if isinstance(root.get("captures"), list) else []
        )
        return {
            "tweets": tweets,
            "captures": captures,
            "database_name": root.get("database_name"),
        }

    raise ValueError(
        "Unsupported JSON format. Please use the script's `Export DB` button to export IndexedDB first."
    )


def parse_dexie_export(root: dict[str, Any]) -> dict[str, Any]:
    data = root.get("data")
    if not isinstance(data, dict):
        raise ValueError("Dexie export is missing the top-level `data` section.")

    table_blocks = data.get("data")
    if not isinstance(table_blocks, list):
        raise ValueError("Dexie export is missing the table rows in `data.data`.")

    table_map: dict[str, list[dict[str, Any]]] = {}
    for block in table_blocks:
        if not isinstance(block, dict):
            continue
        table_name = block.get("tableName")
        if not isinstance(table_name, str):
            continue
        table_map[table_name] = normalize_dexie_rows(block)

    tweets = [row for row in table_map.get("tweets", []) if looks_like_tweet(row)]
    captures = [row for row in table_map.get("captures", []) if isinstance(row, dict)]
    if not tweets:
        raise ValueError("No tweets were found in the Dexie export.")

    return {
        "tweets": tweets,
        "captures": captures,
        "database_name": data.get("databaseName"),
    }


def normalize_dexie_rows(block: dict[str, Any]) -> list[dict[str, Any]]:
    inbound = bool(block.get("inbound", True))
    rows = block.get("rows")
    if not isinstance(rows, list):
        return []

    normalized: list[dict[str, Any]] = []
    for row in rows:
        if inbound and isinstance(row, dict):
            normalized.append(row)
            continue

        if not inbound and isinstance(row, list) and len(row) == 2:
            key, value = row
            if isinstance(value, dict):
                normalized_row = dict(value)
                normalized_row.setdefault("__dexie_key__", key)
                normalized.append(normalized_row)
            else:
                normalized.append({"__dexie_key__": key, "value": value})
    return normalized


def parse_extensions(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    values = {item.strip() for item in raw.split(",") if item.strip()}
    return values or None


def select_tweets(
    tweets: list[dict[str, Any]],
    captures: list[dict[str, Any]],
    selected_extensions: set[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    tweet_map = {get_tweet_id(tweet): tweet for tweet in tweets if get_tweet_id(tweet)}
    captures_by_tweet: dict[str, list[dict[str, Any]]] = {}

    for capture in captures:
        if not isinstance(capture, dict):
            continue
        if capture.get("type") not in SUPPORTED_CAPTURE_TYPES:
            continue
        tweet_id = str(capture.get("data_key") or "").strip()
        if not tweet_id:
            continue
        if selected_extensions and capture.get("extension") not in selected_extensions:
            continue
        captures_by_tweet.setdefault(tweet_id, []).append(capture)

    if selected_extensions:
        selected_ids = [
            tweet_id for tweet_id in captures_by_tweet if tweet_id in tweet_map
        ]
        return [tweet_map[tweet_id] for tweet_id in selected_ids], captures_by_tweet

    return list(tweet_map.values()), captures_by_tweet


def render_markdown(
    *,
    tweets: list[dict[str, Any]],
    captures_by_tweet: dict[str, list[dict[str, Any]]],
    input_path: Path | str,
    document_title: str,
    database_name: str | None,
    selected_extensions: set[str] | None,
) -> str:
    adapter = TwitterAdapter()
    context = adapter.build_context(
        document_title=document_title,
        source_label=str(input_path),
        database_name=database_name,
        selected_filters=sorted(selected_extensions) if selected_extensions else [],
    )
    records = [
        build_export_record(tweet, captures_by_tweet.get(get_tweet_id(tweet), []))
        for tweet in tweets
    ]
    return render_records_markdown(context=context, records=records)


def render_single_tweet_markdown(
    *,
    tweet: dict[str, Any],
    captures: list[dict[str, Any]],
    input_path: Path | str,
    document_title: str,
    database_name: str | None,
    selected_extensions: set[str] | None,
    media_records: list[dict[str, Any]] | None = None,
) -> str:
    adapter = TwitterAdapter()
    context = adapter.build_context(
        document_title=document_title,
        source_label=str(input_path),
        database_name=database_name,
        selected_filters=sorted(selected_extensions) if selected_extensions else [],
    )
    record = build_export_record(tweet, captures)
    if media_records is not None:
        record.media = [media_asset_from_mapping(item) for item in media_records]
    return render_single_record_markdown(context=context, record=record)


def render_tweet_section(
    index: int,
    tweet: dict[str, Any],
    captures: list[dict[str, Any]],
    media_records: list[dict[str, Any]] | None = None,
) -> list[str]:
    adapter = TwitterAdapter()
    record = build_export_record(tweet, captures)
    if media_records is not None:
        record.media = [media_asset_from_mapping(item) for item in media_records]
    context = adapter.build_context(
        document_title=adapter.default_document_title(),
        source_label="twitter",
        database_name=None,
        selected_filters=[],
    )
    return render_record_lines(
        context=context,
        record=record,
        heading_level=2,
        heading_text=f"{index}. {format_dt(record.created_at)} — @{record.author_handle}",
    )


def media_asset_from_mapping(value: dict[str, Any]) -> MediaAsset:
    return MediaAsset(
        url=str(value.get("url") or "").strip(),
        media_type=str(value.get("type") or value.get("media_type") or "media"),
        alt_text=str(value.get("alt_text") or "").strip(),
        filename=str(value.get("filename") or "").strip(),
        local_path=str(value.get("local_path") or "").strip(),
        local_markdown_path=str(value.get("local_markdown_path") or "").strip(),
        download_error=str(value.get("download_error") or "").strip(),
    )


def build_stats_line(tweet: dict[str, Any]) -> str:
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    views = tweet.get("views") if isinstance(tweet.get("views"), dict) else {}
    parts = [
        f"❤ {legacy.get('favorite_count', 0)}",
        f"🔁 {legacy.get('retweet_count', 0)}",
        f"💬 {legacy.get('reply_count', 0)}",
        f"❝ {legacy.get('quote_count', 0)}",
        f"🔖 {legacy.get('bookmark_count', 0)}",
    ]
    if views.get("count") is not None:
        parts.append(f"👁 {views.get('count')}")
    return " · ".join(parts)


def render_tweet_text(tweet: dict[str, Any]) -> str:
    note_tweet = get_note_tweet(tweet)
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}

    if note_tweet:
        text = str(note_tweet.get("text") or "")
        entities = (
            note_tweet.get("entity_set")
            if isinstance(note_tweet.get("entity_set"), dict)
            else {}
        )
    else:
        text = str(legacy.get("full_text") or "")
        entities = (
            legacy.get("entities") if isinstance(legacy.get("entities"), dict) else {}
        )

    quoted_permalink = None
    quoted_status_permalink = legacy.get("quoted_status_permalink")
    if isinstance(quoted_status_permalink, dict):
        quoted_permalink = quoted_status_permalink.get("expanded")

    text = replace_url_entities(text, entities.get("urls"), quoted_permalink)
    text = strip_media_shortlinks(text, legacy)
    return cleanup_text(text)


def replace_url_entities(text: str, urls: Any, quoted_permalink: str | None) -> str:
    if not isinstance(urls, list):
        return text

    ordered_entities = sorted(
        [entity for entity in urls if isinstance(entity, dict)],
        key=lambda entity: len(str(entity.get("url") or "")),
        reverse=True,
    )
    for entity in ordered_entities:
        short_url = str(entity.get("url") or "")
        if not short_url:
            continue
        expanded_url = str(entity.get("expanded_url") or short_url)
        display_url = str(entity.get("display_url") or expanded_url)

        if quoted_permalink and expanded_url == quoted_permalink:
            replacement = ""
        else:
            replacement = f"[{escape_link_label(display_url)}]({expanded_url})"
        text = text.replace(short_url, replacement)
    return text


def strip_media_shortlinks(text: str, legacy: dict[str, Any]) -> str:
    entities = (
        legacy.get("entities") if isinstance(legacy.get("entities"), dict) else {}
    )
    media_entities = (
        entities.get("media") if isinstance(entities.get("media"), list) else []
    )
    short_urls = sorted(
        [
            str(media.get("url") or "")
            for media in media_entities
            if isinstance(media, dict)
        ],
        key=len,
        reverse=True,
    )
    for short_url in short_urls:
        if short_url:
            text = text.replace(short_url, "")
    return text


def cleanup_text(text: str) -> str:
    lines = [re.sub(r"[ \t]+$", "", line) for line in text.splitlines()]
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = re.sub(r" {2,}", " ", cleaned)
    return cleaned.strip()


def get_note_tweet(tweet: dict[str, Any]) -> dict[str, Any] | None:
    note_tweet = tweet.get("note_tweet")
    if not isinstance(note_tweet, dict):
        return None
    note_results = note_tweet.get("note_tweet_results")
    if not isinstance(note_results, dict):
        return None
    result = note_results.get("result")
    return result if isinstance(result, dict) else None


def extract_retweeted_tweet(tweet: dict[str, Any]) -> dict[str, Any] | None:
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    source = legacy.get("retweeted_status_result")
    if not isinstance(source, dict):
        return None
    return extract_tweet_union(source.get("result"))


def extract_quoted_tweet(tweet: dict[str, Any]) -> dict[str, Any] | None:
    source = tweet.get("quoted_status_result")
    if not isinstance(source, dict):
        return None
    return extract_tweet_union(source.get("result"))


def extract_tweet_union(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None

    typename = value.get("__typename")
    if typename == "Tweet" and isinstance(value.get("legacy"), dict):
        return value
    if typename == "TweetWithVisibilityResults":
        tweet = value.get("tweet")
        if isinstance(tweet, dict) and isinstance(tweet.get("legacy"), dict):
            return tweet
    return None


def extract_tweet_media(tweet: dict[str, Any]) -> list[dict[str, Any]]:
    real_tweet = extract_retweeted_tweet(tweet) or tweet
    legacy = (
        real_tweet.get("legacy") if isinstance(real_tweet.get("legacy"), dict) else {}
    )

    extended_entities = legacy.get("extended_entities")
    if isinstance(extended_entities, dict) and isinstance(
        extended_entities.get("media"), list
    ):
        return [item for item in extended_entities["media"] if isinstance(item, dict)]

    entities = legacy.get("entities")
    if isinstance(entities, dict) and isinstance(entities.get("media"), list):
        return [item for item in entities["media"] if isinstance(item, dict)]

    return []


def get_media_original_url(media: dict[str, Any]) -> str:
    media_type = media.get("type")
    if media_type in {"video", "animated_gif"}:
        video_info = (
            media.get("video_info") if isinstance(media.get("video_info"), dict) else {}
        )
        variants = (
            video_info.get("variants")
            if isinstance(video_info.get("variants"), list)
            else []
        )
        best_variant: dict[str, Any] | None = None
        best_bitrate = -1
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            bitrate = variant.get("bitrate")
            if isinstance(bitrate, int) and bitrate > best_bitrate:
                best_bitrate = bitrate
                best_variant = variant
        if isinstance(best_variant, dict) and best_variant.get("url"):
            return str(best_variant["url"])

    media_url = str(media.get("media_url_https") or media.get("media_url") or "")
    if not media_url:
        return ""
    return format_twitter_image(media_url, "orig")


def format_twitter_image(url: str, size: str = "orig") -> str:
    match = re.match(r"^(https?://pbs\.twimg\.com/media/.+)\.(\w+)$", url)
    if match:
        return f"{match.group(1)}?format={match.group(2)}&name={size}"
    return f"{url}?name={size}"


def get_tweet_id(tweet: dict[str, Any]) -> str:
    if tweet.get("rest_id"):
        return str(tweet["rest_id"])
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    if legacy.get("id_str"):
        return str(legacy["id_str"])
    return ""


def get_tweet_user(tweet: dict[str, Any]) -> dict[str, Any] | None:
    core = tweet.get("core")
    if not isinstance(core, dict):
        return None
    user_results = core.get("user_results")
    if not isinstance(user_results, dict):
        return None
    result = user_results.get("result")
    return result if isinstance(result, dict) else None


def get_tweet_screen_name(tweet: dict[str, Any]) -> str | None:
    user = get_tweet_user(tweet)
    if not user:
        return None
    user_core = user.get("core") if isinstance(user.get("core"), dict) else {}
    if user_core.get("screen_name"):
        return str(user_core["screen_name"])
    legacy = user.get("legacy") if isinstance(user.get("legacy"), dict) else {}
    if legacy.get("screen_name"):
        return str(legacy["screen_name"])
    return None


def get_tweet_display_name(tweet: dict[str, Any]) -> str | None:
    user = get_tweet_user(tweet)
    if not user:
        return None
    user_core = user.get("core") if isinstance(user.get("core"), dict) else {}
    if user_core.get("name"):
        return str(user_core["name"])
    legacy = user.get("legacy") if isinstance(user.get("legacy"), dict) else {}
    if legacy.get("name"):
        return str(legacy["name"])
    return None


def get_tweet_url(tweet: dict[str, Any] | None) -> str | None:
    if not isinstance(tweet, dict):
        return None
    screen_name = get_tweet_screen_name(tweet)
    tweet_id = get_tweet_id(tweet)
    if not screen_name or not tweet_id:
        return None
    return f"https://twitter.com/{screen_name}/status/{tweet_id}"


def get_reply_url(tweet: dict[str, Any]) -> str | None:
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    reply_screen_name = legacy.get("in_reply_to_screen_name")
    reply_tweet_id = legacy.get("in_reply_to_status_id_str")
    if not reply_screen_name or not reply_tweet_id:
        return None
    return f"https://twitter.com/{reply_screen_name}/status/{reply_tweet_id}"


def get_created_datetime(tweet: dict[str, Any]) -> datetime:
    legacy = tweet.get("legacy") if isinstance(tweet.get("legacy"), dict) else {}
    created_at = legacy.get("created_at")
    if isinstance(created_at, str):
        try:
            return parsedate_to_datetime(created_at).astimezone()
        except (TypeError, ValueError, IndexError):
            pass

    private_fields = (
        tweet.get("twe_private_fields")
        if isinstance(tweet.get("twe_private_fields"), dict)
        else {}
    )
    return from_millis(private_fields.get("created_at"))


def get_updated_datetime(tweet: dict[str, Any]) -> datetime:
    private_fields = (
        tweet.get("twe_private_fields")
        if isinstance(tweet.get("twe_private_fields"), dict)
        else {}
    )
    return from_millis(private_fields.get("updated_at"))


def first_capture_datetime(captures: list[dict[str, Any]]) -> datetime | None:
    timestamps = [
        capture.get("created_at")
        for capture in captures
        if capture.get("created_at") is not None
    ]
    if not timestamps:
        return None
    return from_millis(min(timestamps))


def tweet_sort_key(
    tweet: dict[str, Any],
    captures: list[dict[str, Any]],
    sort_by: str,
) -> tuple[float, str]:
    if sort_by == "captured":
        captured_at = first_capture_datetime(captures)
        return (captured_at.timestamp() if captured_at else 0.0, get_tweet_id(tweet))
    if sort_by == "updated":
        updated_at = get_updated_datetime(tweet)
        return (updated_at.timestamp(), get_tweet_id(tweet))
    created_at = get_created_datetime(tweet)
    return (created_at.timestamp(), get_tweet_id(tweet))


def from_millis(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000).astimezone()
    return datetime.fromtimestamp(0).astimezone()


def looks_like_tweet(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and bool(get_tweet_id(value))
        and isinstance(value.get("legacy"), dict)
    )


def escape_link_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("]", "\\]")


def escape_inline(value: str) -> str:
    return value.replace("\n", " ").strip()
