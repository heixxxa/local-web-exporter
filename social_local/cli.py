from __future__ import annotations

import argparse

from .browser import default_edge_user_data_dir
from .exporter import DEFAULT_MEDIA_FILENAME_PATTERN, run_indexeddb_export
from .platforms import PLATFORM_ADAPTERS, PLATFORM_ALIASES, get_platform_adapter


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Use Playwright + Microsoft Edge to read IndexedDB content from supported "
            "social platforms and export it into Markdown."
        )
    )
    parser.add_argument(
        "--platform",
        "-p",
        choices=sorted(set(PLATFORM_ADAPTERS) | set(PLATFORM_ALIASES)),
        default="x",
        help="Which platform adapter to use.",
    )
    parser.add_argument(
        "--output-md",
        "-o",
        required=True,
        help=(
            "Path to the index Markdown file, or an output directory. "
            "When a directory is provided, the exporter creates a platform subfolder "
            "(twitter/xiaohongshu/jike) and writes default .md/.json files there."
        ),
    )
    parser.add_argument(
        "--record-dir",
        "--tweet-dir",
        dest="record_dir",
        help="Directory used to store one Markdown file per exported record.",
    )
    parser.add_argument(
        "--history-file",
        help="JSON file used to remember exported records and skip duplicates on future runs.",
    )
    parser.add_argument(
        "--output-json",
        help="Optional path to save the raw extracted IndexedDB payload as JSON.",
    )
    parser.add_argument(
        "--title",
        help="Document title written at the top of the Markdown file. Defaults to the platform title.",
    )
    parser.add_argument(
        "--sort-by",
        choices=("created", "captured", "updated"),
        default="created",
        help="Sort exported records by original post time, first capture time, or local update time.",
    )
    parser.add_argument(
        "--descending",
        action="store_true",
        help="Sort newest-first instead of oldest-first.",
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
        default="auto",
        help="Which site origin to probe. Use `auto` to let the platform adapter decide.",
    )
    parser.add_argument(
        "--db-name",
        action="append",
        default=[],
        help="Exact IndexedDB database name to read. Can be repeated or contain comma-separated values.",
    )
    parser.add_argument(
        "--db-prefix",
        help="Database name prefix used when auto-discovering IndexedDB databases. Defaults to the platform prefix.",
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
        help="Use the live Edge profile directly. This is faster, but may fail if Edge is using the profile.",
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
        help="Ignore the saved export history and regenerate record Markdown files.",
    )
    parser.add_argument(
        "--export-media",
        action="store_true",
        help="Download media files and link them from each exported Markdown file.",
    )
    parser.add_argument(
        "--aria2-input-file",
        help="Optional aria2 input file to generate for media downloads.",
    )
    parser.add_argument(
        "--media-dir",
        help="Directory used for downloaded media files and aria2 output targets.",
    )
    parser.add_argument(
        "--media-filename-pattern",
        default=DEFAULT_MEDIA_FILENAME_PATTERN,
        help=(
            "Filename pattern for media downloads. Available tokens: {id}, {author_handle}, "
            "{author_name}, {screen_name}, {name}, {index}, {num}, {date}, {time}, {type}, {ext}."
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

    for adapter in PLATFORM_ADAPTERS.values():
        adapter.add_arguments(parser)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    adapter = get_platform_adapter(args.platform)
    return run_indexeddb_export(adapter, args)
