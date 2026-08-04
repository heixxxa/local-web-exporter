# local-web-exporter

从 Microsoft Edge 的 IndexedDB 中提取社交平台内容，并导出为本地 Markdown 归档。

## 当前状态

- `twitter`: 已完成适配，可正常导出
- `xiaohongshu`: 已完成首版适配，可导出 note-like 记录（基于常见 schema 的鲁棒解析）
- `okjike`: 已加入适配器骨架，尚未实现具体 IndexedDB schema 映射

## 项目结构

项目已拆成“通用导出引擎 + 平台适配器”：

```text
local-web-exporter/
  pyproject.toml
  README.md
  main.py
  exporter/
    __init__.py
    core.py
    browser.py
    platforms/
      __init__.py
      twitter.py
      xiaohongshu.py
      okjike.py
  tests/
```

职责划分：

- `main.py`: 命令行参数与程序入口
- `exporter/browser.py`: Playwright + Edge + IndexedDB 读取
- `exporter/core.py`: 数据模型、Markdown 渲染和通用导出流程
- `exporter/platforms/*.py`: 平台专属 schema 解析、记录选择和字段归一化

这意味着后续接入新平台时，主要只需新增一个适配器，而不用修改主流程。

## 常用命令

导出 Twitter 的新增内容：

```bash
python main.py twitter
```

导出小红书内容（可用别名 `xhs` 或 `xiaohongshu`）：

```bash
python main.py xhs
```

导出 Twitter 并下载媒体：

```bash
python main.py twitter --media
```

生成并执行 `aria2` 下载任务：

```bash
python main.py twitter --run-aria2
```

忽略历史记录并强制重建：

```bash
python main.py twitter --fresh
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

- `platform`: 可选，支持 `twitter`、`xhs`、`jike`，默认为 `twitter`
- `-o` / `--output`: 输出目录或 Markdown 文件，默认为 `output`
- `--profile`: Edge 配置目录，默认为 `Default`
- `--headed`: 显示浏览器窗口，便于登录或调试
- `--media`: 同时下载媒体文件
- `--fresh`: 忽略历史记录并重新导出

旧版高级参数仍然兼容，但不再显示在常规帮助中。

## 如何扩展新平台

新增平台时，重点实现 `exporter/platforms/<platform>.py` 中的适配器：

1. 定义平台的 `supported_origins`、`store_names`、默认标题和 DB 前缀
2. 在 `aggregate_browser_databases()` 中把原始 IndexedDB 表聚合为平台 payload
3. 在 `load_export_payload()` 中支持该平台的 JSON 导入格式
4. 在 `select_records()` 中把原始平台数据转换成统一的 `ExportRecord`
5. 在 `exporter/platforms/__init__.py` 注册适配器

`xiaohongshu.py` 和 `okjike.py` 已经是这个骨架，可以直接在上面继续实现。
