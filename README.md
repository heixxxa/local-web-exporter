# twitter-local

从 Microsoft Edge 的 IndexedDB 中提取社交平台内容，并导出为本地 Markdown 归档。

## 当前状态

- `twitter`: 已完成适配，可正常导出
- `xiaohongshu`: 已完成首版适配，可导出 note-like 记录（基于常见 schema 的鲁棒解析）
- `okjike`: 已加入适配器骨架，尚未实现具体 IndexedDB schema 映射

## 新架构

项目已拆成“通用导出引擎 + 平台适配器”：

```text
main.py
social_local/
  cli.py
  browser.py
  exporter.py
  models.py
  rendering.py
  platforms/
    base.py
    twitter.py
    xiaohongshu.py
    okjike.py
```

职责划分：

- `social_local/browser.py`: Playwright + Edge + IndexedDB 读取
- `social_local/exporter.py`: 历史记录、媒体下载、索引生成、单文件导出
- `social_local/rendering.py`: 平台无关的 Markdown 渲染
- `social_local/platforms/*.py`: 平台专属 schema 解析、记录选择、字段归一化

这意味着后续要接入新平台时，主要只需要新增一个适配器，而不是修改主流程。

## 常用命令

导出 Twitter 的新增内容：

```bash
python main.py --platform twitter -o output/twitter.md
```

导出小红书内容（可用别名 `xhs` 或 `xiaohongshu`）：

```bash
python main.py --platform xiaohongshu -o output/xhs.md
```

导出 Twitter 并下载媒体：

```bash
python main.py --platform twitter -o output/twitter.md --export-media
```

生成并执行 `aria2` 下载任务：

```bash
python main.py --platform twitter -o output/twitter.md --run-aria2
```

忽略历史记录并强制重建：

```bash
python main.py --platform twitter -o output/twitter.md --ignore-history
```

## 默认输出结构

当输出文件是 `output/twitter.md` 时，默认会生成：

```text
output/
  twitter.md
  twitter.history.json
  twitter_tweets/
    1234567890.md
  twitter_media/
    tester_1234567890_photo_1_20260401.jpg
```

## 主要参数

- `--platform`: 选择平台适配器
- `--record-dir` / `--tweet-dir`: 指定单条内容 Markdown 的输出目录
- `--history-file`: 指定导出历史 JSON 文件
- `--export-media`: 直接下载媒体文件
- `--media-dir`: 指定媒体文件目录
- `--aria2-input-file`: 只生成 aria2 任务文件
- `--run-aria2`: 生成 aria2 任务文件后立即执行
- `--db-prefix`: 覆盖平台默认的 IndexedDB 前缀
- `--origin`: 覆盖平台默认的站点 origin

## 如何扩展新平台

新增平台时，重点实现 `social_local/platforms/<platform>.py` 中的适配器：

1. 定义平台的 `supported_origins`、`store_names`、默认标题和 DB 前缀
2. 在 `aggregate_browser_databases()` 中把原始 IndexedDB 表聚合为平台 payload
3. 在 `load_export_payload()` 中支持该平台的 JSON 导入格式
4. 在 `select_records()` 中把原始平台数据转换成统一的 `ExportRecord`
5. 在 `social_local/platforms/__init__.py` 注册适配器

`xiaohongshu.py` 和 `okjike.py` 已经是这个骨架，可以直接在上面继续实现。
