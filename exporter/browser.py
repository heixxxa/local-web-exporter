from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import PlatformAdapter

INDEXEDDB_EVAL = r"""
async ({ dbPrefix, dbNames, storeNames }) => {
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
      const tables = {};
            const availableStores = Array.from(db.objectStoreNames || []);
            const requestedStores = Array.isArray(storeNames)
                ? storeNames.filter((storeName) => availableStores.includes(storeName))
                : [];
            const storesToRead = requestedStores.length ? requestedStores : availableStores;

            for (const storeName of storesToRead) {
        tables[storeName] = await readStore(db, storeName);
      }
      results.push({
        name,
                objectStores: availableStores,
        tables,
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


def default_edge_user_data_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return Path("Microsoft/Edge/User Data")
    return Path(local_appdata) / "Microsoft" / "Edge" / "User Data"


def ensure_playwright_import() -> tuple[Any, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run `pip install playwright` first."
        ) from exc
    return sync_playwright, PlaywrightTimeoutError


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

    with tempfile.TemporaryDirectory(prefix="social-local-edge-") as temp_dir:
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
    source_root: Path,
    temp_root: Path,
    profile_directory: str,
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
    adapter: PlatformAdapter,
    edge_user_data_dir: Path,
    edge_profile_directory: str,
    edge_executable_path: Path | None,
    origin: str | None,
    db_prefix: str,
    db_names: list[str],
    timeout_ms: int,
    headed: bool,
    copy_indexeddb: bool,
    keep_temp_profile: bool,
) -> dict[str, Any]:
    sync_playwright, PlaywrightTimeoutError = ensure_playwright_import()
    errors: list[str] = []

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
                        for url in adapter.iter_probe_urls(origin):
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
                                        "storeNames": list(adapter.store_names),
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
                                            "platform": adapter.key,
                                        },
                                        "origin": result.get("origin"),
                                        "url": result.get("url"),
                                        "attempts": attempts,
                                        "databases": databases,
                                    }
                            except Exception as exc:
                                attempts.append(
                                    {"requested_url": url, "error": str(exc)}
                                )
                        raise RuntimeError(
                            "No matching IndexedDB databases were found for this Edge profile."
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
