from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from .core import PlatformAdapter

INDEXEDDB_EVAL = r"""
async ({ dbPrefix, dbNames, storeNames, readOptions = {} }) => {
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

  // Keep requests inside cursor callbacks so transactions remain active.
  const scan = (source, query, visit, keysOnly = false) => new Promise((resolve, reject) => {
    const request = keysOnly ? source.openKeyCursor(query) : source.openCursor(query);
    request.onerror = () => reject(request.error);
    request.onsuccess = () => {
      const cursor = request.result;
      if (!cursor) { resolve(); return; }
      try { visit(cursor); cursor.continue(); } catch (error) { reject(error); }
    };
  });
  const existing = new Set(readOptions.existingIds || []);
  const extensions = new Set(readOptions.extensions || []);
  const skipped = new Set();
  const selectedIds = new Set();
  const tweetId = (row) => String(row?.rest_id || row?.legacy?.id_str || '');
  const readCaptures = async (db) => {
    if (!db.objectStoreNames.contains('captures')) return [];
    const tx = db.transaction('captures', 'readonly');
    const store = tx.objectStore('captures');
    const rows = [];
    const visit = (cursor) => {
      const row = cursor.value;
      if (!row || (row.type != null && row.type !== 'tweet')) return;
      if (extensions.size && !extensions.has(row.extension)) return;
      const id = String(row.data_key || '').trim();
      if (!id) return;
      selectedIds.add(id);
      if (!existing.has(id)) rows.push(row);
    };
    const indexName = Array.from(store.indexNames).find(
      (name) => store.index(name).keyPath === 'extension'
    );
    if (extensions.size && indexName) {
      await Promise.all(Array.from(extensions, (value) =>
        scan(store.index(indexName), IDBKeyRange.only(value), visit)));
    } else {
      await scan(store, null, visit);
    }
    return rows;
  };
  const readTweets = (db) => new Promise((resolve, reject) => {
    if (!db.objectStoreNames.contains('tweets')) { resolve([]); return; }
    const tx = db.transaction('tweets', 'readonly');
    const store = tx.objectStore('tweets');
    const rows = [];
    tx.oncomplete = () => resolve(rows);
    tx.onabort = () => reject(tx.error || new Error('Tweet transaction aborted'));
    tx.onerror = () => reject(tx.error || new Error('Failed to read tweets'));
    const accept = (row) => {
      const id = tweetId(row);
      if (!id || (extensions.size && !selectedIds.has(id))) return;
      if (existing.has(id)) skipped.add(id);
      else rows.push(row);
    };
    // rest_id is the canonical identity. Other schemas use a value cursor so
    // legacy.id_str fallback and mixed layouts retain Python's exact semantics.
    if (store.keyPath === 'rest_id' && extensions.size) {
      for (const id of selectedIds) {
        // Probe keys first; do not materialize already archived tweet bodies.
        const keys = [id];
        const numeric = Number(id);
        if (Number.isSafeInteger(numeric) && String(numeric) === id) keys.push(numeric);
        for (const key of keys) {
          const lookup = store.getKey(key);
          lookup.onsuccess = () => {
            if (lookup.result === undefined) return;
            if (existing.has(id)) { skipped.add(id); return; }
            const get = store.get(lookup.result);
            get.onsuccess = () => { if (get.result) accept(get.result); };
          };
        }
      }
    } else if (store.keyPath === 'rest_id') {
      const request = store.openKeyCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        const id = String(cursor.key);
        if (!extensions.size || selectedIds.has(id)) {
          if (existing.has(id)) skipped.add(id);
          else {
            const get = store.get(cursor.primaryKey);
            get.onsuccess = () => { if (get.result) accept(get.result); };
          }
        }
        cursor.continue();
      };
    } else {
      const request = store.openCursor();
      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        accept(cursor.value);
        cursor.continue();
      };
    }
  });

  const results = [];
  const opened = [];
  try {
    for (const name of names) {
      try {
        const db = await openDatabase(name);
        const result = { name, objectStores: Array.from(db.objectStoreNames), tables: {} };
        opened.push({ db, result });
        results.push(result);
      } catch (error) {
        results.push({ name, error: String(error) });
      }
    }
    if (readOptions.kind === 'twitter') {
      // Capture references can point to tweets held in another matching DB.
      for (const { db, result } of opened) {
        result.tables.captures = await readCaptures(db);
      }
      const returnedIds = new Set();
      for (const { db, result } of opened) {
        result.tables.tweets = await readTweets(db);
        for (const row of result.tables.tweets) returnedIds.add(tweetId(row));
      }
      for (const { result } of opened) {
        result.tables.captures = result.tables.captures.filter(
          (row) => returnedIds.has(String(row.data_key || '').trim()));
      }
    } else {
      for (const { db, result } of opened) {
        const requested = Array.isArray(storeNames) ? storeNames : [];
        const stores = requested.length
          ? requested.filter((name) => result.objectStores.includes(name))
          : result.objectStores;
        result.tables = Object.fromEntries(await Promise.all(stores.map(async (name) =>
          [name, await readStore(db, name)])));
      }
    }
    return {
      origin: location.origin, url: location.href,
      databases: results, skippedIds: Array.from(skipped),
    };
  } finally {
    for (const { db } of opened) db.close();
  }
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
    origins: list[str] | None = None,
):
    if not copy_indexeddb:
        yield source_root
        return

    with tempfile.TemporaryDirectory(prefix="social-local-edge-") as temp_dir:
        temp_root = Path(temp_dir)
        prepare_minimal_edge_profile_copy(source_root, temp_root, profile_directory, origins)
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
    origins: list[str] | None = None,
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
            if item_name == "IndexedDB" and origins:
                # Chromium stores all databases and blobs for an origin together.
                prefixes = set()
                for origin in origins:
                    parsed = urlparse(origin)
                    port = parsed.port
                    default_port = 443 if parsed.scheme == "https" else 80
                    storage_port = 0 if port is None or port == default_port else port
                    prefixes.add(f"{parsed.scheme}_{parsed.hostname}_{storage_port}.indexeddb.")
                target_item.mkdir(parents=True, exist_ok=True)
                for child in source_item.iterdir():
                    if not any(child.name.startswith(prefix) for prefix in prefixes):
                        continue
                    if child.is_dir():
                        shutil.copytree(child, target_item / child.name)
                    else:
                        shutil.copy2(child, target_item / child.name)
            else:
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
    read_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sync_playwright, _ = ensure_playwright_import()
    errors: list[str] = []

    for use_copy in [True, False] if copy_indexeddb else [False]:
        try:
            with maybe_copied_user_data_dir(
                source_root=edge_user_data_dir,
                profile_directory=edge_profile_directory,
                copy_indexeddb=use_copy,
                keep_temp_profile=keep_temp_profile,
                origins=adapter.iter_probe_urls(origin),
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
                        empty_origins: set[str] = set()
                        for url in adapter.iter_probe_urls(origin):
                            parsed_url = urlparse(url)
                            requested_origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
                            if requested_origin in empty_origins:
                                continue
                            try:
                                page.goto(
                                    url,
                                    wait_until="domcontentloaded",
                                    timeout=timeout_ms,
                                )
                                result = page.evaluate(
                                    INDEXEDDB_EVAL,
                                    {
                                        "dbPrefix": db_prefix,
                                        "dbNames": db_names,
                                        "storeNames": list(adapter.store_names),
                                        "readOptions": read_options or {},
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
                                if (
                                    result.get("origin") == requested_origin
                                    and not result.get("databases")
                                ):
                                    empty_origins.add(requested_origin)
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
                                        "skipped_ids": result.get("skippedIds", []),
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
