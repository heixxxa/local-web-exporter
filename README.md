# twitter-local

从 Microsoft Edge 的 `twitter-web-exporter` IndexedDB 中读取 tweet，并导出为本地 Markdown 归档。

当前 `main.py` 的导出行为：

- 每条 tweet 会单独写入一个 Markdown 文件
- 默认保存导出历史，后续运行会跳过已导出的 tweet
- 可选下载媒体文件，或者生成 / 运行 `aria2` 下载任务
- 同时生成一个索引 Markdown，方便统一浏览

## 常用命令

只导出新的 tweet：

```bash
python main.py -o tweet.md
```

导出新的 tweet 并下载媒体文件：

```bash
python main.py -o tweet.md --export-media
```

用 `aria2` 下载媒体：

```bash
python main.py -o tweet.md --run-aria2
```

忽略历史记录并强制重建：

```bash
python main.py -o tweet.md --ignore-history
```

## 默认输出结构

当输出文件是 `tweet.md` 时，默认会生成：

```text
tweet.md
tweet.history.json
tweet_tweets/
  1234567890.md
tweet_media/
  tester_1234567890_photo_1_20260401.jpg
```

## 主要参数

- `--tweet-dir`: 指定单条 tweet Markdown 的输出目录
- `--history-file`: 指定导出历史 JSON 文件
- `--export-media`: 直接下载媒体文件
- `--media-dir`: 指定媒体文件目录
- `--aria2-input-file`: 只生成 aria2 任务文件
- `--run-aria2`: 生成 aria2 任务文件后立即执行
