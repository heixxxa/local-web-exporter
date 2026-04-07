from __future__ import annotations

from argparse import Namespace
from typing import Any

from ..models import PlatformPayload, SelectionResult
from .base import PlatformAdapter


class OkjikeAdapter(PlatformAdapter):
    key = "jike"
    display_name = "OKJike"
    record_label_singular = "post"
    record_label_plural = "posts"
    supported_origins = ("web.okjike.com",)
    probe_paths = ("/", "/robots.txt", "/home")

    def aggregate_browser_databases(
        self,
        databases: list[dict[str, Any]],
    ) -> PlatformPayload:
        del databases
        raise NotImplementedError(
            "The OKJike adapter scaffold is in place, but its IndexedDB schema mapping has not been implemented yet."
        )

    def load_export_payload(self, root: Any) -> PlatformPayload:
        del root
        raise NotImplementedError(
            "The OKJike adapter scaffold is in place, but JSON payload parsing has not been implemented yet."
        )

    def select_records(
        self,
        payload: PlatformPayload,
        args: Namespace,
    ) -> SelectionResult:
        del payload, args
        raise NotImplementedError(
            "The OKJike adapter scaffold is in place, but record selection has not been implemented yet."
        )
