from __future__ import annotations

from ..core import PlatformAdapter
from .okjike import OkjikeAdapter
from .twitter import TwitterAdapter
from .xiaohongshu import XiaohongshuAdapter

PLATFORM_ADAPTERS: dict[str, PlatformAdapter] = {
    adapter.key: adapter
    for adapter in (
        TwitterAdapter(),
        XiaohongshuAdapter(),
        OkjikeAdapter(),
    )
}

PLATFORM_ALIASES: dict[str, str] = {
    "twitter": "x",
    "xiaohongshu": "xhs",
    "okjike": "jike",
}


def get_platform_adapter(platform_key: str) -> PlatformAdapter:
    normalized_key = PLATFORM_ALIASES.get(platform_key, platform_key)
    try:
        return PLATFORM_ADAPTERS[normalized_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported platform: {platform_key}") from exc
