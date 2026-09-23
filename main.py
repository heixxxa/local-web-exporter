#!/usr/bin/env python3

from __future__ import annotations

import argparse

from exporter.browser import default_edge_user_data_dir
from exporter.core import DEFAULT_MEDIA_FILENAME_PATTERN, run_indexeddb_export
from exporter.platforms import PLATFORM_ADAPTERS, PLATFORM_ALIASES, get_platform_adapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 Edge 的 IndexedDB 导出社交平台内容。",
    )
    all_platforms = sorted(set(PLATFORM_ADAPTERS) | set(PLATFORM_ALIASES))
    parser.add_argument(
        "platform",
        nargs="?",
        choices=all_platforms,
        help="导出平台：x、xiaohongshu、okjike（别名：twitter、xhs、jike），默认为 x。",
    )
    parser.add_argument(
        "--output",
        "-o",
        dest="output_md",
        default="output",
        help="输出目录或 Markdown 文件，默认为 output。",
    )
    parser.add_argument(
        "--profile",
        dest="edge_profile_directory",
        default="Default",
        help="Edge 配置目录，例如 Default 或 Profile 1。",
    )
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口。")
    parser.add_argument(
        "--media",
        dest="export_media",
        action="store_true",
        help="同时下载图片和视频。",
    )
    parser.add_argument(
        "--fresh",
        dest="ignore_history",
        action="store_true",
        help="忽略历史记录并重新导出。",
    )
    parser.add_argument(
        "--no-copy-indexeddb", dest="copy_indexeddb", action="store_false",
        help="直接读取原始 Edge 配置，省去复制耗时；使用前需完全关闭 Edge。",
    )

    # Keep the old interface working without crowding the normal help output.
    hidden = argparse.SUPPRESS
    parser.add_argument(
        "--platform",
        "-p",
        dest="platform_option",
        choices=all_platforms,
        help=hidden,
    )
    parser.add_argument(
        "--output-md",
        dest="output_md",
        default=argparse.SUPPRESS,
        help=hidden,
    )
    parser.add_argument("--record-dir", "--tweet-dir", dest="record_dir", help=hidden)
    parser.add_argument("--history-file", help=hidden)
    parser.add_argument("--output-json", help=hidden)
    parser.add_argument("--title", help=hidden)
    parser.add_argument("--sort-by", choices=("created", "captured", "updated"), default="created", help=hidden)
    parser.add_argument("--descending", action="store_true", help=hidden)
    parser.add_argument("--edge-user-data-dir", default=str(default_edge_user_data_dir()), help=hidden)
    parser.add_argument("--edge-profile-directory", dest="edge_profile_directory", help=hidden)
    parser.add_argument("--edge-executable-path", help=hidden)
    parser.add_argument("--origin", default="auto", help=hidden)
    parser.add_argument("--db-name", action="append", default=[], help=hidden)
    parser.add_argument("--db-prefix", help=hidden)
    parser.add_argument("--timeout-ms", type=int, default=15000, help=hidden)
    parser.add_argument("--keep-temp-profile", action="store_true", help=hidden)
    parser.add_argument("--ignore-history", dest="ignore_history", action="store_true", help=hidden)
    parser.add_argument("--export-media", dest="export_media", action="store_true", help=hidden)
    parser.add_argument("--aria2-input-file", help=hidden)
    parser.add_argument("--media-dir", help=hidden)
    parser.add_argument("--media-filename-pattern", default=DEFAULT_MEDIA_FILENAME_PATTERN, help=hidden)
    parser.add_argument("--run-aria2", action="store_true", help=hidden)
    parser.add_argument("--aria2-bin", default="aria2c", help=hidden)
    parser.add_argument("--aria2-max-concurrent-downloads", type=int, default=8, help=hidden)
    parser.add_argument("--aria2-split", type=int, default=8, help=hidden)
    parser.add_argument("--extensions", help=hidden)
    parser.add_argument("--author-handles", help=hidden)
    parser.add_argument("--keywords", help=hidden)
    parser.set_defaults(copy_indexeddb=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    platform = args.platform_option or args.platform or "x"
    return run_indexeddb_export(get_platform_adapter(platform), args)


if __name__ == "__main__":
    raise SystemExit(main())
