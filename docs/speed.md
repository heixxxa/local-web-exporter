看了你现在的实现，慢的主要原因其实**不是 Playwright 本身**，而是当前数据路径做了几件非常重的事：

`复制整个 IndexedDB → 启动 Edge → 打开页面 → 等 networkidle → tweets.getAll() → users.getAll() → captures.getAll() → 整坨数据通过 Playwright 传回 Python → Python 再过滤`。其中 `getAll()` 会把整个 object store 的对象全部 materialize/structured-clone 出来；库大以后成本会很明显。

我会按下面这个顺序改，前 3 项收益最大：

1. **Twitter 不要读取 `users`。** 你现在 `TwitterAdapter.store_names = ("tweets", "users", "captures")`，但 `aggregate_browser_databases()` 实际只处理 `tweets` 和 `captures`，`users` 完全没用。也就是说整个 users store 被读取、clone、跨 Playwright RPC 传到 Python，然后直接丢掉。

直接改：

```python
class TwitterAdapter(PlatformAdapter):
    ...
    store_names = ("tweets", "captures")
```

如果 users 很多，这一行就可能明显提速。

2. **不要等 `networkidle`。** 你访问页面只是为了拿到该 origin 下的 IndexedDB，并不依赖 X 的 React、GraphQL 或页面资源加载。但现在 `goto(...domcontentloaded)` 后又额外：

```python
page.wait_for_load_state(
    "networkidle",
    timeout=min(timeout_ms, 4000)
)
```

对 X 这种持续发网络请求的网站，这一步很容易白等几秒。

直接删掉。甚至可以考虑：

```python
page.goto(
    url,
    wait_until="domcontentloaded",
    timeout=timeout_ms,
)
```

之后立即 `page.evaluate()`。

你默认 probe path 又有：

```python
("/robots.txt", "/", "/home")
```

所以失败情况下可能重复导航。IndexedDB 是按 origin 隔离，不需要真的打开 `/home` 才能访问数据库。

3. **不要先 `getAll()` 再在 Python 过滤。这是最大的结构性问题。**

你现在：

```js
const request = store.getAll();
```

每个目标 store 都整个读出来。

然后 Twitter 到 Python 以后才：

```python
select_tweets(
    payload.tables.get("tweets", []),
    payload.tables.get("captures", []),
    selected_extensions,
)
```

也就是说哪怕最终只需要 100 条 bookmark，也可能先把 5 万 / 20 万条 tweet 全传回来。

真正应该改成：

```text
captures
   ↓
先找出需要的 tweet IDs
   ↓
减去 history 中已经导出的 IDs
   ↓
tweets.get(id)
tweets.get(id)
tweets.get(id)
...
   ↓
只返回新增记录
```

这会让你的程序从：

```text
每次运行复杂度 ≈ 整个 IndexedDB 大小
```

变成：

```text
每次运行复杂度 ≈ 本次新增内容大小
```

这通常不是 20%～30% 的提升，而可能是**一个数量级甚至更多**。

你已经有 history 机制，但现在 history 加载的位置太晚了：先完整抽取 IndexedDB，之后才 `load_export_history()`。

应该把：

```python
history = load_export_history(history_path)
```

提前到：

```python
extract_indexeddb_payload(...)
```

之前，然后传进去：

```python
extracted = extract_indexeddb_payload(
    ...
    existing_ids=set(...),
)
```

浏览器里面则类似：

```js
async function getMany(store, ids) {
  return Promise.all(
    ids.map(id => new Promise((resolve, reject) => {
      const req = store.get(id);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    }))
  );
}
```

不过更好的方式是根据实际 Dexie schema 利用 index；这里我建议先检测一次：

```js
const tx = db.transaction(["tweets", "captures"], "readonly");

for (const name of ["tweets", "captures"]) {
  const store = tx.objectStore(name);

  console.log({
    name,
    keyPath: store.keyPath,
    autoIncrement: store.autoIncrement,
    indexes: Array.from(store.indexNames).map(name => {
      const index = store.index(name);
      return {
        name,
        keyPath: index.keyPath,
        unique: index.unique,
      };
    })
  });
}
```

如果 `captures` 有诸如：

```text
data_key
extension
created_at
updated_at
```

这样的 index，就更舒服，可以用 `IDBKeyRange` 直接查询新增数据，不需要连 captures 都 `getAll()`。IndexedDB 的 `getAll(query, count)` 和 index 查询本身就支持 key range。([Mozilla 开发者网络][1])

---

另外，你这里还有一个非常大的固定开销：**默认每次复制整个 Profile 的 IndexedDB**。

目前：

```python
parser.set_defaults(copy_indexeddb=True)
```

而：

```python
prepare_minimal_edge_profile_copy(...)
```

里面是：

```python
for item_name in ("IndexedDB", "Preferences"):
    ...
    shutil.copytree(source_item, target_item)
```

也就是每次启动都复制：

```text
Default/IndexedDB/
```

整个目录，而不是只复制 twitter-web-exporter 对应 DB。

你现在已经隐藏支持：

```bash
python main.py twitter --no-copy-indexeddb
```

Edge **完全关闭时**可以先测试这个。

如果比如当前：

```text
正常：
12 秒复制 Profile
+ 8 秒读取 IndexedDB
+ 1 秒输出
= 21 秒
```

那 `--no-copy-indexeddb` 很可能直接变成 9 秒左右。

问题是 Edge 正在运行时，persistent profile 通常会有 profile lock。因此长期方案我反而建议改变架构：

```text
现有 Edge
   │
   │ CDP / remote debugging
   ▼
Python
   │
   └── 直接操作该 tab 的 IndexedDB
```

这样每次都不用：

```text
复制 IndexedDB
启动 Edge
关闭 Edge
```

而是：

```text
连接已经运行的浏览器
→ evaluate IndexedDB
→ 退出
```

---

还有个很容易改的小优化。目前 store 是**串行读取**的：

```js
for (const storeName of storesToRead) {
    tables[storeName] = await readStore(db, storeName);
}
```

可以改成：

```js
const entries = await Promise.all(
  storesToRead.map(async (storeName) => [
    storeName,
    await readStore(db, storeName),
  ])
);

const tables = Object.fromEntries(entries);
```

于是：

```text
tweets ────────┐
               ├─ 同时读
captures ──────┘
```

而不是：

```text
tweets █████████████
                   captures ███████
```

不过这项我排在后面，因为如果两个 store 都超级大，并行 `getAll()` 反而会增加峰值内存。**先解决“少读数据”，再考虑并行。**

---

你的导出后半段也还有一个 O(N) 问题。虽然你有增量 history，所以 Markdown 文件只写新的，但每次最后仍会重新遍历整个 history、检查每个 Markdown 是否存在、重新生成完整 index，并把整个 history pretty-print 回 JSON：

```python
platform_history_entries = [
    ...
    if markdown_artifact_exists(...)
]

index_markdown = render_export_index(...)
output_md.write_text(index_markdown)

save_export_history(...)
```

如果最终到了几十万条，这块也会慢。

长期可以考虑：

```text
history.json
     ↓
SQLite
```

例如：

```sql
CREATE TABLE exported (
    platform TEXT,
    record_id TEXT,
    created_at INTEGER,
    updated_at INTEGER,
    markdown_path TEXT,
    PRIMARY KEY(platform, record_id)
);
```

这样判断：

```sql
SELECT 1
FROM exported
WHERE platform = ? AND record_id = ?
```

增量状态就不需要每次 load/save 一个越来越大的 JSON。

---

所以如果让我直接改这个项目，我会把结构改成：

```text
启动
 │
 ├─ load history
 │
 ▼
连接现有 Edge / 最小化 profile copy
 │
 ▼
打开 IndexedDB
 │
 ├─ captures:
 │    只查目标 extension
 │
 ├─ 得到 tweet IDs
 │
 ├─ 排除 history IDs
 │
 ▼
tweets:
按 ID / index 只取需要的数据
 │
 ▼
浏览器只返回新增 records
 │
 ▼
Python 转 Markdown
 │
 ▼
更新 history
```

而不是当前：

```text
整个 IndexedDB
   ↓
整个 IndexedDB
   ↓
整个 IndexedDB
   ↓
Python
   ↓
过滤掉 99%
```

**我认为最值得先做的四个修改是：**

1. `TwitterAdapter.store_names` 去掉 `users`
2. 删掉 `networkidle`
3. 默认不要复制整个 `IndexedDB`，至少测试 `--no-copy-indexeddb`
4. 最关键：把 history/extension 过滤推进 IndexedDB 查询阶段，避免 `tweets.getAll()`

其中第 **4** 才是能让数据库从 1 万条涨到 10 万、100 万条以后，执行时间仍然比较稳定的方案。

如果你愿意，我可以下一步直接按这个思路，**基于你 GitHub 当前代码给你写出 `browser.py + core.py + twitter.py` 的具体 patch/diff**，把读取方式改成真正的增量 IndexedDB 导出。

[1]: https://developer.mozilla.org/en-US/docs/Web/API/IDBObjectStore/getAll?utm_source=chatgpt.com "IDBObjectStore: getAll() method - Web APIs | MDN"
