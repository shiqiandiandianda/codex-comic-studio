#!/usr/bin/env python3
"""Dependency-free Codex CLI bridge for layout and panel image jobs.

The bridge has two deliberately strict contracts:

* layout jobs write a JSON final message validated against
  ``layout-output.schema.json``;
* image jobs write a JSON final message containing ``saved_path``. The path
  must resolve inside an allowed generation directory and contain a readable
  image before the job is marked successful.

Codex JSONL events are retained for progress display, but an image path is
never inferred from arbitrary stdout or natural-language text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class Asset:
    id: str
    kind: str
    path: str
    label: str | None = None
    notes: str | None = None


@dataclass
class Rect:
    x: float
    y: float
    width: float
    height: float


@dataclass
class Panel:
    id: str
    role: str
    rect: Rect
    prompt: str = ""
    asset_ids: list[str] = field(default_factory=list)
    bubble_ids: list[str] = field(default_factory=list)
    gradient_id: str | None = None
    image_path: str | None = None


@dataclass
class Page:
    id: str
    width: int
    height: int
    panels: list[Panel] = field(default_factory=list)
    bubbles: list[dict[str, Any]] = field(default_factory=list)
    background: dict[str, Any] | None = None
    gradient: dict[str, Any] | None = None


@dataclass
class Episode:
    id: str
    title: str
    pages: list[Page] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    layout_reference_paths: list[str] = field(default_factory=list)
    generation_standard: str = ""


@dataclass
class Layout:
    episode_id: str
    pages: list[Page] = field(default_factory=list)
    rules: dict[str, Any] = field(default_factory=dict)
    layout_reference_paths: list[str] = field(default_factory=list)
    generation_standard: str = ""


@dataclass
class Job:
    id: str
    type: str
    input: dict[str, Any]
    prompt: str = ""
    asset_paths: list[str] = field(default_factory=list)
    max_retries: int = 0


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_json(path: str) -> dict[str, Any]:
    if path == "-":
        value = json.load(sys.stdin)
    else:
        with open(path, "r", encoding="utf-8-sig") as handle:
            value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("输入 JSON 必须是对象")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_codex_bin(value: str | None) -> str:
    """Resolve codex/codex.cmd on Windows without requiring a shell."""
    if value and Path(value).is_file():
        return str(Path(value).resolve())
    if value and Path(value).parent != Path("."):
        return value
    for name in ([value] if value else []) + ["codex", "codex.cmd"]:
        if not name:
            continue
        found = shutil.which(name)
        if found:
            return found
    return value or "codex"


def _standard_text(payload: dict[str, Any]) -> str:
    standard = payload.get("generation_standard", "")
    standard_path = payload.get("generation_standard_path")
    if isinstance(standard_path, str) and Path(standard_path).is_file():
        try:
            return Path(standard_path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return str(standard)
    return str(standard)


def prompt_for(kind: str, payload: dict[str, Any], override: str = "") -> str:
    """Build a prompt while retaining structured input alongside overrides."""
    body = _json(payload)
    if kind == "layout":
        instruction = (
            "You are the comic layout planner. Return only the fixed JSON object requested by the output schema. "
            "Use pixel coordinates (not normalized values). Keep main/sub panels, base image, bubbles and gradients "
            "as separate editable objects. Put an image prompt in prompts for every main or sub panel. "
            "The program, not the image model, renders bubbles and gradients."
        )
        if override:
            instruction += "\nEDITOR_PROMPT=" + override
        return f"{instruction}\nGENERATION_STANDARD={_standard_text(payload)}\nSTORYBOARD_JSON={body}"

    instruction = (
        "$imagegen\nGenerate only the visual content of one comic panel. Do not draw speech bubbles, text, gradients, "
        "or editor UI; those are rendered as separate program objects. Use the supplied reference images for "
        "character, prop, background, or style consistency. At the end, return exactly JSON with one field, "
        "saved_path, pointing to the actual generated image file. Do not claim a path that does not exist."
    )
    if override:
        instruction += "\nEDITOR_PROMPT=" + override
    return f"{instruction}\nPANEL_JSON={body}"


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_layout(value: Any) -> dict[str, Any]:
    """Validate the editor's fixed pixel layout contract and return it."""
    if isinstance(value, dict) and isinstance(value.get("layout"), dict):
        value = value["layout"]
    if not isinstance(value, dict):
        raise ValueError("排版最终结果必须是 JSON 对象")
    for key in ("width", "height", "objects", "prompts"):
        if key not in value:
            raise ValueError(f"排版结果缺少字段: {key}")
    if not isinstance(value["width"], int) or isinstance(value["width"], bool) or value["width"] <= 0:
        raise ValueError("排版 width 必须是正整数像素")
    if not isinstance(value["height"], int) or isinstance(value["height"], bool) or value["height"] <= 0:
        raise ValueError("排版 height 必须是正整数像素")
    if not isinstance(value["objects"], list) or not isinstance(value["prompts"], list):
        raise ValueError("排版 objects/prompts 必须是数组")
    allowed_types = {"main", "sub", "base", "bubble", "gradient"}
    required_object = {"id", "type", "name", "x", "y", "w", "h", "rotation", "opacity", "fill", "visible", "locked"}
    seen: set[str] = set()
    for obj in value["objects"]:
        if not isinstance(obj, dict) or not required_object.issubset(obj):
            raise ValueError("每个排版对象必须包含固定字段")
        if not isinstance(obj["id"], str) or not obj["id"] or obj["id"] in seen:
            raise ValueError("排版对象 id 必须非空且唯一")
        seen.add(obj["id"])
        if obj["type"] not in allowed_types:
            raise ValueError(f"不支持的排版对象类型: {obj['type']}")
        for key in ("x", "y", "w", "h", "rotation", "opacity"):
            if not _finite_number(obj[key]):
                raise ValueError(f"排版对象 {key} 必须是有限数字")
        if obj["w"] < 0 or obj["h"] < 0 or not 0 <= obj["opacity"] <= 1:
            raise ValueError("排版对象尺寸或 opacity 越界")
        if not isinstance(obj["name"], str) or not isinstance(obj["fill"], str):
            raise ValueError("排版对象 name/fill 必须是字符串")
        if not isinstance(obj["visible"], bool) or not isinstance(obj["locked"], bool):
            raise ValueError("排版对象 visible/locked 必须是布尔值")
        if "parentId" in obj and obj["parentId"] is not None and not isinstance(obj["parentId"], str):
            raise ValueError("parentId 必须是字符串或 null")
        if "text" in obj and not isinstance(obj["text"], str):
            raise ValueError("气泡 text 必须是字符串")
        for key in ("startColor", "endColor"):
            if key in obj and not isinstance(obj[key], str):
                raise ValueError(f"{key} 必须是字符串")
        if "angle" in obj and not _finite_number(obj["angle"]):
            raise ValueError("渐变 angle 必须是有限数字")
    for prompt in value["prompts"]:
        if not isinstance(prompt, dict) or not isinstance(prompt.get("panel_id"), str) or not isinstance(prompt.get("prompt"), str):
            raise ValueError("每个 prompts 项必须包含 panel_id/prompt")
        if not isinstance(prompt.get("asset_ids", []), list) or not all(isinstance(item, str) for item in prompt.get("asset_ids", [])):
            raise ValueError("prompts.asset_ids 必须是字符串数组")
    return value


def _valid_png(data: bytes) -> bool:
    return len(data) >= 45 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and struct.unpack(">II", data[16:24]) > (0, 0) and data[-8:-4] == b"IEND" and data[-4:] == b"\xaeB`\x82"


def _valid_gif(data: bytes) -> bool:
    return len(data) >= 11 and data[:6] in {b"GIF87a", b"GIF89a"} and struct.unpack("<HH", data[6:10]) > (0, 0) and data[-1:] == b";"


def _valid_bmp(data: bytes) -> bool:
    if len(data) < 54 or data[:2] != b"BM" or struct.unpack("<ii", data[18:26]) == (0, 0):
        return False
    declared_size = struct.unpack("<I", data[2:6])[0]
    return declared_size == 0 or declared_size <= len(data)


def _valid_jpeg(data: bytes) -> bool:
    if len(data) < 4 or data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
        return False
    index = 2
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            return False
        length = struct.unpack(">H", data[index:index + 2])[0]
        if length < 2 or index + length > len(data):
            return False
        index += length
    return True


def image_mime(path: Path) -> str | None:
    """Recognize common bitmap headers; optionally use Pillow for full verify."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            image.verify()
            return str(Image.MIME.get(image.format, "application/octet-stream"))
    except ImportError:
        pass
    except Exception:
        return None
    if _valid_png(data):
        return "image/png"
    if _valid_gif(data):
        return "image/gif"
    if _valid_bmp(data):
        return "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP" and struct.unpack("<I", data[4:8])[0] + 8 <= len(data):
        return "image/webp"
    if _valid_jpeg(data):
        return "image/jpeg"
    return None


def _parse_final_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取 Codex 最终文件: {exc}") from exc
    if not text:
        raise ValueError("Codex 最终文件为空")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Codex 最终文件不是有效 JSON") from exc


class CodexWorker:
    def __init__(
        self,
        codex_bin: str | None,
        output_dir: Path,
        max_retries: int = 0,
        dry_run: bool = False,
        image_source_dirs: Iterable[str | Path] = (),
        generation_output_dir: str | Path | None = None,
    ):
        self.codex_bin = resolve_codex_bin(codex_bin)
        self.output_dir = Path(output_dir)
        self.max_retries = max(0, max_retries)
        self.dry_run = dry_run
        self.image_source_dirs = [Path(item) for item in image_source_dirs]
        self.generation_output_dir = Path(generation_output_dir) if generation_output_dir else None

    def _event(self, stream, record: dict[str, Any], **extra: Any) -> None:
        enriched = {"ts": time.time(), **record, **extra}
        stream.write(_json(enriched) + "\n")
        stream.flush()
        print(_json(enriched), flush=True)

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        try:
            process.kill()
        except OSError:
            pass

    def _process(self, command: list[str], cwd: Path, events, attempt: int, cancel_event: threading.Event | None, timeout_seconds: float | None) -> tuple[int, str | None, bool]:
        process = subprocess.Popen(command, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1)
        assert process.stdout is not None and process.stderr is not None
        lines: queue.Queue[tuple[str, str | None]] = queue.Queue()

        def pump(channel: str, stream) -> None:
            try:
                for line in iter(stream.readline, ""):
                    lines.put((channel, line.rstrip("\r\n")))
            finally:
                lines.put((channel, None))

        threads = [threading.Thread(target=pump, args=("stdout", process.stdout), daemon=True), threading.Thread(target=pump, args=("stderr", process.stderr), daemon=True)]
        for thread in threads:
            thread.start()
        ended = 0
        deadline = time.monotonic() + timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
        reason: str | None = None
        while ended < 2:
            if cancel_event is not None and cancel_event.is_set() and process.poll() is None:
                reason = "cancelled"
                self._terminate(process)
            elif deadline is not None and time.monotonic() >= deadline and process.poll() is None:
                reason = "timeout"
                self._terminate(process)
            try:
                channel, line = lines.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                ended += 1
                continue
            if channel == "stdout":
                try:
                    record = {"event": "codex", "channel": channel, "attempt": attempt, "data": json.loads(line)}
                except json.JSONDecodeError:
                    record = {"event": "codex_raw", "channel": channel, "attempt": attempt, "data": line}
            else:
                record = {"event": "codex_raw", "channel": channel, "attempt": attempt, "data": line}
            self._event(events, record)
        returncode = process.wait()
        return returncode, reason, reason is not None

    def _allowed_image_roots(self, generation_dir: Path) -> list[Path]:
        # The parent is the episode/job workspace. This permits an explicitly
        # reported image generated beside the per-job output while remaining
        # bounded to that workspace; callers can add another root explicitly.
        roots = [self.output_dir.resolve(), self.output_dir.parent.resolve(), generation_dir.resolve(), (self.output_dir / ".codex").resolve()]
        codex_home = os.environ.get("CODEX_HOME")
        roots.append(Path(codex_home).resolve() if codex_home else (Path.home() / ".codex").resolve())
        roots.extend(Path(item).resolve() for item in self.image_source_dirs)
        return list(dict.fromkeys(roots))

    def _safe_image_source(self, saved_path: str, generation_dir: Path) -> tuple[Path, str]:
        raw = Path(saved_path)
        source = (self.output_dir / raw).resolve() if not raw.is_absolute() else raw.resolve()
        if not any(source == root or root in source.parents for root in self._allowed_image_roots(generation_dir)):
            raise ValueError("saved_path 不在允许的生成目录内")
        if not source.is_file():
            raise ValueError("saved_path 文件不存在")
        mime = image_mime(source)
        if not mime:
            raise ValueError("saved_path 不是可验证的图片文件")
        return source, mime

    def _copy_image(self, source: Path, mime: str, generation_dir: Path, job_id: str) -> dict[str, Any]:
        generation_dir.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower() or {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(mime, ".bin")
        target = generation_dir / f"{job_id}{suffix}"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return {"saved_path": str(source), "path": str(target), "mime": mime, "sha256": sha256(target)}

    def run(self, kind: str, payload: dict[str, Any], prompt: str, assets: Iterable[str], job_id: str, *, cancel_event: threading.Event | None = None, timeout_seconds: float | None = None) -> dict[str, Any]:
        if kind not in {"layout", "image"}:
            raise ValueError("kind must be layout or image")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        event_path = self.output_dir / f"{job_id}.events.jsonl"
        result_path = self.output_dir / f"{job_id}.result.json"
        final_path = self.output_dir / "final-message.txt"
        generation_dir = self.generation_output_dir or (self.output_dir / "generation-output")
        if not generation_dir.is_absolute():
            generation_dir = (self.output_dir / generation_dir).resolve()
        # API callers may pass a short editor override. Rebuild the complete
        # prompt in that case so storyboard/panel JSON and the standard
        # document are never silently dropped. CLI callers that already used
        # prompt_for() carry the marker and are left unchanged.
        if kind == "layout" and "STORYBOARD_JSON=" not in prompt:
            prompt = prompt_for(kind, payload, prompt)
        elif kind == "image" and "PANEL_JSON=" not in prompt:
            prompt = prompt_for(kind, payload, prompt)
        if kind == "image":
            prompt = prompt if prompt.lstrip().startswith("$imagegen") else "$imagegen\n" + prompt
        references = [str(Path(item)) for item in assets]
        command = [self.codex_bin, "exec", "--json", "--skip-git-repo-check", "--sandbox", "workspace-write", "-o", "final-message.txt"]
        if kind == "layout":
            command.extend(["--output-schema", str(Path(__file__).with_name("layout-output.schema.json").resolve())])
        for reference in references:
            command.extend(["-i", reference])
        command.append(prompt)
        summary: dict[str, Any] = {"job_id": job_id, "type": kind, "status": "queued", "attempts": 0, "saved_path": None, "saved_paths": [], "artifacts": [], "events_path": str(event_path), "final_message_path": str(final_path), "generation_output_dir": str(generation_dir), "command": command[:-1] + ["<prompt>"]}
        if self.dry_run:
            summary["status"] = "dry_run"
            result_path.write_text(_json(summary) + "\n", encoding="utf-8")
            print(_json(summary), flush=True)
            return summary

        with event_path.open("w", encoding="utf-8") as events:
            self._event(events, {"event": "job_queued", "job_id": job_id, "type": kind})
            for attempt in range(1, self.max_retries + 2):
                summary["attempts"] = attempt
                final_path.unlink(missing_ok=True)
                self._event(events, {"event": "job_started", "job_id": job_id, "type": kind, "attempt": attempt})
                try:
                    returncode, stop_reason, stopped = self._process(command, self.output_dir, events, attempt, cancel_event, timeout_seconds)
                    self._event(events, {"event": "process_exit", "attempt": attempt, "returncode": returncode})
                    reason: str | None = None
                    if stopped:
                        reason = f"codex {stop_reason}"
                    elif returncode != 0:
                        reason = f"codex exit code {returncode}"
                    else:
                        try:
                            final = _parse_final_json(final_path)
                            if kind == "layout":
                                summary["layout"] = validate_layout(final)
                            else:
                                if not isinstance(final, dict) or not isinstance(final.get("saved_path"), str):
                                    raise ValueError("生图最终 JSON 必须包含 saved_path 字符串")
                                source, mime = self._safe_image_source(final["saved_path"], generation_dir)
                                artifact = self._copy_image(source, mime, generation_dir, job_id)
                                summary["saved_path"] = str(source)
                                summary["saved_paths"] = [str(source)]
                                summary["artifacts"] = [artifact]
                            summary["status"] = "succeeded"
                            break
                        except (ValueError, OSError) as exc:
                            reason = f"layout-final JSON invalid: {exc}" if kind == "layout" else str(exc)
                    self._event(events, {"event": "attempt_failed", "attempt": attempt, "reason": reason})
                    summary["error"] = reason
                except OSError as exc:
                    summary["error"] = str(exc)
                    self._event(events, {"event": "attempt_failed", "attempt": attempt, "reason": str(exc)})
                if attempt <= self.max_retries and not (cancel_event and cancel_event.is_set()):
                    time.sleep(min(2 ** (attempt - 1), 8))
            if summary["status"] != "succeeded":
                summary["status"] = "failed"

        result_path.write_text(_json(summary) + "\n", encoding="utf-8")
        print(_json(summary), flush=True)
        return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Codex CLI layout or image jobs with JSONL events.")
    parser.add_argument("kind", choices=["layout", "image"], help="任务类型；排版和生图分开执行")
    parser.add_argument("--input", default="", help="输入 JSON 文件，或 - 读取 stdin；可只用 --prompt 运行")
    parser.add_argument("--output-dir", required=True, help="事件、结果和生成图片的输出目录")
    parser.add_argument("--prompt", default="", help="追加到结构化输入之后的编辑器提示词")
    parser.add_argument("--asset", action="append", default=[], help="参考资产图路径，可重复传入")
    parser.add_argument("--image-source-dir", action="append", default=[], help="允许读取 Codex 生图文件的目录，可重复传入")
    parser.add_argument("--generation-output", default="", help="生成图片的复制目录；默认是 output-dir/generation-output")
    parser.add_argument("--codex-bin", default=os.environ.get("CODEX_BIN", ""), help="codex/codex.cmd 路径")
    parser.add_argument("--max-retries", type=int, default=0, help="失败后的重试次数，默认 0，避免重复消耗生图额度")
    parser.add_argument("--timeout", type=float, default=0, help="单次 Codex 调用超时秒数，0 表示不设超时")
    parser.add_argument("--job-id", default="", help="任务 ID；默认从输入 id 读取，否则随机生成")
    parser.add_argument("--dry-run", action="store_true", help="只输出将执行的命令，不启动 Codex")
    args = parser.parse_args()

    payload = load_json(args.input) if args.input else {}
    job_id = args.job_id or str(payload.get("id") or payload.get("job_id") or f"{args.kind}-{uuid.uuid4().hex[:10]}")
    assets = list(args.asset)
    for key in ("layout_reference_paths", "asset_paths"):
        for item in payload.get(key, []):
            if isinstance(item, str):
                assets.append(item)
    for item in payload.get("assets", []):
        if isinstance(item, dict) and item.get("path"):
            assets.append(str(item["path"]))
    prompt = prompt_for(args.kind, payload, args.prompt)
    worker = CodexWorker(resolve_codex_bin(args.codex_bin), Path(args.output_dir), args.max_retries, args.dry_run, args.image_source_dir, args.generation_output or None)
    summary = worker.run(args.kind, payload, prompt, assets, job_id, timeout_seconds=args.timeout or None)
    return 0 if summary["status"] in {"succeeded", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
