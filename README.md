# Codex Comic Studio

基于 [PhotoCraft](https://github.com/storytold/photocraft) 与 Codex CLI 的漫画生成工作台。

## 目标

- 从分镜表、排版参考和生成标准文档生成页面构图参数与提示词。
- 将构图参数转换为可编辑的页面格子，并绑定气泡、文字和资产图。
- 通过 Codex CLI 排队生成图片，支持边生成边人工 QA 和局部修改。
- 按话管理独立项目，完成后导出整话图片。

## 当前状态

当前仓库包含一个无依赖的漫画编辑器 MVP：主格、副格、底图、渐变和气泡是独立对象，可在 SVG 画布上选中、拖动、缩放和修改属性；页面按话和页管理，并提供项目 JSON/PNG 导出、资产绑定、版本安全回写和 Codex 任务队列入口。

启动编辑器和本地 Codex 桥接服务：

```powershell
python services/server.py
```

访问 <http://127.0.0.1:8080/>。编辑器负责显示结果与人工调整，`services/codex_worker.py` 负责把排版任务和 `$imagegen` 生图任务交给 Codex CLI，并保存 JSONL 事件、结果文件和生成资产。

本地服务还提供 `GET/PUT /api/project`、`GET/POST /api/assets`、`GET /api/jobs`、`POST /api/jobs`、`GET /api/jobs/{id}` 和 `POST /api/jobs/{id}/cancel`。`pipeline` 任务先让 Codex 返回结构化排版，再按面板提示词逐格生图；页面 revision 发生变化时，结果会保留为待人工应用，避免覆盖编辑。

## 记录

- [漫画生成项目可行性评估](outputs/issue-reports/2026-10-07-1149-漫画生成项目可行性评估.md)
- [GitHub 项目创建能力检查](outputs/issue-reports/2026-10-07-1258-GitHub项目创建能力检查.md)
