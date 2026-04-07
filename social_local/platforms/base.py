from __future__ import annotations

from abc import ABC, abstractmethod
from argparse import ArgumentParser, Namespace
from typing import Any
from urllib.parse import urlparse

from ..models import ExportContext, PlatformPayload, SelectionResult


class PlatformAdapter(ABC):
    key = ""
    display_name = ""
    record_label_singular = "record"
    record_label_plural = "records"
    supported_origins: tuple[str, ...] = ()
    probe_paths: tuple[str, ...] = ("/robots.txt", "/", "/home")
    store_names: tuple[str, ...] = ()

    def add_arguments(self, parser: ArgumentParser) -> None:
        del parser

    def default_document_title(self) -> str:
        return f"{self.display_name} Export Restore"

    def default_db_prefix(self) -> str:
        return self.key

    def resolve_document_title(self, value: str | None) -> str:
        return value or self.default_document_title()

    def resolve_db_prefix(self, value: str | None) -> str:
        return value or self.default_db_prefix()

    def iter_probe_urls(self, origin: str | None) -> list[str]:
        domains = self._resolve_origins(origin)
        urls: list[str] = []
        for domain in domains:
            if "://" in domain:
                parsed = urlparse(domain)
                base = f"{parsed.scheme}://{parsed.netloc}"
                if parsed.path and parsed.path not in {"", "/"}:
                    urls.append(domain.rstrip("/"))
            else:
                base = f"https://{domain.strip('/')}"

            for path in self.probe_paths:
                if path == "/":
                    urls.append(base + "/")
                else:
                    urls.append(base + path)
        return dedupe_preserve_order(urls)

    def build_context(
        self,
        *,
        document_title: str,
        source_label: str,
        database_name: str | None,
        selected_filters: list[str],
    ) -> ExportContext:
        return ExportContext(
            platform_key=self.key,
            platform_name=self.display_name,
            record_label_singular=self.record_label_singular,
            record_label_plural=self.record_label_plural,
            document_title=document_title,
            source_label=source_label,
            database_name=database_name,
            selected_filters=selected_filters,
        )

    @abstractmethod
    def aggregate_browser_databases(
        self,
        databases: list[dict[str, Any]],
    ) -> PlatformPayload:
        raise NotImplementedError

    @abstractmethod
    def load_export_payload(self, root: Any) -> PlatformPayload:
        raise NotImplementedError

    @abstractmethod
    def select_records(
        self,
        payload: PlatformPayload,
        args: Namespace,
    ) -> SelectionResult:
        raise NotImplementedError

    def _resolve_origins(self, origin: str | None) -> list[str]:
        if not origin or origin == "auto":
            return list(self.supported_origins)
        return [origin]


def dedupe_preserve_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
