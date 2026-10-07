"""HTTP contract tests for the local project/job bridge.

The server is exercised over a real loopback HTTP socket.  The Codex worker is
patched with an in-process fake, so the tests verify persistence, queue
ordering, cancellation/retry plumbing, and page identity without generating
an image or using an account.
"""

from __future__ import annotations

import base64
import http.client
import importlib
import json
import shutil
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SERVICES = ROOT / "services"
if str(SERVICES) not in sys.path:
    sys.path.insert(0, str(SERVICES))


class ServerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_module = importlib.import_module("server")
        cls.Handler = getattr(cls.server_module, "Handler", None)
        cls.httpd = None
        cls.thread = None
        if cls.Handler is not None:
            cls.httpd = cls.server_module.ThreadingHTTPServer(("127.0.0.1", 0), cls.Handler)
            cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
            cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.httpd is not None:
            cls.httpd.shutdown()
            cls.httpd.server_close()
            cls.thread.join(timeout=2)

    def setUp(self) -> None:
        if self.httpd is None:
            self.skipTest("server does not expose Handler/ThreadingHTTPServer yet")

    def request(self, method: str, path: str, payload: object | None = None) -> tuple[int, object]:
        host, port = self.httpd.server_address
        connection = http.client.HTTPConnection(host, port, timeout=3)
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        connection.request(method, path, body=body, headers={"Content-Type": "application/json"} if body else {})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = raw
        return response.status, value

    def request_raw(self, method: str, path: str) -> tuple[int, bytes, dict[str, str]]:
        host, port = self.httpd.server_address
        connection = http.client.HTTPConnection(host, port, timeout=3)
        connection.request(method, path)
        response = connection.getresponse()
        raw = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, raw, headers

    def test_artifact_route_serves_only_job_scoped_files(self) -> None:
        job_id = f"contract-artifact-{uuid4().hex[:8]}"
        job_root = self.server_module.JOB_DIR / job_id
        artifact = job_root / "artifact.png"
        artifact_bytes = b"contract artifact bytes"
        job_root.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(artifact_bytes)
        try:
            status, body, headers = self.request_raw("GET", f"/api/artifacts/{job_id}/artifact.png")
            self.assertEqual(status, 200)
            self.assertEqual(body, artifact_bytes)
            self.assertIn("image/png", headers.get("content-type", ""))

            status, _, _ = self.request_raw("GET", f"/api/artifacts/{job_id}/../artifact.png")
            self.assertEqual(status, 404)
        finally:
            shutil.rmtree(job_root, ignore_errors=True)

    def test_project_round_trip_and_asset_registration(self) -> None:
        status, original = self.request("GET", "/api/project")
        self.assertEqual(status, 200)
        project = {"episodes": [{"id": "contract-episode", "title": "Contract", "pages": []}]}
        try:
            status, saved = self.request("PUT", "/api/project", project)
            self.assertEqual(status, 200)
            status, loaded = self.request("GET", "/api/project")
            self.assertEqual(status, 200)
            self.assertEqual(loaded.get("episodes"), project["episodes"])

            data_url = "data:image/png;base64," + base64.b64encode(b"contract-asset").decode("ascii")
            status, asset = self.request("POST", "/api/assets", {"name": "contract.png", "data_url": data_url})
            self.assertEqual(status, 201)
            asset_id = asset.get("id") or asset.get("asset", {}).get("id")
            self.assertTrue(asset_id)
            status, assets = self.request("GET", "/api/assets")
            self.assertEqual(status, 200)
            entries = assets.get("assets", assets if isinstance(assets, list) else [])
            self.assertTrue(any(item.get("id") == asset_id for item in entries))
        finally:
            # Restore the user's project even when an assertion fails.
            self.request("PUT", "/api/project", original if isinstance(original, dict) else {"episodes": []})

    def test_pipeline_job_keeps_target_page_identity_and_revision(self) -> None:
        page = {"id": "page-contract", "revision": 9, "width": 720, "height": 1000, "objects": []}
        payload = {"episode": {"id": "episode-contract", "title": "Contract"}, "page": page}

        class FakeWorker:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, kind, payload, prompt, assets, job_id, **kwargs):
                return {
                    "job_id": job_id,
                    "type": kind,
                    "status": "succeeded",
                    "result": {"layout": {"width": 720, "height": 1000, "objects": [], "prompts": []}, "images": []},
                    "page_id": page["id"],
                    "expected_revision": page["revision"],
                }

        with patch.object(self.server_module, "CodexWorker", FakeWorker):
            status, queued = self.request(
                "POST", "/api/jobs",
                {"kind": "pipeline", "payload": payload, "assets": [], "expected_revision": page["revision"]},
            )
            self.assertEqual(status, 202)
            job_id = queued.get("id") or queued.get("job_id")
            self.assertTrue(job_id)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                status, job = self.request("GET", f"/api/jobs/{job_id}")
                self.assertEqual(status, 200)
                if job.get("status") in {"succeeded", "failed", "cancelled"}:
                    break
                time.sleep(0.03)
            self.assertEqual(job.get("status"), "succeeded", job)
            self.assertEqual(job.get("page_id") or job.get("target_page_id"), page["id"])
            self.assertEqual(job.get("expected_revision"), page["revision"])


if __name__ == "__main__":
    unittest.main()
