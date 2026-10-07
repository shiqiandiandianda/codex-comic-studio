# Codex CLI 接入层

`codex_worker.py` 是一个只依赖 Python 标准库的后台 worker。它把“排版规划”和“面板生图”拆成两个任务类型，供编辑器在后台排队调用。主格、副格、气泡、底图和渐变仍然由上层编辑器作为独立对象保存；本层只负责把结构化输入交给 Codex，并把结果落到稳定的任务目录。

## 启动本地桥接服务

从仓库根目录运行：

```powershell
python services/server.py
```

然后打开 <http://127.0.0.1:8080/>。浏览器编辑器通过 `GET/PUT /api/project` 保存分话和页面，通过 `GET/POST /api/assets` 管理资产图，通过 `POST /api/jobs` 提交排版或生图任务，使用 `GET /api/jobs/{job_id}` 查看状态。服务未启动时，编辑器仍可使用本地保存和人工调整，但不会伪造 Codex 已完成。

`pipeline` 会先调用一次 `layout`，再根据结构化 `prompts` 为每个主格/副格调用 `image`；生成结果通过 `/api/artifacts/...` 提供给浏览器。每个任务记录 `page_id` 和 `expected_revision`，前端据此决定自动应用还是等待人工确认。

## 任务输入

结构约定在 [`schema.json`](schema.json)。最小排版输入示例：

```json
{
  "id": "ep01-layout",
  "episode_id": "ep01",
  "generation_standard": "黑白漫画，保留对白安全区",
  "pages": [{"id": "p01", "width": 1600, "height": 2560, "panels": []}]
}
```

面板生图输入至少包含 `id`、`prompt`（或由 worker 根据 JSON 生成）以及可选的 `assets: [{"path": "..."}]`。

## 调用

```powershell
python services/codex_worker.py layout `
  --input episode-layout.json `
  --output-dir .data/ep01/layout

python services/codex_worker.py image `
  --input panel-001.json `
  --asset assets/character-a.png `
  --output-dir .data/ep01/images
```

也可以直接传提示词，不提供输入 JSON：

```powershell
python services/codex_worker.py image `
  --prompt "雨夜的屋顶，角色回头，电影感构图" `
  --asset assets/character-a.png `
  --output-dir .data/ep01/images
```

排版输入还可以提供 `generation_standard_path`；worker 会读取 UTF-8 文档内容并将它放进排版提示词。

生图任务会在提示词前加入 `$imagegen`，并把每个参考图以 `-i` 传给 `codex exec --json`；排版任务会读取 `layout_reference_paths` 和 `asset_paths` 作为布局参考。排版命令同时使用 [`layout-output.schema.json`](layout-output.schema.json) 和 `-o final-message.txt`，只接受像素坐标的固定编辑器对象格式。每个任务生成：

- `<job-id>.events.jsonl`：逐行记录 worker 事件和 Codex 原始 JSONL；
- `<job-id>.result.json`：状态、重试次数、排版 `layout` 或生图首个 `saved_path`、复制后的 artifact 路径、MIME 和 SHA-256；
- `generation-output/`：通过最终 JSON `{"saved_path":"..."}` 找到并验证后复制的图片。

可用 `--generation-output <目录>` 指定复制目录；未指定时使用 `output-dir/generation-output`。

默认失败不重试，可用 `--max-retries N` 显式开启。`--timeout` 可设置单次调用超时；API 调用也可以传入 `cancel_event`。`--dry-run` 可在没有启动 Codex 时检查命令组装：

```powershell
python services/codex_worker.py image --input panel-001.json --output-dir .data/test --dry-run
```

生图不会从 CLI JSONL 事件或自然语言中猜路径，只读取最终文件中的结构化 `saved_path`。路径必须位于任务输出目录、任务 `.codex`、`CODEX_HOME` 或显式 `--image-source-dir` 下，且必须能验证为图片；否则任务失败。编辑器可以监听事件文件，边生成边显示，并在人工修改后重新提交同一个面板任务。
