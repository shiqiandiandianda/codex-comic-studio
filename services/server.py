#!/usr/bin/env python3
"""Local UI/API bridge for Codex Comic Studio.

The browser owns the editable comic scene. This process owns project/asset
persistence and runs Codex CLI jobs in background threads. It binds to loopback
so the local editor does not expose the user's files to the LAN.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from codex_worker import CodexWorker, prompt_for, resolve_codex_bin


ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
DATA_DIR = ROOT / ".data"
JOB_DIR = DATA_DIR / "jobs"
ASSET_DIR = DATA_DIR / "assets"
PROJECT_FILE = DATA_DIR / "project.json"
HOST = os.environ.get("COMIC_HOST", "127.0.0.1")
PORT = int(os.environ.get("COMIC_PORT", "8080"))
MAX_BODY = 40 * 1024 * 1024
JOBS: dict[str, dict] = {}
CANCEL_EVENTS: dict[str, threading.Event] = {}
LOCK = threading.RLock()


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def read_json_file(path: Path, fallback: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return fallback


def project_value() -> dict:
    value = read_json_file(PROJECT_FILE, {"episodes": []})
    return value if isinstance(value, dict) and isinstance(value.get("episodes", []), list) else {"episodes": []}


def write_json_file(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_jobs() -> None:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    for result_path in JOB_DIR.glob("*/**/*.api.json"):
        record = read_json_file(result_path, None)
        if isinstance(record, dict) and record.get("id"):
            JOBS.setdefault(str(record["id"]), record)


def save_job(job: dict) -> None:
    job_id = str(job["id"])
    write_json_file(JOB_DIR / job_id / f"{job_id}.api.json", job)


def update_job(job_id: str, **updates: object) -> dict:
    with LOCK:
        job = JOBS.setdefault(job_id, {"id": job_id, "job_id": job_id})
        job.update(updates)
        snapshot = dict(job)
    save_job(snapshot)
    return snapshot


def data_url_bytes(data_url: str) -> tuple[str, bytes]:
    if not isinstance(data_url, str) or not data_url.startswith("data:") or "," not in data_url:
        raise ValueError("data_url 必须是 data: 图片")
    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("只支持 base64 data_url")
    mime = header[5:].split(";", 1)[0].lower() or "application/octet-stream"
    if not mime.startswith("image/"):
        raise ValueError("资产必须是图片")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ValueError("data_url 不是有效的 base64") from exc
    if not raw or len(raw) > MAX_BODY:
        raise ValueError("资产大小超出限制")
    return mime, raw


def asset_records() -> list[dict]:
    records = []
    for metadata in sorted(ASSET_DIR.glob("*.json")):
        value = read_json_file(metadata, None)
        if isinstance(value, dict) and value.get("id"):
            records.append(value)
    return records


def collect_asset_paths(value: object) -> list[str]:
    found: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key in ("path", "asset_path", "file", "file_path"):
                candidate = node.get(key)
                if isinstance(candidate, str) and candidate and not candidate.startswith(("data:", "/api/")):
                    found.append(candidate)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, str) and ("\\" in node or ":/" in node or node.startswith(".")):
            found.append(node)

    visit(value)
    for record in asset_records():
        if record.get("path"):
            found.append(str(record["path"]))
    unique: list[str] = []
    for item in found:
        if item not in unique and Path(item).is_file():
            unique.append(item)
    return unique


def artifact_url(job_id: str, artifact_path: str) -> str:
    path = Path(artifact_path).resolve()
    job_root = (JOB_DIR / job_id).resolve()
    try:
        relative = path.relative_to(job_root).as_posix()
    except ValueError:
        return ""
    return f"/api/artifacts/{job_id}/{relative}"


def artifact_view(job_id: str, artifact: dict) -> dict:
    output = dict(artifact)
    if isinstance(output.get("path"), str):
        output["url"] = artifact_url(job_id, output["path"])
    return output


def run_job(job_id: str, kind: str, payload: dict, prompt: str, assets: list[str], expected_revision: object) -> None:
    job_root = JOB_DIR / job_id
    job_root.mkdir(parents=True, exist_ok=True)
    cancel_event = CANCEL_EVENTS[job_id]
    update_job(job_id, status="running", phase="调用 Codex", attempts=0)
    codex_bin = resolve_codex_bin(os.environ.get("CODEX_BIN", "codex"))
    timeout = float(os.environ.get("COMIC_CODEX_TIMEOUT", "0")) or None
    max_retries = max(0, int(os.environ.get("COMIC_MAX_RETRIES", "0")))
    try:
        if kind == "pipeline":
            episode = payload.get("episode", {})
            page = payload.get("page", {})
            layout_payload = {
                "episode": episode,
                "page": page,
                "generation_standard": payload.get("generation_standard") or episode.get("generation_standard", ""),
                "generation_standard_path": payload.get("generation_standard_path", ""),
                "layout_reference_paths": payload.get("layout_reference_paths", []) or episode.get("layout_reference_paths", []),
                "asset_paths": assets,
                "storyboard": page.get("storyboard", {}),
            }
            update_job(job_id, phase="Codex 排版")
            layout_worker = CodexWorker(codex_bin, job_root / "layout", max_retries=max_retries)
            layout_summary = layout_worker.run("layout", layout_payload, prompt or prompt_for("layout", layout_payload), assets, f"{job_id}-layout", cancel_event=cancel_event, timeout_seconds=timeout)
            if layout_summary.get("status") != "succeeded":
                raise RuntimeError(layout_summary.get("error") or "排版任务失败")
            layout = layout_summary.get("layout", {})
            images: list[dict] = []
            prompts = layout.get("prompts", []) if isinstance(layout, dict) else []
            for index, item in enumerate(prompts, 1):
                if cancel_event.is_set():
                    raise RuntimeError("job cancelled")
                panel_id = str(item.get("panel_id", ""))
                panel_assets = [str(x) for x in item.get("asset_paths", []) if isinstance(x, str)] or assets
                image_payload = {"panel_id": panel_id, "prompt": item.get("prompt", ""), "asset_paths": panel_assets, "page_id": page.get("id")}
                update_job(job_id, phase=f"Codex 生图 {index}/{len(prompts)}")
                image_job_id = f"{job_id}-image-{index}"
                image_worker = CodexWorker(codex_bin, job_root / f"image-{index}", max_retries=max_retries)
                image_summary = image_worker.run("image", image_payload, str(item.get("prompt", "")), panel_assets, image_job_id, cancel_event=cancel_event, timeout_seconds=timeout)
                if image_summary.get("status") != "succeeded":
                    raise RuntimeError(image_summary.get("error") or f"面板 {panel_id} 生图失败")
                for artifact in image_summary.get("artifacts", []):
                    images.append({"panel_id": panel_id, "artifact": artifact_view(job_id, artifact)})
            result = {"layout": layout, "images": images}
        else:
            panel_id = str(payload.get("panel_id") or payload.get("panelId") or "")
            update_job(job_id, phase="Codex 单格生图")
            worker = CodexWorker(codex_bin, job_root / "image", max_retries=max_retries)
            summary = worker.run("image", payload, prompt or str(payload.get("prompt", "")), assets, f"{job_id}-image", cancel_event=cancel_event, timeout_seconds=timeout)
            if summary.get("status") != "succeeded":
                raise RuntimeError(summary.get("error") or "生图任务失败")
            result = {"layout": None, "images": [{"panel_id": panel_id, "artifact": artifact_view(job_id, item)} for item in summary.get("artifacts", [])]}
        if cancel_event.is_set():
            update_job(job_id, status="cancelled", phase="已取消", error="job cancelled", result=result, page_id=payload.get("page", {}).get("id"), expected_revision=expected_revision)
        else:
            update_job(job_id, status="succeeded", phase="完成", result=result, page_id=payload.get("page", {}).get("id") or payload.get("page_id"), expected_revision=expected_revision)
    except Exception as exc:
        status = "cancelled" if cancel_event.is_set() else "failed"
        update_job(job_id, status=status, phase="已取消" if status == "cancelled" else "失败", error=str(exc), page_id=payload.get("page", {}).get("id") or payload.get("page_id"), expected_revision=expected_revision)
    finally:
        CANCEL_EVENTS.pop(job_id, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "ComicCraftLocal/0.2"

    def log_message(self, format: str, *args: object) -> None:
        print("[comiccraft] " + format % args)

    def send_json(self, status: int, value: object) -> None:
        body = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0 or length > MAX_BODY:
            raise ValueError("请求体过大")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(200, {"ok": True, "codex": resolve_codex_bin(os.environ.get("CODEX_BIN", "codex"))})
            return
        if path == "/api/project":
            self.send_json(200, project_value())
            return
        if path == "/api/assets":
            self.send_json(200, {"assets": asset_records()})
            return
        if path.startswith("/api/assets/"):
            asset_id = unquote(path.rsplit("/", 1)[-1])
            metadata = next((x for x in asset_records() if x.get("id") == asset_id), None)
            if not metadata:
                self.send_json(404, {"error": "asset not found"})
                return
            target = Path(str(metadata.get("path", ""))).resolve()
            try:
                target.relative_to(ASSET_DIR.resolve())
                body = target.read_bytes()
            except (OSError, ValueError):
                self.send_json(404, {"error": "asset file not found"})
                return
            self.send_bytes(200, body, metadata.get("mime", "application/octet-stream"))
            return
        if path == "/api/jobs":
            with LOCK:
                jobs = [dict(item) for item in JOBS.values()]
            self.send_json(200, {"jobs": jobs})
            return
        if path.startswith("/api/jobs/"):
            job_id = unquote(path.rsplit("/", 1)[-1])
            with LOCK:
                job = dict(JOBS.get(job_id, {}))
            if not job:
                job = read_json_file(JOB_DIR / job_id / f"{job_id}.api.json", None)
            if not isinstance(job, dict) or not job:
                self.send_json(404, {"error": "job not found"})
            else:
                self.send_json(200, job)
            return
        if path.startswith("/api/artifacts/"):
            parts = path.split("/")
            if len(parts) < 5:
                self.send_json(404, {"error": "artifact not found"})
                return
            job_id = unquote(parts[3])
            relative = Path(*[unquote(part) for part in parts[4:]])
            if relative.is_absolute() or ".." in relative.parts:
                self.send_json(404, {"error": "artifact not found"})
                return
            target = (JOB_DIR / job_id / relative).resolve()
            try:
                target.relative_to((JOB_DIR / job_id).resolve())
                body = target.read_bytes()
            except (OSError, ValueError):
                self.send_json(404, {"error": "artifact not found"})
                return
            self.send_bytes(200, body, mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            return
        if path in {"/", "/index.html"}:
            self.serve_file(APP_DIR / "index.html")
            return
        if path in {"/app.js", "/styles.css"}:
            self.serve_file(APP_DIR / path.lstrip("/"))
            return
        if path.startswith("/app/"):
            self.serve_file(APP_DIR / path.removeprefix("/app/"))
            return
        self.send_json(404, {"error": "not found"})

    def send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, file_path: Path) -> None:
        try:
            target = file_path.resolve()
            target.relative_to(APP_DIR.resolve())
            body = target.read_bytes()
        except (FileNotFoundError, ValueError, OSError):
            self.send_json(404, {"error": "file not found"})
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix.lower() in {".html", ".css", ".js"}:
            content_type += "; charset=utf-8"
        self.send_bytes(200, body, content_type)

    def do_PUT(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/project":
            self.send_json(404, {"error": "not found"})
            return
        try:
            value = self.body_json()
            if not isinstance(value.get("episodes", []), list):
                raise ValueError("episodes 必须是数组")
            write_json_file(PROJECT_FILE, value)
            self.send_json(200, value)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/assets":
                data = self.body_json()
                mime, raw = data_url_bytes(str(data.get("data_url", "")))
                asset_id = str(data.get("id") or f"asset-{uuid.uuid4().hex[:12]}")
                extension = mimetypes.guess_extension(mime) or ".bin"
                filename = f"{asset_id}{extension}"
                file_path = ASSET_DIR / filename
                ASSET_DIR.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(raw)
                record = {"id": asset_id, "name": str(data.get("name") or filename), "mime": mime, "size": len(raw), "path": str(file_path.resolve()), "url": f"/api/assets/{asset_id}"}
                write_json_file(ASSET_DIR / f"{asset_id}.json", record)
                self.send_json(201, record)
                return
            if path == "/api/jobs":
                data = self.body_json()
                kind = str(data.get("kind", ""))
                if kind not in {"pipeline", "image"}:
                    raise ValueError("kind must be pipeline or image")
                payload = data.get("payload") or {}
                if not isinstance(payload, dict):
                    raise ValueError("payload must be an object")
                job_id = str(data.get("job_id") or f"{kind}-{uuid.uuid4().hex[:12]}")
                expected_revision = data.get("expected_revision")
                assets = collect_asset_paths({"payload": payload, "assets": data.get("assets", [])})
                record = {"id": job_id, "job_id": job_id, "type": kind, "status": "queued", "phase": "排队中", "attempts": 0, "page_id": payload.get("page", {}).get("id") or payload.get("page_id"), "expected_revision": expected_revision}
                with LOCK:
                    JOBS[job_id] = record
                    CANCEL_EVENTS[job_id] = threading.Event()
                save_job(record)
                thread = threading.Thread(target=run_job, args=(job_id, kind, payload, str(data.get("prompt", "")), assets, expected_revision), daemon=True)
                thread.start()
                self.send_json(202, record)
                return
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = unquote(path.split("/")[3])
                event = CANCEL_EVENTS.get(job_id)
                if event is None:
                    self.send_json(404, {"error": "job not running"})
                else:
                    event.set()
                    update_job(job_id, phase="取消中")
                    self.send_json(202, {"id": job_id, "status": "cancelling"})
                return
            self.send_json(404, {"error": "not found"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    load_jobs()
    print(f"ComicCraft Studio: http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
