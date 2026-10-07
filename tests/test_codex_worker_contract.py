"""Contract tests for the Codex CLI bridge.

These tests never call a real Codex account.  A tiny Python executable stands
in for ``codex`` and emits the same JSONL-shaped records that the worker reads.
The checks cover the boundary that matters to the editor: references and
imagegen are present in the command, layout JSON is accepted only when valid,
an image is successful only when its saved path is a real file, and copied
artifacts are byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import base64
import shutil
import sys
import unittest
from io import StringIO
from contextlib import redirect_stdout
from unittest.mock import patch
from contextlib import contextmanager
from uuid import uuid4
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
TEST_TMP = ROOT / "work" / "test-tmp"
TEST_TMP.mkdir(parents=True, exist_ok=True)


@contextmanager
def workspace_tmp():
    """Use a workspace directory without tempfile's restrictive Windows ACL."""
    directory = TEST_TMP / f"case-{uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)
if str(SERVICES) not in sys.path:
    sys.path.insert(0, str(SERVICES))

from codex_worker import CodexWorker  # noqa: E402


class CodexWorkerContractTests(unittest.TestCase):
    def _run_records(self, worker: CodexWorker, kind: str, payload: dict, prompt: str,
                     records: list[object], job_id: str, final_value: object,
                     exit_code: int = 0, final_is_raw: bool = False) -> dict:
        """Run the worker against an in-process JSONL fake, never real Codex."""
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = StringIO("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records))
                self.stderr = StringIO("")

            def poll(self) -> int:
                return exit_code

            def wait(self) -> int:
                return exit_code

        def fake_popen(command, cwd=None, **kwargs):
            final_path = Path(cwd) / "final-message.txt"
            final_path.write_text(
                final_value if final_is_raw else json.dumps(final_value, ensure_ascii=False),
                encoding="utf-8",
            )
            return FakeProcess()

        with patch("codex_worker.subprocess.Popen", side_effect=fake_popen), redirect_stdout(StringIO()):
            return worker.run(kind, payload, prompt, [], job_id)

    def test_image_command_includes_imagegen_and_all_reference_assets(self) -> None:
        with workspace_tmp() as root:
            asset_a = root / "character.png"
            asset_b = root / "style.png"
            asset_a.write_bytes(b"character")
            asset_b.write_bytes(b"style")

            with redirect_stdout(StringIO()):
                result = CodexWorker(sys.executable, root / "out", max_retries=0, dry_run=True).run(
                    "image",
                    {"id": "panel-1", "prompt": "rainy roof"},
                    "rainy roof",
                    [str(asset_a), str(asset_b)],
                    "job-1",
                )

            command = result["command"]
            self.assertEqual(command[:3], [sys.executable, "exec", "--json"])
            self.assertIn("--skip-git-repo-check", command)
            self.assertEqual(command.count("-i"), 2)
            self.assertEqual(command[command.index("-i") + 1], str(asset_a))
            second_i = command.index("-i", command.index("-i") + 1)
            self.assertEqual(command[second_i + 1], str(asset_b))
            # The actual prompt is intentionally redacted in the dry-run
            # summary, so verify the worker's prompt constructor separately.
            from codex_worker import prompt_for

            self.assertTrue(prompt_for("image", {"prompt": "rainy roof"}).startswith("$imagegen"))

    def test_valid_layout_final_json_is_returned_as_structured_layout(self) -> None:
        layout = {
            "width": 1000,
            "height": 1600,
            "objects": [{"id": "main", "type": "main", "name": "主格", "x": 0, "y": 0,
                          "w": 1000, "h": 1600, "rotation": 0, "opacity": 1,
                          "fill": "#ffffff", "visible": True, "locked": False}],
            "prompts": [{"panel_id": "main", "prompt": "rainy roof", "asset_ids": []}],
        }
        with workspace_tmp() as root:
            result = self._run_records(
                CodexWorker(sys.executable, root / "out", max_retries=0),
                "layout", {"id": "layout-1"}, "layout prompt",
                [{"type": "layout-final", "text": json.dumps(layout, ensure_ascii=False)}], "layout-1", layout
            )
            # ``layout`` is the stable API field; ``layout_final`` is accepted
            # for compatibility with an earlier worker build.
            parsed = result.get("layout", result.get("layout_final"))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(parsed, layout)

    def test_malformed_layout_final_json_fails_closed(self) -> None:
        with workspace_tmp() as root:
            result = self._run_records(
                CodexWorker(sys.executable, root / "out", max_retries=0),
                "layout", {"id": "layout-invalid"}, "layout prompt",
                [{"type": "layout-final", "text": '{"pages": [broken'}], "layout-invalid",
                '{"pages": [broken', final_is_raw=True
            )
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result.get("error"))

    def test_image_with_nonexistent_saved_path_cannot_succeed(self) -> None:
        with workspace_tmp() as root:
            missing = root / "does-not-exist.png"
            result = self._run_records(
                CodexWorker(sys.executable, root / "out", max_retries=0),
                "image", {"id": "panel-1"}, "$imagegen\nmake art",
                [{"type": "image_generation_end", "saved_path": str(missing)}], "image-missing",
                {"saved_path": str(missing)}
            )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result.get("artifacts"), [])
            self.assertIn("saved_path", result.get("error", ""))

    def test_image_artifact_copy_records_sha256(self) -> None:
        with workspace_tmp() as root:
            # Codex output must stay in the worker's allowed generation root;
            # an arbitrary path is rejected to prevent path traversal.
            source = root / "out" / "generation-output" / "generated.png"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            ))
            result = self._run_records(
                CodexWorker(sys.executable, root / "out", max_retries=0),
                "image", {"id": "panel-1"}, "$imagegen\nmake art",
                [{"type": "image_generation_end", "saved_path": str(source)}], "image-copy",
                {"saved_path": str(source)}
            )
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(len(result["artifacts"]), 1)
            artifact = result["artifacts"][0]
            target = Path(artifact["path"])
            self.assertTrue(target.is_file())
            expected = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertEqual(artifact["sha256"], expected)
            self.assertEqual(target.read_bytes(), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
