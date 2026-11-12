from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import app
from delivery import ExportBlocked, redact, save_delivery

TEST_ROOT = app.ROOT / ".local-test"


class MockProvider(BaseHTTPRequestHandler):
    requests = []
    script = []

    def log_message(self, format, *args):
        pass

    def reply(self, value):
        data = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            return self.reply({"data": [{"id": "test-model"}, {"id": "test-model"}, {"id": "another-model"}]})
        self.reply({"data": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"body": body, "authorization": self.headers.get("Authorization"), "path": self.path})
        if isinstance(body.get("input"), list) and any(item.get("role") == "developer" for item in body["input"]):
            data = json.dumps({"error": {"message": "Unexpected role developer"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        response_id = "resp_" + str(len(self.requests))
        text = self.script.pop(0) if body.get("stream") and self.script else "MOCK_OK"
        item = {"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
        response = {"id": response_id, "object": "response", "created_at": int(time.time()),
                    "status": "completed", "model": body.get("model"), "output": [item],
                    "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}
        incomplete = body.get("model") == "incomplete-model"
        if incomplete:
            response.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        if not body.get("stream"):
            return self.reply(response)
        events = [
            {"type": "response.created", "response": dict(response, status="in_progress", output=[])},
            {"type": "response.output_item.added", "output_index": 0,
             "item": dict(item, status="in_progress", content=[])},
            {"type": "response.content_part.added", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}},
            {"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "delta": text},
            {"type": "response.output_text.done", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "text": text},
            {"type": "response.content_part.done", "item_id": item["id"], "output_index": 0,
             "content_index": 0, "part": item["content"][0]},
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.incomplete" if incomplete else "response.completed", "response": response},
        ]
        data = "".join("event: " + str(event["type"]) + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if body.get("model") == "delayed-model":
            time.sleep(0.3)
        if body.get("model") == "cutoff-model":
            self.wfile.write(data[:100])
            self.wfile.flush()
            self.close_connection = True
        else:
            self.wfile.write(data)


class StudioTests(unittest.TestCase):
    def __init__(self, methodName="runTest"):
        super().__init__(methodName)
        self.root = TEST_ROOT
        self.workspace = TEST_ROOT

    @classmethod
    def setUpClass(cls):
        TEST_ROOT.mkdir(exist_ok=True)
        cls.provider = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
        cls.provider_thread = threading.Thread(target=cls.provider.serve_forever, daemon=True)
        cls.provider_thread.start()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.url = "http://127.0.0.1:" + str(cls.server.server_port)
        cls.provider_url = "http://127.0.0.1:" + str(cls.provider.server_port) + "/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.provider.shutdown()
        cls.provider.server_close()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        patcher = patch.multiple(app, LOCAL=self.root / "private", API_KEY="test-credential",
                                 SETTINGS_PATH=self.root / "config" / "settings.json", SETTINGS={"base_url": self.provider_url, "model": "test-model",
                                           "workspace": str(self.workspace), "remember_key": False, "delivery_path": "",
                                           "developer_role": "system", "api_key_file": "", "max_auto_continues": 0},
                                 JOBS={}, PROCESSES={}, WORKERS={}, BRIDGES={}, SCHEDULER=app.RunScheduler(), SHUTTING_DOWN=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        app.initialize()
        MockProvider.requests.clear()
        MockProvider.script.clear()

    def tearDown(self):
        jobs = app.active_jobs()
        for job in jobs:
            app.stop_job(job)
        for job in jobs:
            self.wait_job(job["id"])
        for worker in list(app.WORKERS.values()):
            worker.join(timeout=5)

    def request(self, path, data=None, headers=None):
        actual = {"X-Local-Token": app.TOKEN, "Content-Type": "application/json"}
        actual.update(headers or {})
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers=actual)
        with urlopen(request, timeout=15) as response:
            return json.load(response)

    def wait_job(self, job_id, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if app.JOBS[job_id].get("finished_at"):
                return app.JOBS[job_id]
            time.sleep(0.1)
        self.fail("任务未在测试超时前结束")

    def test_state_never_returns_key(self):
        state = self.request("/api/state")
        self.assertTrue(state["has_key"])
        self.assertNotIn("test-credential", json.dumps(state))

    def test_key_persistence_opt_in_and_clear(self):
        self.request("/api/settings", {"api_key": "test-credential", "remember_key": True})
        path = app.SETTINGS_PATH
        self.assertEqual(json.loads(path.read_text())["api_key"], "test-credential")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.request("/api/settings", {"remember_key": False})
        self.assertEqual(json.loads(path.read_text())["api_key"], "")
        self.request("/api/settings", {"clear_key": True})
        self.assertFalse(app.API_KEY)

    def test_file_key_is_loaded_even_with_old_remember_flag(self):
        app.SETTINGS_PATH.write_text(json.dumps({"api_key": "test-credential", "remember_key": False,
                                                "workspace": str(self.workspace)}))
        app.API_KEY = ""
        app.SETTINGS["workspace"] = ""
        app.initialize()
        stored = json.loads(app.SETTINGS_PATH.read_text())
        self.assertEqual(app.API_KEY, "test-credential")
        self.assertTrue(app.SETTINGS["remember_key"])
        self.assertEqual(stored["api_key"], "test-credential")
        self.assertNotIn("workspace", stored)
        self.assertEqual(app.SETTINGS["workspace"], "")

    def test_workspace_is_selected_after_startup(self):
        app.SETTINGS["workspace"] = ""
        self.assertEqual(self.request("/api/tree")["children"], [])
        for path in ("/api/run", "/api/delivery"):
            with self.assertRaises(HTTPError):
                self.request(path, {"prompt": "test"})
        selected = self.request("/api/workspace", {"path": str(self.workspace)})
        self.assertEqual(selected["settings"]["workspace"], str(self.workspace))
        self.request("/api/settings", {"model": "test-model"})
        self.assertNotIn("workspace", json.loads(app.SETTINGS_PATH.read_text()))

    def test_external_settings_reload_and_legacy_migration(self):
        app.SETTINGS_PATH.unlink()
        legacy = app.LOCAL / "settings.json"
        legacy.write_text(json.dumps({"base_url": self.provider_url, "model": "test-model",
                                      "remember_key": True, "api_key": "test-credential"}))
        app.initialize()
        self.assertFalse(legacy.exists())
        self.assertTrue(app.SETTINGS_PATH.is_file())
        self.assertEqual(app.SETTINGS_PATH.stat().st_mode & 0o777, 0o600)
        app.API_KEY = ""
        app.initialize()
        self.assertEqual(app.API_KEY, "test-credential")
        self.assertEqual(self.request("/api/state")["settings_path"], str(app.SETTINGS_PATH))

    def test_configuration_can_live_in_workspace_but_stays_private(self):
        app.SETTINGS_PATH = self.workspace / "setting.json"
        app.persist_settings()
        self.assertEqual(app.workspace_path(self.workspace), self.workspace)
        tree = self.request("/api/tree")
        self.assertFalse(any(child["name"] == "setting.json" for child in tree["children"]))
        with self.assertRaises(HTTPError):
            self.request("/api/file?path=setting.json")
        result = self.request("/api/delivery", {"path": str(self.root / "exports")})
        self.assertFalse((Path(result["path"]) / "payload" / "setting.json").exists())

    def test_host_and_csrf_and_origin_protection(self):
        for headers in ({"Host": "evil.example"}, {"X-Local-Token": "invalid"}, {"Origin": "http://evil.example"}):
            with self.assertRaises(HTTPError) as caught:
                self.request("/api/settings", {}, headers)
            self.assertEqual(caught.exception.code, 403)

    def test_models_and_responses_probe(self):
        result = self.request("/api/models", {})
        self.assertEqual(result["models"], ["another-model", "test-model"])
        self.assertTrue(self.request("/api/probe", {})["supported"])
        self.assertEqual(MockProvider.requests[-1]["authorization"], "Bearer test-credential")

    def test_url_rejects_embedded_credentials_and_queries(self):
        for url in ("file:///tmp", "http://user:pass@localhost/v1", "http://localhost/v1?secret=x", "http://localhost/v1#fragment"):
            with self.assertRaises(ValueError):
                app.validate_url(url)

    def test_workspace_open_is_in_place(self):
        other = self.root / "另一个项目"
        other.mkdir()
        (other / "hello.txt").write_text("hello")
        state = self.request("/api/workspace", {"path": str(other)})
        self.assertEqual(state["settings"]["workspace"], str(other))
        tree = self.request("/api/tree")
        self.assertEqual(tree["children"][0]["name"], "hello.txt")
        self.assertEqual(self.request("/api/file?path=hello.txt")["text"], "hello")

    def test_tree_returns_posix_paths_with_windows_relative_paths(self):
        from pathlib import PureWindowsPath
        folder = self.workspace / "level1" / "level2" / "level3"
        folder.mkdir(parents=True)
        (folder / "source.txt").write_text("source", encoding="utf-8")
        original = Path.relative_to
        def windows_relative(path, *parents):
            return PureWindowsPath(original(path, *parents).as_posix())
        with patch.object(Path, "relative_to", windows_relative):
            tree = self.request("/api/tree?path=level1/level2")
            self.assertEqual(tree["path"], "level1/level2")
            self.assertEqual(tree["children"][0]["path"], "level1/level2/level3")
            child = self.request("/api/tree?path=" + tree["children"][0]["path"])
            self.assertEqual(child["children"][0]["path"], "level1/level2/level3/source.txt")
        (self.workspace / "Delivery").mkdir()
        self.assertNotIn("Delivery", [item["name"] for item in self.request("/api/tree")["children"]])
        with self.assertRaises(HTTPError):
            self.request("/api/tree?path=Delivery")

    def test_path_traversal_secrets_and_symlinks_blocked(self):
        (self.workspace / ".env").write_text("secret")
        (self.workspace / "hello.txt").write_text("hello")
        (self.workspace / "alias").symlink_to(self.workspace / "hello.txt")
        for path in ("../outside", ".env", "alias"):
            with self.assertRaises(HTTPError):
                self.request("/api/file?path=" + path)

    def test_delivery_snapshot_exclusions_and_hashes(self):
        (self.workspace / "index.html").write_text("<h1>hello</h1>")
        (self.workspace / ".env").write_text("test-credential")
        (self.workspace / "node_modules").mkdir()
        (self.workspace / "node_modules" / "large.txt").write_text("excluded")
        output = self.workspace / "delivery"
        first = save_delivery(self.workspace, output, [], app.SETTINGS, app.API_KEY)
        second = save_delivery(self.workspace, output, [], app.SETTINGS, app.API_KEY)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        folder = Path(first["path"])
        self.assertEqual(len(json.loads((folder / "file_manifest.json").read_text())["files"]), 1)
        self.assertFalse((folder / "payload" / ".env").exists())
        self.assertEqual(json.loads((folder / "validation.json").read_text())["acceptance_status"], "NOT_READY")

    def test_delivery_configured_path_override_and_remember(self):
        default = self.root / "交付输出"
        override = self.root / "single-export"
        self.request("/api/settings", {"delivery_path": str(default)})
        self.assertEqual(json.loads(app.SETTINGS_PATH.read_text())["delivery_path"], str(default))
        first = self.request("/api/delivery", {})
        self.assertEqual(Path(first["path"]).parent, default)
        second = self.request("/api/delivery", {"path": str(override)})
        self.assertEqual(Path(second["path"]).parent, override)
        self.assertEqual(app.SETTINGS["delivery_path"], str(default))
        self.request("/api/delivery", {"path": str(override), "remember_path": True})
        self.assertEqual(app.SETTINGS["delivery_path"], str(override))
        self.assertEqual(json.loads(app.SETTINGS_PATH.read_text())["delivery_path"], str(override))

    def test_delivery_empty_path_follows_workspace(self):
        self.request("/api/settings", {"delivery_path": ""})
        result = self.request("/api/delivery", {})
        self.assertEqual(Path(result["path"]).parent, self.workspace / "delivery")

    def test_delivery_rejects_invalid_paths(self):
        for path in ("relative/delivery", "/", str(app.LOCAL), str(app.SETTINGS_PATH)):
            with self.assertRaises(HTTPError):
                self.request("/api/settings", {"delivery_path": path})
        self.assertEqual(app.SETTINGS["delivery_path"], "")

    def test_custom_settings_file_is_excluded_from_delivery(self):
        app.SETTINGS_PATH = self.workspace / "connection.json"
        app.persist_settings()
        result = self.request("/api/delivery", {"path": str(self.root / "exports")})
        self.assertFalse((Path(result["path"]) / "payload" / "connection.json").exists())

    def test_delivery_secret_scan_blocks_and_cleans_staging(self):
        (self.workspace / "secret.txt").write_text("sk-" + "x" * 28)
        destination = self.root / "exports"
        with self.assertRaises(ExportBlocked):
            save_delivery(self.workspace, destination, [], app.SETTINGS, app.API_KEY)
        self.assertEqual(list(destination.iterdir()), [])

    def test_delivery_run_records_are_redacted(self):
        job = {"id": "run1", "prompt": "use test-credential", "workspace": str(self.workspace),
               "model": "test-model", "base_url": self.provider_url, "created_at": "2026-01-01",
               "status": "completed", "events": [{"type": "log", "text": "test-credential"}]}
        result = save_delivery(self.workspace, self.root / "exports", [job], app.SETTINGS, app.API_KEY)
        for path in Path(result["path"]).rglob("*"):
            if path.is_file():
                self.assertNotIn("test-credential", path.read_text())

    def test_private_key_log_redaction_removes_body(self):
        text = "-----BEGIN " + "PRIVATE KEY-----\nprivate-body\n-----END " + "PRIVATE KEY-----"
        self.assertNotIn("private-body", redact(text))
        self.assertIn("REDACTED_PRIVATE_KEY", redact(text))

    def test_role_normalization_preserves_tools_and_content(self):
        body: dict[str, Any] = {"input": [{"role": "developer", "content": "instruction"},
                          {"type": "function_call_output", "call_id": "call1", "output": "value"},
                          {"role": "assistant", "content": "answer"}],
                "tools": [{"type": "function", "name": "test"}], "stream": True}
        result = app.normalize_roles(body)
        self.assertEqual(result["input"][0]["role"], "system")
        self.assertEqual(body["input"][0]["role"], "developer")
        self.assertEqual(result["input"][1:], body["input"][1:])
        self.assertEqual(result["tools"], body["tools"])

    def test_probe_catches_native_role_error_and_compatible_mode_succeeds(self):
        self.request("/api/settings", {"developer_role": "developer"})
        with self.assertRaises(HTTPError):
            self.request("/api/probe", {})
        self.request("/api/settings", {"developer_role": "system"})
        self.assertTrue(self.request("/api/probe", {})["supported"])
        self.assertEqual(MockProvider.requests[-1]["body"]["input"][0]["role"], "system")
        self.assertTrue(MockProvider.requests[-1]["body"]["tools"])

    def test_bridge_auth_streaming_and_cleanup(self):
        bridge = app.ResponsesBridge(self.provider_url, "test-credential")
        try:
            request = Request(bridge.url + "/responses", data=b'{}',
                              headers={"Content-Type": "application/json", "Authorization": "Bearer invalid"})
            with self.assertRaises(HTTPError) as caught:
                urlopen(request, timeout=5)
            self.assertEqual(caught.exception.code, 403)
            self.assertFalse(MockProvider.requests)
            body = {"model": "test-model", "input": [{"role": "developer", "content": "test"}], "stream": True}
            request = Request(bridge.url + "/responses", data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json", "Authorization": "Bearer " + bridge.token})
            with urlopen(request, timeout=5) as response:
                data = response.read().decode()
                self.assertEqual(response.headers["Content-Type"], "text/event-stream")
            self.assertIn("response.output_text.delta", data)
            self.assertIn("MOCK_OK", data)
            self.assertEqual(MockProvider.requests[-1]["authorization"], "Bearer test-credential")
            self.assertEqual(MockProvider.requests[-1]["body"]["input"][0]["role"], "system")
        finally:
            bridge.close()
        self.assertFalse(bridge.thread.is_alive())

    def test_token_file_is_parsed_not_executed_and_never_copied(self):
        source = self.root / "token_set.py"
        sentinel = self.root / "executed.txt"
        source.write_text("API_KEY = 'test-credential'\\n".replace("\\n", "\n") +
                          "open(" + repr(str(sentinel)) + ", 'w').write('executed')\n")
        self.request("/api/settings", {"api_key_file": str(source)})
        self.assertEqual(app.API_KEY, "test-credential")
        self.assertFalse(sentinel.exists())
        self.assertEqual(json.loads(app.SETTINGS_PATH.read_text())["api_key"], "")
        app.API_KEY = ""
        app.initialize()
        self.assertEqual(app.API_KEY, "test-credential")
        self.request("/api/settings", {"clear_key": True})
        self.assertEqual(app.SETTINGS["api_key_file"], "")
        self.assertFalse(app.API_KEY)

    def wait_task_settled(self, task_id, count, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with app.LOCK:
                jobs = sorted((job for job in app.JOBS.values() if job.get("task_id") == task_id),
                              key=lambda job: job["created_at"])
                settled = all(job.get("finished_at") and not job.get("auto_continue_pending") for job in jobs)
            if len(jobs) >= count and settled:
                time.sleep(0.5)
                with app.LOCK:
                    again = [job for job in app.JOBS.values() if job.get("task_id") == task_id]
                if len(again) == len(jobs):
                    return jobs
            time.sleep(0.1)
        self.fail("自动续跑未在测试超时前结束")

    def test_auto_continue_until_complete_marker(self):
        if not app.CODEX.is_file():
            self.skipTest("官方 Codex 尚未安装")
        MockProvider.script.extend(["Now for the next layer.", "Still working on it.",
                                    "All requirements verified.\n" + app.COMPLETE_MARK])
        started = self.request("/api/run", {"prompt": "Finish everything.", "max_auto_continues": 5})
        jobs = self.wait_task_settled(started["task_id"], 3)
        self.assertEqual(len(jobs), 3, msg=json.dumps([app.job_summary(job) for job in jobs], ensure_ascii=False))
        self.assertEqual([job["auto_continue_count"] for job in jobs], [0, 1, 2])
        self.assertEqual([job["completion_state"] for job in jobs], ["continuing", "continuing", "complete"])
        self.assertTrue(all(job["status"] == "completed" for job in jobs))
        self.assertEqual(jobs[1]["resume_id"], jobs[0]["thread_id"])
        self.assertEqual(jobs[2]["parent_job_id"], jobs[1]["id"])
        self.assertIn("自动继续", jobs[1]["prompt"])
        bodies = [json.dumps(item["body"], ensure_ascii=False) for item in MockProvider.requests]
        self.assertIn(app.COMPLETE_MARK, bodies[0])
        self.assertTrue(any("自动继续（第 2/5 次）" in body for body in bodies))

    def test_auto_continue_stops_at_limit_and_on_need_input(self):
        if not app.CODEX.is_file():
            self.skipTest("官方 Codex 尚未安装")
        MockProvider.script.extend(["partial 1", "partial 2", "partial 3", "partial 4"])
        started = self.request("/api/run", {"prompt": "Long task.", "max_auto_continues": 2})
        jobs = self.wait_task_settled(started["task_id"], 3)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[-1]["completion_state"], "limit_reached")
        MockProvider.script[:] = ["Need the account name.\n" + app.NEED_INPUT_MARK]
        other = self.request("/api/run", {"prompt": "Ask me.", "max_auto_continues": 2})
        asked = self.wait_task_settled(other["task_id"], 1)
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["completion_state"], "needs_input")

    def test_auto_continue_disabled_and_not_after_user_stop(self):
        job: dict[str, Any] = {"kind": "task", "status": "completed", "max_auto_continues": 0, "thread_id": "t",
                               "events": [{"type": "item.completed", "item": {"type": "agent_message", "text": "next"}}]}
        app.judge_completion(job)
        self.assertFalse(job["auto_continue_pending"])
        self.assertEqual(job["completion_state"], "incomplete")
        job.update(status="cancelled", stop_reason="cancelled", max_auto_continues=5, completion_state=None)
        app.judge_completion(job)
        self.assertFalse(job["auto_continue_pending"])
        self.assertIsNone(job["completion_state"])
        job.update(status="failed", stop_reason=None, error="stream disconnected before completion", auto_failures=0)
        app.judge_completion(job)
        self.assertTrue(job["auto_continue_pending"])
        self.assertEqual(job["next_auto_failures"], 1)
        job.update(auto_failures=app.MAX_AUTO_RETRY_FAILURES)
        app.judge_completion(job)
        self.assertFalse(job["auto_continue_pending"])
        job.update(error="max_output_tokens", error_code="max_output_tokens", auto_failures=0)
        app.judge_completion(job)
        self.assertFalse(job["auto_continue_pending"])
        summary: dict[str, Any] = {"kind": "summary", "status": "completed", "max_auto_continues": 5, "events": []}
        app.judge_completion(summary)
        self.assertFalse(summary["auto_continue_pending"])

    def test_tool_images_move_to_user_message_and_are_limited(self):
        from provider_bridge import relocate_tool_images
        image = {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 1000}
        body = {"input": [{"role": "user", "content": "look"},
                          {"type": "function_call", "call_id": "c1", "name": "view_image", "arguments": "{}"},
                          {"type": "function_call", "call_id": "c2", "name": "view_image", "arguments": "{}"},
                          {"type": "function_call_output", "call_id": "c1", "output": [image]},
                          {"type": "function_call_output", "call_id": "c2",
                           "output": [{"type": "input_text", "text": "note"}, image]},
                          {"role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]}
        result, stats = relocate_tool_images(body, max_images=6)
        items = result["input"]
        self.assertEqual(stats, {"moved_images": 2, "dropped_images": 0})
        self.assertEqual([item.get("type") for item in items[3:6]], ["function_call_output", "function_call_output", "message"])
        self.assertIsInstance(items[3]["output"], str)
        self.assertIn("note", items[4]["output"])
        self.assertNotIn("base64", json.dumps(items[3:5]))
        self.assertEqual(items[5]["role"], "user")
        self.assertEqual(sum(part["type"] == "input_image" for part in items[5]["content"]), 2)
        self.assertEqual(items[6]["role"], "assistant")
        self.assertEqual(body["input"][3]["output"], [image])
        many = {"input": [{"type": "function_call_output", "call_id": "c" + str(i), "output": [image]}
                          if i % 2 else {"role": "assistant", "content": "x"} for i in range(20)]}
        limited, stats = relocate_tool_images(many, max_images=3)
        self.assertEqual(stats["moved_images"], 10)
        self.assertEqual(stats["dropped_images"], 7)
        self.assertEqual(json.dumps(limited).count("input_image"), 3)
        self.assertGreater(json.dumps(limited).rfind("input_image"), json.dumps(limited).rfind("较早的图片"))
        plain = {"input": [{"role": "user", "content": "hi"}]}
        self.assertIs(relocate_tool_images(plain)[0], plain)

    def test_large_screenshots_are_shrunk_and_cached(self):
        import base64, os, struct, zlib
        import provider_bridge
        if provider_bridge.Image is None and not os.path.exists(provider_bridge.SIPS):
            self.skipTest("Pillow 和 sips 均不可用")
        width, height = 2400, 300
        raw = b"".join(b"\0" + os.urandom(width * 3) for _ in range(height))
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        small = "data:image/png;base64,AAAA"
        body: dict[str, Any] = {"input": [{"role": "user", "content": [{"type": "input_image", "image_url": url},
                                                        {"type": "input_image", "image_url": small},
                                                        {"type": "input_text", "text": "t"}]}]}
        cache = {}
        result, stats = provider_bridge.shrink_images(body, cache)
        first = result["input"][0]["content"][0]["image_url"]
        self.assertTrue(first.startswith("data:image/jpeg;base64,"))
        self.assertLess(len(first), len(url))
        self.assertEqual(result["input"][0]["content"][1]["image_url"], small)
        self.assertEqual(stats["shrunk_images"], 1)
        self.assertEqual(body["input"][0]["content"][0]["image_url"], url)
        with patch.object(provider_bridge, "SIPS", "/nonexistent/sips"):
            again, _ = provider_bridge.shrink_images(body, cache)
        self.assertEqual(again["input"][0]["content"][0]["image_url"], first)
        with patch.object(provider_bridge, "SIPS", "/nonexistent/sips"), \
             patch.object(provider_bridge, "Image", None), patch.object(provider_bridge, "ImageOps", None):
            fallback, stats = provider_bridge.shrink_images(body, {})
        self.assertEqual(fallback["input"][0]["content"][0]["image_url"], url)
        self.assertEqual(stats["shrunk_images"], 0)
        self.assertGreater(provider_bridge.MAX_INCOMING_BYTES, 64 * 1024 * 1024)

    def test_context_image_limit_is_adjustable_while_running(self):
        image = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        body = {"model": "test-model", "input": [{"type": "function_call_output", "call_id": "c" + str(i), "output": [image]}
                                                 for i in range(5)]}
        job: dict[str, Any] = {"max_context_images": 4}
        bridge = app.ResponsesBridge(self.provider_url, "test-credential", max_images=lambda: job["max_context_images"])
        def send():
            request = Request(bridge.url + "/responses", data=json.dumps(body).encode(),
                              headers={"Content-Type": "application/json", "Authorization": "Bearer " + bridge.token})
            with urlopen(request, timeout=5) as response:
                response.read()
            return json.dumps(MockProvider.requests[-1]["body"]).count("input_image")
        try:
            self.assertEqual(send(), 4)
            job["max_context_images"] = 1
            self.assertEqual(send(), 1)
            job["max_context_images"] = 0
            self.assertEqual(send(), 0)
        finally:
            bridge.close()
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            started = self.request("/api/run", {"prompt": "images", "max_context_images": 3})
            self.assertEqual(app.JOBS[started["id"]]["max_context_images"], 3)
            changed = self.request("/api/job-images", {"id": started["id"], "max_context_images": 10})
            self.assertEqual(changed["max_context_images"], 10)
            self.assertEqual(app.JOBS[started["id"]]["max_context_images"], 10)
            for invalid in (-1, 51, "x", None):
                with self.assertRaises(HTTPError):
                    self.request("/api/job-images", {"id": started["id"], "max_context_images": invalid})
            self.wait_job(started["id"])
        self.request("/api/settings", {"max_context_images": 2})
        self.assertEqual(json.loads(app.SETTINGS_PATH.read_text())["max_context_images"], 2)

    def test_unsigned_reasoning_items_are_stripped(self):
        from provider_bridge import strip_unsigned_reasoning
        body: dict[str, Any] = {"input": [
            {"role": "user", "content": "hi"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "x"}], "encrypted_content": None},
            {"type": "reasoning", "summary": [], "encrypted_content": "signed"},
            {"type": "function_call", "call_id": "c", "name": "shell", "arguments": "{}"}]}
        result, removed = strip_unsigned_reasoning(body)
        self.assertEqual(removed, 1)
        self.assertEqual([item.get("type") for item in result["input"]], [None, "reasoning", "function_call"])
        self.assertEqual(len(body["input"]), 4)
        plain = {"input": [{"role": "user", "content": "hi"}]}
        self.assertIs(strip_unsigned_reasoning(plain)[0], plain)

    def test_api_key_wrappers_are_cleaned_and_non_ascii_rejected(self):
        self.assertEqual(app.clean_key("“sk-test123”"), "sk-test123")
        self.assertEqual(app.clean_key("\u200b Bearer sk-test123 \u3000\n"), "sk-test123")
        self.assertEqual(app.clean_key("'sk-test123'"), "sk-test123")
        for bad in ("sk-测试123", "sk test123", "sk-te\u200bst"):
            with self.assertRaises(ValueError):
                app.clean_key(bad)
        self.request("/api/settings", {"api_key": "“test-credential”"})
        self.assertEqual(app.API_KEY, "test-credential")
        with self.assertRaises(HTTPError):
            self.request("/api/settings", {"api_key": "test-密钥"})
        self.assertEqual(app.API_KEY, "test-credential")
        app.SETTINGS_PATH.write_text(json.dumps({"api_key": "“test-credential”"}))
        app.initialize()
        self.assertEqual(app.API_KEY, "test-credential")

    def test_content_filter_and_key_encoding_errors_are_explained_and_not_retried(self):
        for message, code in (("stream disconnected before completion: Incomplete response returned, reason: content_filter", "content_filter"),
                              ("模型代理连接失败：'latin-1' codec can't encode characters in position 7-8: ordinal not in range(256)", "invalid_key_characters")):
            job: dict[str, Any] = {"id": code, "workspace": str(self.workspace), "events": [], "status": "running",
                                   "prompt": "x", "created_at": "now", "kind": "task", "max_auto_continues": 5, "thread_id": "t"}
            app.append_event(job, {"type": "provider.warning", "code": "stream_disconnected", "message": message}, app.API_KEY)
            app.append_event(job, {"type": "turn.failed", "error": {"message": message}}, app.API_KEY)
            self.assertEqual(job["error_code"], code)
            job["status"] = "failed"
            app.judge_completion(job)
            self.assertFalse(job["auto_continue_pending"])

    def test_request_too_large_is_explained_and_not_retried(self):
        job: dict[str, Any] = {"id": "big", "workspace": str(self.workspace), "events": [], "status": "running",
                               "prompt": "x", "created_at": "now", "kind": "task", "max_auto_continues": 5, "thread_id": "t"}
        app.append_event(job, {"type": "turn.failed", "error": {"message": "unexpected status 413 Payload Too Large: x"}}, app.API_KEY)
        self.assertEqual(job["error_code"], "request_too_large")
        job["status"] = "failed"
        app.judge_completion(job)
        self.assertFalse(job["auto_continue_pending"])

    def test_prompt_too_long_is_explained_and_not_retried(self):
        job: dict[str, Any] = {"id": "long", "workspace": str(self.workspace), "events": [], "status": "running",
                               "prompt": "x", "created_at": "now", "kind": "task", "max_auto_continues": 5, "thread_id": "t"}
        app.append_event(job, {"type": "turn.failed", "error": {"message": "prompt is too long: 1437877 tokens > 1000000 maximum"}}, app.API_KEY)
        self.assertEqual(job["error_code"], "prompt_too_long")
        job["status"] = "failed"
        app.judge_completion(job)
        self.assertFalse(job["auto_continue_pending"])

    def test_codex_command_keeps_sandbox_and_key_out_of_argv(self):
        job = {"model": "test-model", "base_url": self.provider_url, "allow_network": False}
        command = app.codex_command(job)
        self.assertIn('sandbox_mode="workspace-write"', command)
        self.assertIn('approval_policy="never"', command)
        self.assertNotIn("test-credential", " ".join(command))
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_installed_codex_responses_and_resume_end_to_end(self):
        if not app.CODEX.is_file():
            self.skipTest("官方 Codex 尚未安装")
        started = self.request("/api/run", {"prompt": "Reply only MOCK_OK. Do not execute tools."})
        job = self.wait_job(started["id"])
        self.assertEqual(job["status"], "completed", msg=json.dumps(job["events"][-8:], ensure_ascii=False))
        self.assertTrue(job["thread_id"])
        self.assertTrue(any(event.get("item", {}).get("text") == "MOCK_OK" for event in job["events"]))
        self.assertEqual(MockProvider.requests[-1]["body"]["model"], "test-model")
        self.assertEqual(MockProvider.requests[-1]["path"], "/v1/responses")
        second = self.request("/api/run", {"prompt": "Again reply only MOCK_OK.", "resume_id": job["thread_id"]})
        resumed = self.wait_job(second["id"])
        self.assertEqual(resumed["status"], "completed", msg=json.dumps(resumed["events"][-8:], ensure_ascii=False))
        self.assertEqual(resumed["thread_id"], job["thread_id"])
        self.assertEqual(len(MockProvider.requests), 2)

    def test_output_budget_and_context_limits_are_validated(self):
        with self.assertRaises(ValueError):
            app.validate_token_limits({"max_output_tokens": 0})
        with self.assertRaises(ValueError):
            app.validate_token_limits({"model_auto_compact_token_limit": 380000})
        self.request("/api/settings", {"max_output_tokens": 16384})
        self.assertEqual(json.loads(app.SETTINGS_PATH.read_text())["max_output_tokens"], 16384)
        command = app.codex_command({"model": "test-model", "base_url": self.provider_url, "allow_network": False})
        self.assertIn("model_context_window=400000", command)
        self.assertIn("model_auto_compact_token_limit=280000", command)
        self.assertIn('model_providers.local_gateway.capabilities.remote_compaction="unsupported"', command)

    def test_incomplete_probe_does_not_report_success(self):
        app.SETTINGS["model"] = "incomplete-model"
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/probe", {})
        self.assertIn("max_output_tokens", caught.exception.read().decode())

    def test_incomplete_cli_is_not_replayed_and_records_are_saved(self):
        app.SETTINGS.update(model="incomplete-model", delivery_path=str(self.root / "G001"))
        started = self.request("/api/run", {"prompt": "Reply MOCK_OK."})
        job = self.wait_job(started["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "max_output_tokens")
        self.assertEqual(len(MockProvider.requests), 1)
        self.assertEqual(MockProvider.requests[0]["body"]["max_output_tokens"], 65536)
        result = json.loads((Path(job["delivery_records_path"]) / "result.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "max_output_tokens")

    def test_summary_then_fresh_thread_uses_only_summary(self):
        app.SETTINGS["delivery_path"] = str(self.root / "G001")
        first = self.wait_job(self.request("/api/run", {"prompt": "old-reference-content"})["id"])
        summary = self.wait_job(self.request("/api/run", {"kind": "summary", "resume_id": first["thread_id"]})["id"])
        self.assertEqual(summary["status"], "completed")
        self.assertIn("MOCK_OK", summary["context_summary"])
        self.assertTrue((Path(summary["delivery_records_path"]) / "context_summary.json").exists())
        next_job = self.wait_job(self.request("/api/run", {"prompt": "new task", "summary_id": summary["id"]})["id"])
        self.assertEqual(next_job["status"], "completed")
        self.assertNotEqual(first["thread_id"], next_job["thread_id"])
        latest_input = json.dumps(MockProvider.requests[-1]["body"]["input"])
        self.assertIn("MOCK_OK", latest_input)
        self.assertNotIn("old-reference-content", latest_input)
        self.assertEqual(len(list((self.root / "G001" / "agent_runs").iterdir())), 3)
        self.assertEqual(first["task_id"], summary["task_id"])
        self.assertEqual(first["task_id"], next_job["task_id"])
        self.assertEqual(next_job["parent_job_id"], summary["id"])
        tasks = self.request("/api/state")["tasks"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["run_count"], 3)

    def test_setting_delivery_path_backfills_existing_interactions(self):
        job = self.wait_job(self.request("/api/run", {"prompt": "hello"})["id"])
        target = self.root / "G001"
        self.request("/api/settings", {"delivery_path": str(target)})
        self.assertTrue((target / "agent_runs" / job["id"] / "request.json").exists())
        self.assertTrue((target / "agent_runs" / job["id"] / "response.json").exists())
        self.assertEqual(json.loads((target / "agent_runs" / job["id"] / "result.json").read_text())["status"], "completed")

    def test_delivery_write_failure_is_visible_without_losing_local_record(self):
        app.SETTINGS["delivery_path"] = str(self.root / "G001")
        with patch.object(app, "save_run_records", side_effect=PermissionError("no permission")):
            job = self.wait_job(self.request("/api/run", {"prompt": "hello"})["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["delivery_error"], "no permission")
            self.assertTrue((app.LOCAL / "runs" / (job["id"] + ".json")).exists())

    def test_all_persistent_runs_are_restored_for_delivery(self):
        for index in range(105):
            job: dict[str, Any] = {"id": "run-" + str(index), "workspace": str(self.workspace), "status": "completed",
                   "prompt": "history", "created_at": str(index), "events": []}
            app.write_json(app.LOCAL / "runs" / (job["id"] + ".json"), job)
        app.initialize()
        self.assertEqual(len(app.JOBS), 105)
        self.request("/api/settings", {"delivery_path": str(self.root / "G001")})
        self.assertEqual(len(list((self.root / "G001" / "agent_runs").iterdir())), 105)

    def test_terminal_event_is_preserved_past_old_limit(self):
        job: dict[str, Any] = {"id": "many-events", "workspace": str(self.workspace), "events": [{"type": "log"}] * 10000,
               "status": "running", "prompt": "test", "created_at": "now"}
        app.append_event(job, {"type": "turn.failed", "error": {"message": "max_output_tokens"}}, app.API_KEY)
        self.assertEqual(len(job["events"]), 10001)
        self.assertEqual(job["events"][-1]["type"], "turn.failed")
        self.assertEqual(job["error_code"], "max_output_tokens")

    def test_task_grouping_migrates_old_threads_summary_and_failed_continuation(self):
        base: dict[str, Any] = {"workspace": str(self.workspace), "base_url": self.provider_url,
                                "prompt": "task", "status": "completed", "events": []}
        jobs = [dict(base, id="first", created_at="01", thread_id="thread1"),
                dict(base, id="failed", created_at="02", resume_id="thread1", status="failed"),
                dict(base, id="summary", created_at="03", resume_id="thread1", thread_id="thread1", kind="summary"),
                dict(base, id="resumed", created_at="04", summary_id="summary", thread_id="thread2"),
                dict(base, id="separate", created_at="05", thread_id="thread3"),
                dict(base, id="different-project", created_at="06", thread_id="thread1", workspace="/other")]
        app.assign_task_ids(jobs)
        self.assertEqual({job["task_id"] for job in jobs[:4]}, {"first"})
        self.assertNotEqual(jobs[4]["task_id"], "first")
        self.assertNotEqual(jobs[5]["task_id"], "first")
        before = json.dumps(jobs, sort_keys=True)
        app.assign_task_ids(jobs)
        self.assertEqual(json.dumps(jobs, sort_keys=True), before)
        tasks = app.group_tasks(jobs)
        task = next(item for item in tasks if item["id"] == "first")
        self.assertEqual(task["run_ids"], ["first", "failed", "summary", "resumed"])
        self.assertEqual(task["title"], "task")

    def test_task_identity_is_restored_and_independent_tasks_stay_separate(self):
        first = self.wait_job(self.request("/api/run", {"prompt": "task one"})["id"])
        continued = self.wait_job(self.request("/api/run", {"prompt": "follow up", "resume_id": first["thread_id"],
                                                               "task_id": first["task_id"], "parent_job_id": first["id"]})["id"])
        separate = self.wait_job(self.request("/api/run", {"prompt": "task two"})["id"])
        self.assertEqual(first["task_id"], continued["task_id"])
        self.assertNotEqual(first["task_id"], separate["task_id"])
        app.JOBS.clear()
        app.initialize()
        tasks = self.request("/api/state")["tasks"]
        self.assertEqual(len(tasks), 2)
        self.assertEqual(sorted(item["run_count"] for item in tasks), [1, 2])
        self.assertEqual(app.JOBS[continued["id"]]["parent_job_id"], first["id"])

    def test_failure_without_thread_can_retry_in_same_task(self):
        failed: dict[str, Any] = {"id": "failed-run", "task_id": "failed-run", "workspace": str(self.workspace),
                                 "base_url": self.provider_url, "status": "failed", "prompt": "initial task",
                                 "created_at": "2020-01-01", "events": [], "thread_id": None}
        app.JOBS[failed["id"]] = failed
        retry = self.wait_job(self.request("/api/run", {"prompt": "retry safely", "task_id": failed["task_id"],
                                                         "parent_job_id": failed["id"]})["id"])
        self.assertEqual(retry["task_id"], failed["task_id"])
        self.assertEqual(len(self.request("/api/state")["tasks"]), 1)

    def test_task_grouping_rejects_cross_task_and_cross_project_parent(self):
        base: dict[str, Any] = {"workspace": str(self.workspace), "base_url": self.provider_url,
                                "prompt": "task", "status": "completed", "created_at": "now", "events": []}
        app.JOBS.update(a=dict(base, id="a", task_id="a", thread_id="thread-a"),
                        b=dict(base, id="b", task_id="b", thread_id="thread-b"),
                        other=dict(base, id="other", task_id="other", workspace="/other"))
        for extra in ({"task_id": "a", "resume_id": "thread-b"}, {"parent_job_id": "other"},
                      {"task_id": "missing"}, {"parent_job_id": "missing"}):
            with self.assertRaises(HTTPError):
                self.request("/api/run", {"prompt": "test", **extra})
        self.assertFalse(MockProvider.requests)

    def fake_sleep_agent(self):
        executable = self.root / "fake_codex"
        executable.write_text('#!/usr/bin/python3\nimport time,json\nfrom pathlib import Path\nPath("started.txt").write_text("started")\nprint(json.dumps({"type":"turn.started"}),flush=True)\ntime.sleep(2)\nprint(json.dumps({"type":"turn.completed","usage":{}}),flush=True)\n')
        executable.chmod(0o700)
        return executable

    def wait_running(self, run_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if run_id in app.PROCESSES:
                return
            time.sleep(0.02)
        self.fail("任务未启动")

    def test_parallel_projects_and_stop_only_selected_run(self):
        other = self.root / "other-project"
        other.mkdir()
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            a = self.request("/api/run", {"prompt": "a", "workspace": str(self.workspace), "model": "model-a"})
            b = self.request("/api/run", {"prompt": "b", "workspace": str(other), "model": "model-b"})
            self.wait_running(a["id"])
            self.wait_running(b["id"])
            self.assertEqual(len(app.PROCESSES), 2)
            self.request("/api/stop", {"id": a["id"]})
            self.assertEqual(self.wait_job(a["id"])["status"], "cancelled")
            self.assertEqual(self.wait_job(b["id"])["status"], "completed")
            self.assertEqual(app.JOBS[a["id"]]["model"], "model-a")
            self.assertEqual(app.JOBS[b["id"]]["model"], "model-b")

    def test_official_codex_processes_run_concurrently_in_distinct_projects(self):
        if not app.CODEX.is_file():
            self.skipTest("Codex 未安装")
        other = self.root / "other-project"
        other.mkdir()
        a = self.request("/api/run", {"prompt": "Reply MOCK_OK without tools.", "model": "delayed-model"})
        b = self.request("/api/run", {"prompt": "Reply MOCK_OK without tools.", "workspace": str(other), "model": "delayed-model"})
        self.wait_running(a["id"])
        self.wait_running(b["id"])
        self.assertEqual(len(app.PROCESSES), 2)
        first, second = self.wait_job(a["id"]), self.wait_job(b["id"])
        self.assertEqual(first["status"], "completed")
        self.assertEqual(second["status"], "completed")
        self.assertNotEqual(first["thread_id"], second["thread_id"])
        self.assertLess(first["started_at"], second["finished_at"])
        self.assertLess(second["started_at"], first["finished_at"])

    def test_same_workspace_queue_can_cancel_without_starting(self):
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            first = self.request("/api/run", {"prompt": "first"})
            self.wait_running(first["id"])
            second = self.request("/api/run", {"prompt": "second"})
            time.sleep(0.1)
            self.assertEqual(app.JOBS[second["id"]]["status"], "queued")
            self.assertNotIn(second["id"], app.PROCESSES)
            self.request("/api/stop", {"id": second["id"]})
            self.assertEqual(self.wait_job(second["id"])["status"], "cancelled")
            self.assertEqual(self.wait_job(first["id"])["status"], "completed")

    def test_same_workspace_second_task_runs_after_first(self):
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            first = self.request("/api/run", {"prompt": "first"})
            self.wait_running(first["id"])
            second = self.request("/api/run", {"prompt": "second"})
            a, b = self.wait_job(first["id"]), self.wait_job(second["id"])
            self.assertEqual(b["status"], "completed")
            self.assertGreaterEqual(b["started_at"], a["finished_at"])

    def test_parallel_limit_and_same_task_are_enforced(self):
        other = self.root / "other-project"
        other.mkdir()
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            first = self.request("/api/run", {"prompt": "first", "max_parallel_runs": 1})
            self.wait_running(first["id"])
            second = self.request("/api/run", {"prompt": "second", "workspace": str(other), "max_parallel_runs": 1})
            time.sleep(0.1)
            self.assertEqual(app.JOBS[second["id"]]["status"], "queued")
            with self.assertRaises(HTTPError):
                self.request("/api/run", {"prompt": "same task", "task_id": first["task_id"], "parent_job_id": first["id"]})
            self.wait_job(first["id"])
            self.assertEqual(self.wait_job(second["id"])["status"], "completed")

    def test_window_workspace_isolation_and_settings_snapshot(self):
        other = self.root / "other-project"
        other.mkdir()
        (other / "b.txt").write_text("b")
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            first = self.request("/api/run", {"prompt": "first", "workspace": str(self.workspace), "model": "before"})
            self.wait_running(first["id"])
            response = self.request("/api/workspace", {"path": str(other), "window_context": True})
            self.assertEqual(response["settings"]["workspace"], str(other))
            self.assertEqual(app.SETTINGS["workspace"], str(self.workspace))
            self.request("/api/settings", {"model": "after"})
            self.assertEqual(app.JOBS[first["id"]]["model"], "before")
            tree = self.request("/api/tree?workspace=" + str(other))
            self.assertTrue(any(item["name"] == "b.txt" for item in tree["children"]))
            self.assertEqual(self.wait_job(first["id"])["status"], "completed")

    def test_run_submission_idempotency(self):
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            body = {"prompt": "once", "client_request_id": "browser-request-1"}
            first = self.request("/api/run", body)
            again = self.request("/api/run", body)
            self.assertEqual(first["id"], again["id"])
            self.assertEqual(len(app.JOBS), 1)
            with self.assertRaises(HTTPError):
                self.request("/api/run", {"prompt": "different", "client_request_id": "browser-request-1"})
            self.wait_job(first["id"])
            self.assertEqual(self.request("/api/run", body)["id"], first["id"])

    def test_runtime_timeout_is_opt_in_and_reports_reason(self):
        self.assertEqual(app.validate_runtime_limits({})["task_timeout_seconds"], 0)
        command = app.codex_command({"model": "test-model", "base_url": self.provider_url, "allow_network": False})
        self.assertIn("model_providers.local_gateway.stream_idle_timeout_ms=900000", command)
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            start = self.request("/api/run", {"prompt": "timeout", "task_timeout_seconds": 1})
            job = self.wait_job(start["id"])
            self.assertEqual(job["status"], "timeout")
            self.assertEqual(job["error_code"], "task_timeout")

    def test_shutdown_stops_all_running_and_queued_jobs(self):
        other = self.root / "other-project"
        other.mkdir()
        with patch.object(app, "CODEX", self.fake_sleep_agent()):
            a = self.request("/api/run", {"prompt": "a"})
            b = self.request("/api/run", {"prompt": "b", "workspace": str(other)})
            self.wait_running(a["id"])
            self.wait_running(b["id"])
            queued = self.request("/api/run", {"prompt": "queued"})
            app.stop_all_jobs()
            for job in (a, b, queued):
                self.assertEqual(self.wait_job(job["id"])["status"], "cancelled")
            with self.assertRaises(HTTPError):
                self.request("/api/run", {"prompt": "after shutdown"})

    def test_transient_reconnect_warning_is_not_terminal_failure(self):
        job: dict[str, Any] = {"id": "retry", "workspace": str(self.workspace), "events": [], "status": "running"}
        app.append_event(job, {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected)"}, app.API_KEY)
        self.assertNotIn("error", job)
        self.assertEqual(job["events"][0]["type"], "provider.warning")

    def test_bridge_heartbeats_and_reports_premature_stream_eof(self):
        for model in ("delayed-model", "cutoff-model"):
            warnings = []
            bridge = app.ResponsesBridge(self.provider_url, "test-credential", idle_timeout=2,
                                         heartbeat_seconds=0.05, on_request=warnings.append)
            try:
                request = Request(bridge.url + "/responses", data=json.dumps({"model": model, "stream": True, "input": "test"}).encode(),
                                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + bridge.token})
                with urlopen(request, timeout=5) as response:
                    text = response.read().decode()
                if model == "delayed-model":
                    self.assertIn(": local-studio heartbeat", text)
                    self.assertIn("response.completed", text)
                else:
                    self.assertIn("stream_disconnected", text)
                    self.assertFalse("response.completed" in text)
                    self.assertTrue(any(event.get("code") == "stream_disconnected" for event in warnings))
            finally:
                bridge.close()

    def test_stop_server_no_listener_is_safe(self):
        with patch.object(app, "listening_pids", return_value=set()), patch.object(app.os, "kill") as kill:
            self.assertIn("无需停止", app.stop_server(8765))
            kill.assert_not_called()

    def test_stop_server_refuses_other_project(self):
        with patch.object(app, "listening_pids", return_value={123}), \
             patch.object(app.subprocess, "check_output", side_effect=["/usr/bin/python3 app.py", "p123\nn/other/project\n"]), \
             patch.object(app.os, "kill") as kill:
            with self.assertRaises(ValueError):
                app.stop_server(8765)
            kill.assert_not_called()

    def test_stop_server_verifies_owner_before_sigterm(self):
        with patch.object(app, "listening_pids", side_effect=[{123}, {123}, set(), set()]), \
             patch.object(app.subprocess, "check_output", side_effect=["/usr/bin/python3 app.py --port 8765",
                                                                      "p123\nn" + str(app.ROOT) + "\n"]), \
             patch.object(app.os, "kill") as kill:
            self.assertIn("已停止", app.stop_server(8765))
            kill.assert_called_once_with(123, app.signal.SIGTERM)

    def test_stop_server_refuses_changed_listener(self):
        with patch.object(app, "listening_pids", side_effect=[{123}, {456}]), \
             patch.object(app.subprocess, "check_output", side_effect=["/usr/bin/python3 app.py",
                                                                      "p123\nn" + str(app.ROOT) + "\n"]), \
             patch.object(app.os, "kill") as kill:
            with self.assertRaises(ValueError):
                app.stop_server(8765)
            kill.assert_not_called()

    def test_stop_cli_does_not_load_settings_or_start_server(self):
        with patch("sys.argv", ["app.py", "--stop", "--port", "8766"]), \
             patch.object(app, "stop_server", return_value="stopped") as stop, \
             patch.object(app, "initialize") as initialize, \
             patch.object(app, "ThreadingHTTPServer") as server:
            app.main()
            stop.assert_called_once_with(8766)
            initialize.assert_not_called()
            server.assert_not_called()

    def test_stop_terminates_child_process(self):
        executable = self.root / "fake_codex"
        executable.write_text('#!/usr/bin/python3\nimport time\ntime.sleep(60)\n')
        executable.chmod(0o700)
        with patch.object(app, "CODEX", executable):
            job = app.start_job({"prompt": "test"})
            deadline = time.monotonic() + 5
            while job["id"] not in app.PROCESSES and time.monotonic() < deadline:
                time.sleep(0.05)
            self.request("/api/stop", {"id": job["id"]})
            stopped = self.wait_job(job["id"], 10)
            self.assertEqual(stopped["status"], "cancelled")


class PlatformTests(unittest.TestCase):
    def __init__(self, methodName="runTest"):
        super().__init__(methodName)
        import delivery
        import install_codex
        self.delivery = delivery
        self.installer = install_codex
        self.root = TEST_ROOT

    def setUp(self):
        TEST_ROOT.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_supported_platform_mapping(self):
        for system, machine, expected in [
            ("Darwin", "arm64", "darwin-arm64"), ("Darwin", "x86_64", "darwin-x64"),
            ("Linux", "aarch64", "linux-arm64"), ("Linux", "x86_64", "linux-x64"),
            ("Windows", "AMD64", "win32-x64"), ("Windows", "ARM64", "win32-arm64"),
        ]:
            with self.subTest(expected=expected), \
                 patch.object(self.installer.platform, "system", return_value=system), \
                 patch.object(self.installer.platform, "machine", return_value=machine):
                self.assertEqual(self.installer.detect_platform(), expected)
        with patch.object(self.installer.platform, "system", return_value="Other"):
            with self.assertRaises(SystemExit):
                self.installer.detect_platform()

    def install_mock(self, tag="win32-x64", failure=False, unsafe=False):
        import base64
        import hashlib
        import io
        import tarfile
        from unittest.mock import MagicMock
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            member = tarfile.TarInfo("../escape" if unsafe else
                                     "package/vendor/x86_64-pc-windows-msvc/bin/codex.exe")
            content = b"mock executable"
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
        archive = buffer.getvalue()
        metadata = {"version": self.installer.VERSION + "-" + tag,
                    "dist": {"tarball": "https://registry.npmjs.org/mock.tgz",
                             "integrity": "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()}}
        real_run = self.installer.subprocess.run
        def run_command(command, **kwargs):
            if command[1:] == ["--version"]:
                if failure:
                    raise OSError("missing runtime")
                return MagicMock(stdout="codex-cli 0.167.0", stderr="", returncode=0)
            return real_run(command, **kwargs)
        with patch.object(self.installer, "ROOT", self.root), \
             patch.object(self.installer, "detect_platform", return_value=tag), \
             patch.object(self.installer.urllib.request, "urlopen", side_effect=[
                 io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(archive)]) as fetch, \
             patch.object(self.installer.subprocess, "run", side_effect=run_command), \
             patch.object(Path, "symlink_to", side_effect=AssertionError("Windows must not require symlinks")):
            self.installer.install()
            self.assertTrue(fetch.call_args_list[0].args[0].endswith(tag))

    def test_windows_install_without_symlink_and_safe_reinstall(self):
        self.install_mock()
        entry = self.root / "bin" / "codex.cmd"
        self.assertIn('"%~dp0..\\vendor\\codex\\', entry.read_text(encoding="utf-8"))
        record = json.loads((self.root / "codex-install.json").read_text(encoding="utf-8"))
        self.assertEqual(record["platform"], "win32-x64")
        executable = app.installed_codex(self.root, windows=True)
        self.assertTrue(executable.is_file())
        self.assertEqual(executable.suffix, ".exe")
        self.install_mock()

    def test_windows_failed_start_preserves_old_install(self):
        self.install_mock()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaises(SystemExit):
            self.install_mock(failure=True)
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_install_blocks_archive_traversal(self):
        with self.assertRaises(SystemExit):
            self.install_mock(unsafe=True)
        self.assertFalse((self.root / "bin").exists())

    def test_windows_launcher_preserves_unknown_file(self):
        entry = self.root / "bin" / "codex.cmd"
        entry.parent.mkdir()
        entry.write_text("user launcher", encoding="utf-8")
        with patch.object(self.installer, "ROOT", self.root), \
             patch.object(self.installer, "detect_platform", return_value="win32-x64"):
            with self.assertRaises(SystemExit):
                self.installer.install()
        self.assertEqual(entry.read_text(encoding="utf-8"), "user launcher")

    def test_windows_old_mac_install_not_reported_as_installed(self):
        (self.root / "codex-install.json").write_text(json.dumps({
            "platform": "darwin-arm64", "executable": "vendor/codex/codex"}), encoding="utf-8")
        self.assertEqual(app.installed_codex(self.root, windows=True), self.root / "bin" / "codex.exe")

    def test_windows_environment_keeps_system_variables_not_secrets(self):
        with patch.object(app, "IS_WINDOWS", True), patch.dict(app.os.environ, {
            "SystemRoot": r"C:\Windows", "USERPROFILE": r"C:\Users\tester",
            "TEMP": r"C:\Temp", "COMSPEC": r"C:\Windows\System32\cmd.exe",
            "PATHEXT": ".COM;.EXE;.BAT;.CMD", "OPENAI_API_KEY": "secret", "OTHER_SECRET": "secret",
        }, clear=True):
            environment = app.process_environment()
            self.assertEqual(environment["SystemRoot"], r"C:\Windows")
            self.assertEqual(environment["HOME"], r"C:\Users\tester")
            self.assertIn("TEMP", environment)
            self.assertIn("PATHEXT", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("OTHER_SECRET", environment)

    def test_windows_process_group_and_tree_stop(self):
        from unittest.mock import MagicMock
        process = MagicMock(pid=123)
        process.poll.return_value = None
        with patch.object(app, "IS_WINDOWS", True), \
             patch.object(app.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            self.assertEqual(app.process_group_options(), {"creationflags": 0x200})
            app.terminate_process(process)
            self.assertEqual(run.call_args.args[0][1:], ["/PID", "123", "/T", "/F"])
        with patch.object(app, "IS_WINDOWS", False):
            self.assertEqual(app.process_group_options(), {"start_new_session": True})

    def test_directory_open_platform_dispatch(self):
        with patch.object(app, "IS_WINDOWS", True), patch.object(app.os, "startfile", create=True) as open_file:
            app.open_directory(self.root)
            open_file.assert_called_once_with(str(self.root))
        with patch.object(app, "IS_WINDOWS", False), patch.object(app.sys, "platform", "linux"), \
             patch.object(app.shutil, "which", return_value="/usr/bin/xdg-open"), \
             patch.dict(app.os.environ, {"DISPLAY": ":0"}), patch.object(app.subprocess, "run") as run:
            app.open_directory(self.root)
            self.assertEqual(run.call_args.args[0], ["/usr/bin/xdg-open", str(self.root)])
        with patch.object(app, "IS_WINDOWS", False), patch.object(app.sys, "platform", "linux"), \
             patch.dict(app.os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "服务器"):
                app.open_directory(self.root)

    def test_quoted_paths_and_foreign_windows_path_error(self):
        self.assertEqual(self.delivery.local_path('"' + str(self.root) + '"'), self.root)
        with patch.object(self.delivery, "IS_WINDOWS", False):
            with self.assertRaisesRegex(ValueError, "WSL"):
                app.validate_delivery_path(r"D:\交付\delivery")

    def test_atomic_json_rewrites_unicode_and_cleans_failed_temp(self):
        path = self.root / "交互记录.json"
        for index in range(3):
            self.delivery.write_json(path, {"text": "用户中文输入", "index": index})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["index"], 2)
        self.assertEqual(len(list(self.root.iterdir())), 1)
        with patch.object(Path, "replace", side_effect=PermissionError("blocked")):
            with self.assertRaises(PermissionError):
                self.delivery.write_json(path, {"index": 4})
        self.assertEqual(len(list(self.root.iterdir())), 1)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["index"], 2)

    def test_case_insensitive_exclusions_and_windows_snapshot(self):
        workspace = self.root / "项目"
        workspace.mkdir()
        (workspace / "source.txt").write_text("源码", encoding="utf-8")
        (workspace / "AUTH.JSON").write_text("private", encoding="utf-8")
        (workspace / "Delivery").mkdir()
        (workspace / "Delivery" / "old.txt").write_text("old", encoding="utf-8")
        with patch.object(self.delivery, "IS_WINDOWS", True):
            result = save_delivery(workspace, self.root / "交付", [], {"base_url": "http://localhost", "model": "test"})
        payload = Path(result["path"]) / "payload"
        self.assertEqual([path.name for path in payload.iterdir()], ["source.txt"])

    def test_stop_without_lsof_uses_cooperative_protocol(self):
        with patch.object(app, "IS_WINDOWS", False), patch.object(app.shutil, "which", return_value=None), \
             patch.object(app, "stop_server_http", return_value="requested") as stop:
            self.assertEqual(app.stop_server(8765), "requested")
            stop.assert_called_once_with(8765)

    def test_http_stop_checks_identity_and_token(self):
        import io
        from unittest.mock import MagicMock
        identity = {"application": "codex-local-studio", "root": str(app.ROOT), "instance": "test-instance"}
        with patch.object(app.HTTP, "open", side_effect=[
            io.BytesIO(json.dumps(identity).encode()), io.BytesIO(b'{"stopping": true}')]) as open_request:
            self.assertIn("已请求", app.stop_server_http(8765))
            request = open_request.call_args.args[0]
            self.assertEqual(request.get_header("X-local-token"), "test-instance")
            self.assertEqual(json.loads(request.data), {"instance": "test-instance"})
        identity["root"] = str(self.root)
        with patch.object(app.HTTP, "open", return_value=io.BytesIO(json.dumps(identity).encode())) as open_request:
            with self.assertRaises(ValueError):
                app.stop_server_http(8765)
            self.assertEqual(open_request.call_count, 1)

    def test_cooperative_shutdown_endpoint_rejects_wrong_instance(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with patch.object(app, "stop_all_jobs") as stop_jobs:
                request = Request("http://127.0.0.1:" + str(server.server_port) + "/api/shutdown",
                                  data=b'{"instance": "wrong"}',
                                  headers={"Content-Type": "application/json", "X-Local-Token": app.TOKEN})
                with self.assertRaises(HTTPError) as denied:
                    urlopen(request, timeout=5)
                self.assertEqual(denied.exception.code, 403)
                stop_jobs.assert_not_called()
                self.assertIn("已请求", app.stop_server_http(server.server_port))
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                stop_jobs.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_pillow_shrinks_without_sips_and_handles_invalid_images(self):
        import base64
        import io
        import provider_bridge
        if provider_bridge.Image is None:
            self.skipTest("Pillow 未安装")
        with provider_bridge.Image.new("RGBA", (2400, 300), (10, 20, 30, 128)) as source:
            buffer = io.BytesIO()
            source.save(buffer, format="PNG")
        url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        with patch.object(provider_bridge, "SIPS", "/nonexistent/sips"), \
             patch.object(provider_bridge, "SHRINK_MIN_BYTES", 0), \
             patch.object(provider_bridge.subprocess, "run") as run:
            smaller = provider_bridge.shrink_image_url(url, {})
            self.assertTrue(smaller.startswith("data:image/jpeg;base64,"))
            self.assertLess(len(smaller), len(url))
            with provider_bridge.Image.open(io.BytesIO(base64.b64decode(smaller.split(",", 1)[1]))) as image:
                self.assertLessEqual(max(image.size), 1568)
                self.assertEqual(image.mode, "RGB")
            invalid = "data:image/png;base64,AAAA"
            self.assertEqual(provider_bridge.shrink_image_url(invalid, {}), invalid)
            run.assert_not_called()


class RemoteTests(unittest.TestCase):
    def __init__(self, methodName="runTest"):
        super().__init__(methodName)
        self.root = TEST_ROOT
        self.config = TEST_ROOT / "ssh_config"

    def setUp(self):
        TEST_ROOT.mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "ssh_config"
        self.config.write_text("Host devbox\n    HostName example.invalid\n    ProxyJump jumpbox\n", encoding="utf-8")

    def test_host_aliases_and_include_without_credentials(self):
        from remote_connection import ssh_hosts
        include = self.root / "included config"
        include.write_text("Host included\nInclude ssh_config\n", encoding="utf-8")
        self.config.write_text('Host *.example !blocked devbox\nInclude "included config"\nHost=second\n'
                               'IdentityFile "ignored secret path\n', encoding="utf-8")
        self.assertEqual(ssh_hosts(self.config), ["devbox", "included", "second"])

    def test_connect_validates_before_starting_ssh(self):
        from remote_connection import RemoteConnection
        remote = RemoteConnection()
        with patch("remote_connection.subprocess.Popen") as popen:
            for host in ["devbox; whoami", "unknown", "-option", "*"]:
                with self.subTest(host=host), self.assertRaises(ValueError):
                    remote.connect(self.config, host)
            popen.assert_not_called()

    def start_files_channel(self, remote, auth=False):
        import shlex
        import sys
        real_popen = subprocess.Popen
        def spawn(command, **kwargs):
            code = shlex.split(command[-1])[-1]
            if auth:
                code = ("import os,subprocess; "
                        "subprocess.run([os.environ['SSH_ASKPASS'],'jumpbox password:'],check=True,stdout=subprocess.DEVNULL);"
                        "subprocess.run([os.environ['SSH_ASKPASS'],'targetbox password:'],check=True,stdout=subprocess.DEVNULL);" + code)
            kwargs["env"] = dict(kwargs.get("env") or os.environ, STUDIO_REMOTE_STATE_DIR=str(self.root / "remote-state"))
            return real_popen([sys.executable, "-u", "-c", code], **kwargs)
        with patch("remote_connection.shutil.which", return_value="/usr/bin/ssh"), \
             patch("remote_connection.subprocess.Popen", side_effect=spawn) as popen:
            if auth:
                remote.start(self.config, "devbox")
                import time
                deadline = time.monotonic() + 3
                while not remote.status()["prompts"] and time.monotonic() < deadline:
                    time.sleep(0.01)
            else:
                remote.start(self.config, "devbox")
                worker = remote.worker
                assert worker is not None
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertTrue(remote.status()["connected"], str(remote.status().get("error")))
            return popen

    def test_files_channel_has_no_remote_web_requirement(self):
        from remote_connection import RemoteConnection
        remote = RemoteConnection()
        self.addCleanup(remote.disconnect)
        popen = self.start_files_channel(remote)
        status = remote.status()
        self.assertTrue(status["connected"])
        self.assertIn("home", status)
        self.assertNotIn("url", status)
        self.assertNotIn("port", status)
        command = popen.call_args.args[0]
        self.assertEqual(command[-2], "devbox")
        self.assertNotIn("-N", command)
        self.assertNotIn("-L", command)
        self.assertIn("python3 -u -c", command[-1])
        self.assertNotIn("shell", popen.call_args.kwargs)
        target = status["target_id"]
        folder = self.root / "remote project 中文"
        folder.mkdir()
        (folder / "hello.txt").write_text("远端内容", encoding="utf-8")
        result = remote.rpc(target, "tree", {"workspace": str(folder)})
        self.assertEqual(result["children"][0]["path"], "hello.txt")
        opened = remote.rpc(target, "file", {"workspace": str(folder), "path": "hello.txt"})
        remote.rpc(target, "save", {"workspace": str(folder), "path": "hello.txt", "text": "服务器修改", "revision": opened["revision"]})
        self.assertEqual((folder / "hello.txt").read_text(encoding="utf-8"), "服务器修改")
        with self.assertRaisesRegex(ValueError, "断开或已切换"):
            remote.rpc("wrong-target", "tree", {"workspace": str(folder)})
        remote.disconnect()
        with self.assertRaises(ValueError):
            remote.rpc(target, "tree", {"workspace": str(folder)})

    def test_failed_remote_python_does_not_leave_connection(self):
        import sys
        from remote_connection import RemoteConnection
        remote = RemoteConnection()
        remote.password_mode = False
        self.addCleanup(remote.disconnect)
        real_popen = subprocess.Popen
        def fail(command, **kwargs):
            return real_popen([sys.executable, "-c", "import sys;sys.stderr.write('python3: not found\\n');sys.exit(127)"], **kwargs)
        with patch("remote_connection.shutil.which", return_value="/usr/bin/ssh"), \
             patch("remote_connection.subprocess.Popen", side_effect=fail):
            with self.assertRaises(ValueError):
                remote.connect(self.config, "devbox")
            self.assertFalse(remote.status()["connected"])
            self.assertTrue(remote.status()["error"])

    def test_remote_api_requires_local_token_and_serves_buttons(self):
        from remote_connection import RemoteConnection
        remote = RemoteConnection()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        try:
            with patch.object(app, "REMOTE", remote), patch.object(remote, "hosts", return_value={
                "hosts": ["devbox"], "ssh_config": str(self.config), "ssh_available": True,
                "error": "", "connection": {"connected": False}}):
                with urlopen(url + "/api/remote/hosts", timeout=5) as response:
                    self.assertEqual(json.load(response)["hosts"], ["devbox"])
                data = json.dumps({"host": "devbox", "port": 8767}).encode()
                request = Request(url + "/api/remote/connect", data=data,
                                  headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as denied:
                    urlopen(request, timeout=5)
                self.assertEqual(denied.exception.code, 403)
                with patch.object(remote, "start", return_value={"connected": True}) as connect:
                    request.add_header("X-Local-Token", app.TOKEN)
                    with urlopen(request, timeout=5) as response:
                        self.assertTrue(json.load(response)["connected"])
                    connect.assert_called_once_with(None, "devbox")
                answer_data = json.dumps({"challenge_id": "challenge", "answer": "synthetic-answer"}).encode()
                answer_request = Request(url + "/api/remote/answer", data=answer_data,
                                         headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as denied:
                    urlopen(answer_request, timeout=5)
                self.assertEqual(denied.exception.code, 403)
                with patch.object(remote, "answer", return_value={"submitted": True}) as answer:
                    answer_request.add_header("X-Local-Token", app.TOKEN)
                    with urlopen(answer_request, timeout=5) as response:
                        value = json.load(response)
                        self.assertTrue(value["submitted"])
                        self.assertNotIn("synthetic-answer", json.dumps(value))
                    answer.assert_called_once_with("challenge", "synthetic-answer")
                fs_data = json.dumps({"target_id": "target", "operation": "tree", "args": {"workspace": "/remote/project"}}).encode()
                fs_request = Request(url + "/api/remote/fs", data=fs_data, headers={"Content-Type": "application/json"})
                with self.assertRaises(HTTPError) as denied:
                    urlopen(fs_request, timeout=5)
                self.assertEqual(denied.exception.code, 403)
                with patch.object(remote, "rpc", return_value={"children": []}) as rpc:
                    fs_request.add_header("X-Local-Token", app.TOKEN)
                    with urlopen(fs_request, timeout=5) as response:
                        self.assertEqual(json.load(response)["children"], [])
                    rpc.assert_called_once_with("target", "tree", {"workspace": "/remote/project"})
                run_data = json.dumps({"target_id": "target", "operation": "run", "args": {
                    "credential": "untrusted-client-key", "data": {"workspace": "/remote/project", "prompt": "task"}}}).encode()
                run_request = Request(url + "/api/remote/fs", data=run_data, headers={
                    "Content-Type": "application/json", "X-Local-Token": app.TOKEN})
                with patch.object(app, "API_KEY", "trusted-local-key"), patch.object(remote, "rpc", return_value={"id": "remote-run"}) as rpc:
                    with urlopen(run_request, timeout=5) as response:
                        raw_response = response.read().decode()
                    self.assertNotIn("trusted-local-key", raw_response)
                    arguments = rpc.call_args.args[2]
                    self.assertEqual(arguments["credential"], "trusted-local-key")
                    self.assertEqual(arguments["data"]["workspace"], "/remote/project")
                with urlopen(url, timeout=5) as response:
                    html = response.read().decode("utf-8")
                    self.assertIn('id="remote-nav"', html)
                    self.assertIn('id="remote-top"', html)
                    self.assertIn('id="remote-form"', html)
                    self.assertIn('id="remote-disconnect"', html)
                    self.assertIn('<textarea id="remote-status"', html)
                    self.assertIn('id="remote-copy-error"', html)
                    self.assertIn('id="remote-copy-diagnostics"', html)
                    self.assertIn('id="copy-content"', html)
                    self.assertIn('id="toast-copy"', html)
                    self.assertIn('popover="manual" hidden', html)
                    self.assertIn('id="remote-install-codex"', html)
                    self.assertIn('id="send-block-reason"', html)
                    self.assertIn('id="remote-install-inline"', html)
                    self.assertIn('id="delivery-set-path"', html)
                    script = (app.STATIC / "app.js").read_text(encoding="utf-8")
                    declared = set(re.findall(r'id="([^\"]+)"', html))
                    referenced = set(re.findall(r"\$\('([^']+)'\)", script))
                    self.assertFalse(referenced - declared, str(referenced - declared))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            remote.disconnect()

    @unittest.skipIf(os.name == "nt", "网页 askpass 目前仅支持 POSIX")
    def test_real_askpass_launcher_two_separate_passwords(self):
        import time
        from ssh_askpass import AskpassBroker
        broker = AskpassBroker(timeout=5)
        self.addCleanup(broker.close)
        launcher = broker.launcher
        for prompt, synthetic in [("jumpuser@jumpbox's password:", "jump-test-answer"),
                                  ("targetuser@targetbox's password:", "target-test-answer")]:
            process = subprocess.Popen([str(launcher), prompt], env=dict(os.environ, **broker.environment()),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 4
                while not broker.prompts() and time.monotonic() < deadline:
                    time.sleep(0.01)
                challenge = broker.prompts()[0]
                self.assertEqual(challenge["prompt"], prompt)
                broker.answer(challenge["id"], synthetic)
                output, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(output.decode().strip(), synthetic)
                self.assertEqual(errors, b"")
                self.assertEqual(broker.prompts(), [])
                with self.assertRaises(ValueError):
                    broker.answer(challenge["id"], synthetic)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        broker.close()
        self.assertFalse(launcher.exists())

    @unittest.skipIf(os.name == "nt", "网页 askpass 目前仅支持 POSIX")
    def test_askpass_rejects_invalid_token_and_unblocks_cancel(self):
        import socket
        import time
        from ssh_askpass import AskpassBroker
        broker = AskpassBroker(timeout=5)
        self.addCleanup(broker.close)
        address = ("127.0.0.1", int(broker.server.server_address[1]))
        with socket.create_connection(address, timeout=2) as client:
            client.sendall(b'{"token":"wrong","prompt":"password:"}\n')
            self.assertEqual(client.recv(1024), b"")
        self.assertEqual(broker.prompts(), [])
        results = []
        thread = threading.Thread(target=lambda: results.append(broker.request("password:")))
        thread.start()
        deadline = time.monotonic() + 2
        while not broker.prompts() and time.monotonic() < deadline:
            time.sleep(0.01)
        broker.close()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [None])
        self.assertEqual(broker.prompts(), [])

    @unittest.skipIf(os.name == "nt", "网页 askpass 目前仅支持 POSIX")
    def test_async_connect_delivers_two_auth_prompts_without_locking(self):
        import time
        from remote_connection import RemoteConnection
        remote = RemoteConnection()
        self.addCleanup(remote.disconnect)
        self.start_files_channel(remote, auth=True)
        for label, answer in [("jumpbox", "jump-test"), ("targetbox", "target-test")]:
            deadline = time.monotonic() + 4
            prompts = []
            while time.monotonic() < deadline:
                prompts = remote.status()["prompts"]
                if prompts and label in prompts[0]["prompt"]:
                    break
                time.sleep(0.01)
            self.assertTrue(prompts)
            self.assertFalse(remote.status()["connected"])
            self.assertEqual(remote.answer(prompts[0]["id"], answer), {"submitted": True})
        worker = remote.worker
        assert worker is not None
        worker.join(timeout=4)
        self.assertTrue(remote.status()["connected"])
        self.assertNotIn("jump-test", json.dumps(remote.status()))

    def test_remote_files_versions_traversal_and_delivery(self):
        import base64
        import remote_files
        folder = self.root / "remote"
        folder.mkdir()
        (folder / "readme.txt").write_text("server text", encoding="utf-8")
        (folder / "AUTH.JSON").write_text("private", encoding="utf-8")
        args = {"workspace": str(folder)}
        tree = remote_files.handle("tree", args)
        self.assertEqual([item["name"] for item in tree["children"]], ["readme.txt"])
        opened = remote_files.handle("file", {**args, "path": "readme.txt"})
        with self.assertRaisesRegex(ValueError, "已变化"):
            remote_files.handle("save", {**args, "path": "readme.txt", "text": "bad", "revision": "stale"})
        remote_files.handle("save", {**args, "path": "readme.txt", "text": "new", "revision": opened["revision"]})
        with self.assertRaises(ValueError):
            remote_files.handle("file", {**args, "path": "../ssh_config"})
        remote_files.handle("mkdir", {**args, "path": "newdir"})
        remote_files.handle("upload", {**args, "path": "newdir/upload.bin", "data": base64.b64encode(b"hello").decode()})
        downloaded = remote_files.handle("download", {**args, "path": "newdir/upload.bin"})
        self.assertEqual(base64.b64decode(downloaded["data"]), b"hello")
        from remote_connection import RemoteConnection
        remote = RemoteConnection()
        self.addCleanup(remote.disconnect)
        self.start_files_channel(remote)
        result = remote.rpc(remote.status()["target_id"], "delivery", {**args, "destination": str(self.root / "server delivery")})
        self.assertEqual(result["execution_backend"], "ssh")
        self.assertEqual(result["runs"], 0)
        self.assertTrue((Path(result["path"]) / "payload/readme.txt").is_file())
        self.assertFalse((Path(result["path"]) / "payload/AUTH.JSON").exists())

    def test_remote_jobs_cannot_use_local_executor(self):
        with self.assertRaisesRegex(ValueError, "本机"):
            app.start_job({"backend": "ssh", "workspace": str(self.root)})
        with self.assertRaisesRegex(ValueError, "本机"):
            app.run_job({"backend": "ssh"}, "not-a-key")

    def execution_fixture(self, real_codex=False):
        import sys
        from remote_connection import RemoteConnection
        root = self.root / "remote-state"
        binary = root / "bin" / "codex"
        binary.parent.mkdir(parents=True)
        if real_codex:
            if not app.CODEX.is_file():
                self.skipTest("本机未安装 Codex，真实 CLI 远端执行测试跳过")
            binary.symlink_to(app.CODEX.resolve())
        else:
            source = """import sys,os,json,time
from pathlib import Path
from urllib.request import Request,urlopen
if '--version' in sys.argv:
 print('codex-cli fixture');raise SystemExit(0)
prompt=sys.stdin.read()
print(json.dumps({'type':'thread.started','thread_id':'fixture-thread'}),flush=True)
if 'WAIT_FOR_STOP' in prompt:
 time.sleep(60)
else:
 options={}
 for i,arg in enumerate(sys.argv):
  if arg=='-c':
   name,value=sys.argv[i+1].split('=',1);options[name]=json.loads(value)
 base=options['model_providers.local_gateway.base_url']
 request=Request(base+'/responses',data=json.dumps({'model':'test-model','input':[{'role':'user','content':prompt}],'stream':False}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['LOCAL_CODEX_API_KEY']})
 with urlopen(request,timeout=10) as response: json.load(response)
 Path('changed-on-server.txt').write_text('remote change',encoding='utf-8')
 print(json.dumps({'type':'item.completed','item':{'id':'cmd1','type':'command_execution','command':'create changed-on-server.txt','aggregated_output':'remote change','exit_code':0,'status':'completed'}}),flush=True)
 print(json.dumps({'type':'item.completed','item':{'id':'msg1','type':'agent_message','text':'[TASK_COMPLETE]'}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}),flush=True)
"""
            binary.write_text("#!" + sys.executable + "\n" + source, encoding="utf-8")
            binary.chmod(0o700)
        provider = ThreadingHTTPServer(("127.0.0.1", 0), MockProvider)
        provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
        provider_thread.start()
        self.addCleanup(provider.server_close)
        self.addCleanup(provider.shutdown)
        remote = RemoteConnection()
        self.addCleanup(remote.disconnect)
        self.start_files_channel(remote)
        workspace = self.root / "服务器项目 中文"
        workspace.mkdir()
        destination = self.root / "选择的delivery目录"
        data = {"workspace": str(workspace), "delivery_path": str(destination),
                "base_url": "http://127.0.0.1:" + str(provider.server_port) + "/v1", "model": "test-model",
                "max_auto_continues": 0, "client_request_id": "remote-submit", "prompt": "执行并检查服务器文件"}
        return remote, workspace, destination, data

    def wait_remote_job(self, remote, run_id, seconds=30):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            result = remote.rpc(remote.status()["target_id"], "job", {"id": run_id, "cursor": 0})
            if result["job"].get("finished_at"):
                return result
            time.sleep(0.05)
        self.fail("远端任务未按时完成")

    def test_remote_instruction_execution_trajectory_and_delivery_end_to_end(self):
        remote, workspace, destination, data = self.execution_fixture()
        target = remote.status()["target_id"]
        state = remote.rpc(target, "execution-state", {})
        self.assertTrue(state["codex_installed"])
        key = "test-remote-upstream-credential"
        job = remote.rpc(target, "run", {"data": data, "credential": key})
        duplicate = remote.rpc(target, "run", {"data": data, "credential": key})
        self.assertEqual(job["id"], duplicate["id"])
        result = self.wait_remote_job(remote, job["id"])
        self.assertEqual(result["job"]["status"], "completed")
        self.assertEqual((workspace / "changed-on-server.txt").read_text(), "remote change")
        records = destination / "agent_runs" / job["id"]
        request = json.loads((records / "request.json").read_text(encoding="utf-8"))
        response = json.loads((records / "response.json").read_text(encoding="utf-8"))
        final = json.loads((records / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(request["workspace"], str(workspace))
        self.assertEqual(final["status"], "completed")
        self.assertTrue(any(event.get("type") == "provider.request" for event in response))
        self.assertTrue(any(event.get("item", {}).get("type") == "command_execution" for event in response))
        self.assertNotIn(key, json.dumps(response))
        self.assertNotIn(key, (self.root / "remote-state/setting.json").read_text(encoding="utf-8"))
        export = remote.rpc(target, "delivery", {"workspace": str(workspace), "destination": str(destination)})
        package = Path(export["path"])
        self.assertEqual(export["runs"], 1)
        self.assertTrue((package / "payload/changed-on-server.txt").is_file())
        self.assertTrue((package / "agent_runs" / job["id"] / "response.json").is_file())
        commands = json.loads((package / "validation_logs/commands.json").read_text())
        self.assertEqual(len(commands["commands"]), 1)
        moved = self.root / "改选保存路径"
        chosen = remote.rpc(target, "delivery-path", {"workspace": str(workspace), "destination": str(moved)})
        self.assertTrue((Path(chosen["trajectory_path"]) / job["id"] / "response.json").is_file())
        remote.disconnect()
        self.start_files_channel(remote)
        restored = remote.rpc(remote.status()["target_id"], "execution-state", {})
        self.assertEqual(restored["jobs"][0]["id"], job["id"])

    def test_remote_stop_timeout_and_disconnect_preserve_final_trajectory(self):
        remote, workspace, destination, data = self.execution_fixture()
        target = remote.status()["target_id"]
        data.update(prompt="WAIT_FOR_STOP", task_timeout_seconds=1, client_request_id="timeout")
        timed = remote.rpc(target, "run", {"data": data, "credential": "test-remote-key"})
        result = self.wait_remote_job(remote, timed["id"])
        self.assertEqual(result["job"]["status"], "timeout")
        self.assertEqual(json.loads((destination / "agent_runs" / timed["id"] / "result.json").read_text())["status"], "timeout")
        data.update(task_timeout_seconds=0, client_request_id="stop")
        stopped = remote.rpc(target, "run", {"data": data, "credential": "test-remote-key"})
        with self.assertRaisesRegex(ValueError, "仍有任务"):
            remote.rpc(target, "delivery", {"workspace": str(workspace), "destination": str(destination)})
        remote.rpc(target, "stop", {"id": stopped["id"]})
        result = self.wait_remote_job(remote, stopped["id"])
        self.assertEqual(result["job"]["status"], "cancelled")
        data.update(client_request_id="disconnect")
        disconnected = remote.rpc(target, "run", {"data": data, "credential": "test-remote-key"})
        remote.disconnect()
        final = json.loads((destination / "agent_runs" / disconnected["id"] / "result.json").read_text())
        self.assertEqual(final["status"], "cancelled")

    def test_remote_real_codex_model_response_and_trajectory(self):
        remote, workspace, destination, data = self.execution_fixture(real_codex=True)
        job = remote.rpc(remote.status()["target_id"], "run", {"data": data, "credential": "test-real-remote-key"})
        result = self.wait_remote_job(remote, job["id"], seconds=60)
        self.assertEqual(result["job"]["status"], "completed", str(result["job"].get("error")))
        self.assertTrue(any(event.get("item", {}).get("type") == "agent_message" for event in result["events"]))
        self.assertTrue((destination / "agent_runs" / job["id"] / "response.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
