import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import queue
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


def normalize_roles(body: dict[str, Any]) -> dict[str, Any]:
    result = dict(body)
    if isinstance(body.get("input"), list):
        result["input"] = [dict(item, role="system") if isinstance(item, dict) and item.get("role") == "developer"
                           else item for item in body["input"]]
    return result


def strip_unsigned_reasoning(body: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """网关不返回 encrypted_content，这类推理记录模型无法复用；实测回传后会触发网关 content_filter，转发前去掉。"""
    items = body.get("input")
    if not isinstance(items, list):
        return body, 0
    kept = [item for item in items if not (isinstance(item, dict) and item.get("type") == "reasoning"
                                           and not item.get("encrypted_content"))]
    removed = len(items) - len(kept)
    return (dict(body, input=kept) if removed else body), removed


def is_image(part):
    return isinstance(part, dict) and part.get("type") == "input_image"


MAX_INCOMING_BYTES = 512 * 1024 * 1024
SIPS = "/usr/bin/sips"
SHRINK_MIN_BYTES = 300 * 1024


def shrink_image_url(url, cache, max_edge=1568, quality=82):
    """用 macOS 自带 sips 将大截图缩到长边 max_edge 的 JPEG；失败或未变小时保留原图。"""
    if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url or len(url) < SHRINK_MIN_BYTES:
        return url
    digest = hashlib.sha256(url.encode()).hexdigest()
    if digest in cache:
        return cache[digest]
    result = url
    if os.path.exists(SIPS):
        try:
            raw = base64.b64decode(url.split(",", 1)[1], validate=False)
            with tempfile.TemporaryDirectory(prefix="codex-image-") as directory:
                source, target = os.path.join(directory, "source"), os.path.join(directory, "target.jpg")
                with open(source, "wb") as handle:
                    handle.write(raw)
                done = subprocess.run([SIPS, "-Z", str(max_edge), "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
                                       source, "--out", target], capture_output=True, timeout=30)
                if done.returncode == 0 and os.path.isfile(target):
                    with open(target, "rb") as handle:
                        candidate = "data:image/jpeg;base64," + base64.b64encode(handle.read()).decode()
                    if len(candidate) < len(url):
                        result = candidate
        except (OSError, ValueError, subprocess.SubprocessError):
            result = url
    if len(cache) > 256:
        cache.clear()
    cache[digest] = result
    return result


def shrink_images(body: dict[str, Any], cache) -> tuple[dict[str, Any], dict[str, int]]:
    stats = {"shrunk_images": 0, "image_bytes_saved": 0}
    items = body.get("input")
    if not isinstance(items, list):
        return body, stats
    changed = False
    result = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("content"), list) and any(is_image(p) for p in item["content"]):
            content = []
            for part in item["content"]:
                if is_image(part):
                    smaller = shrink_image_url(part.get("image_url"), cache)
                    if smaller is not part.get("image_url"):
                        stats["shrunk_images"] += 1
                        stats["image_bytes_saved"] += len(part["image_url"]) - len(smaller)
                        part = dict(part, image_url=smaller)
                        changed = True
                content.append(part)
            item = dict(item, content=content)
        result.append(item)
    return (dict(body, input=result) if changed else body), stats


def relocate_tool_images(body: dict[str, Any], max_images: int = 6) -> tuple[dict[str, Any], dict[str, int]]:
    """网关会把工具结果里的图片 base64 当文本计 Token；把图片移到紧随其后的用户消息中，并只保留最近几张。"""
    stats = {"moved_images": 0, "dropped_images": 0}
    items = body.get("input")
    if not isinstance(items, list):
        return body, stats
    result, pending = [], []
    for item in items + [None]:
        is_output = isinstance(item, dict) and str(item.get("type", "")).endswith("_call_output")
        if pending and not is_output:
            result.append({"type": "message", "role": "user", "content":
                           [{"type": "input_text", "text": "以下图片由上方工具调用返回（call_id：" +
                             ", ".join(sorted({call for call, _ in pending})) + "）："}] + [part for _, part in pending]})
            pending = []
        if item is None:
            break
        if is_output and isinstance(item.get("output"), list) and any(is_image(part) for part in item["output"]):
            texts = []
            for part in item["output"]:
                if is_image(part):
                    pending.append((str(item.get("call_id", "")), part))
                    stats["moved_images"] += 1
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                elif isinstance(part, str):
                    texts.append(part)
            texts.append("[图片已附在随后的用户消息中]")
            item = dict(item, output="\n".join(texts))
        result.append(item)
    seen = 0
    for index in range(len(result) - 1, -1, -1):
        item = result[index]
        if not (isinstance(item, dict) and isinstance(item.get("content"), list)
                and any(is_image(part) for part in item["content"])):
            continue
        content = []
        for part in reversed(item["content"]):
            if is_image(part):
                seen += 1
                if seen > max_images:
                    stats["dropped_images"] += 1
                    part = {"type": "input_text", "text": "[较早的图片已省略以节省上下文；需要时请重新查看]"}
            content.append(part)
        result[index] = dict(item, content=list(reversed(content)))
    if not stats["moved_images"] and not stats["dropped_images"]:
        return body, stats
    return dict(body, input=result), stats


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ResponsesBridge:
    def __init__(self, base_url, api_key, max_output_tokens=65536, developer_role="system", on_request=None,
                 idle_timeout: float = 900, heartbeat_seconds: float = 15, relocate_images=True,
                 max_images: "int | Callable[[], int]" = 6):
        self.relocate_images = relocate_images
        self.max_images = max_images
        self.image_cache = {}
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.max_output_tokens = max_output_tokens
        self.developer_role = developer_role
        self.on_request = on_request
        self.idle_timeout = idle_timeout
        self.heartbeat_seconds = heartbeat_seconds
        self.cancelled = threading.Event()
        self.connections_lock = threading.Lock()
        self.responses = set()
        self.readers = set()
        self.token = secrets.token_urlsafe(32)
        self.opener = build_opener(NoRedirect)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def error(self, status, message):
                data = json.dumps({"error": {"message": message, "type": "local_bridge_error"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)
                self.close_connection = True

            def do_POST(self):
                expected_host = "127.0.0.1:" + str(self.server.server_port)
                if (self.headers.get("Host") != expected_host or self.headers.get("Origin")
                        or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + bridge.token)):
                    return self.error(403, "本地代理认证失败。")
                if self.path not in {"/v1/responses", "/v1/responses/compact"}:
                    return self.error(404, "本地代理只转发 Responses 请求。")
                response_started = False
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > MAX_INCOMING_BYTES:
                        return self.error(413, "Codex 发给本地代理的请求超过 " + str(MAX_INCOMING_BYTES // 1024 // 1024)
                                          + "MiB，请点击“总结上下文”后在新线程继续。")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        return self.error(400, "模型请求必须为 JSON 对象。")
                    if bridge.developer_role == "system":
                        body = normalize_roles(body)
                    image_stats = {"moved_images": 0, "dropped_images": 0, "incoming_bytes": length}
                    body, image_stats["dropped_reasoning"] = strip_unsigned_reasoning(body)
                    if bridge.relocate_images:
                        limit = bridge.max_images() if callable(bridge.max_images) else bridge.max_images
                        body, moved = relocate_tool_images(body, limit)
                        moved["image_limit"] = limit
                        body, shrunk = shrink_images(body, bridge.image_cache)
                        image_stats.update(moved, **shrunk)
                    if self.path == "/v1/responses":
                        body["max_output_tokens"] = bridge.max_output_tokens
                    if bridge.on_request:
                        bridge.on_request({"type": "provider.request", "path": self.path, **image_stats,
                            "max_output_tokens": body.get("max_output_tokens"),
                            "input_items": len(body.get("input", [])) if isinstance(body.get("input"), list) else 1,
                            "request_bytes": len(json.dumps(body, ensure_ascii=False).encode()),
                            "auto_compact_request": "Summarize the coding session for continuation." in json.dumps(body.get("input", ""), ensure_ascii=False)})
                    headers = {"Authorization": "Bearer " + bridge.api_key, "Content-Type": "application/json",
                               "Accept": self.headers.get("Accept", "application/json"),
                               "User-Agent": self.headers.get("User-Agent", "codex-local-studio/0.1.0")}
                    for name in ("OpenAI-Beta", "X-Client-Request-Id"):
                        if self.headers.get(name):
                            headers[name] = self.headers[name]
                    request = Request(bridge.base_url + self.path[len("/v1"):],
                                      data=json.dumps(body, ensure_ascii=False).encode(), headers=headers)
                    try:
                        response = bridge.opener.open(request, timeout=bridge.idle_timeout)
                    except HTTPError as error:
                        response = error
                    with bridge.connections_lock:
                        bridge.responses.add(response)
                    try:
                        with response:
                            content_type = response.headers.get("Content-Type", "application/json")
                            self.send_response(response.code)
                            self.send_header("Content-Type", content_type)
                            self.send_header("Cache-Control", "no-store")
                            self.send_header("Connection", "close")
                            self.end_headers()
                            response_started = True
                            self.close_connection = True
                            if "text/event-stream" in content_type and response.code == 200:
                                bridge.stream_response(response, self.wfile)
                            else:
                                while not bridge.cancelled.is_set():
                                    chunk = response.read1(65536)
                                    if not chunk:
                                        break
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                            bridge.report({"type": "provider.response", "http_status": response.code})
                    finally:
                        with bridge.connections_lock:
                            bridge.responses.discard(response)
                except (BrokenPipeError, ConnectionResetError):
                    bridge.report({"type": "provider.warning", "message": "模型连接或CLI连接已断开。"})
                except (OSError, ValueError) as error:
                    message = "模型代理连接失败：" + str(error).replace(bridge.api_key, "[REDACTED]")
                    bridge.report({"type": "provider.warning", "message": message})
                    if not response_started:
                        self.error(502, message)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:" + str(self.server.server_port) + "/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def report(self, event):
        if self.on_request:
            self.on_request(event)

    def stream_response(self, response, output):
        chunks = queue.Queue(maxsize=16)
        stop = threading.Event()

        def enqueue(value):
            while not stop.is_set() and not self.cancelled.is_set():
                try:
                    chunks.put(value, timeout=0.1)
                    return
                except queue.Full:
                    pass

        def read_stream():
            try:
                while not stop.is_set() and not self.cancelled.is_set():
                    chunk = response.read1(65536)
                    enqueue(chunk)
                    if not chunk:
                        return
            except (OSError, ValueError) as error:
                enqueue(error)
            except AttributeError as error:
                if not stop.is_set() and not self.cancelled.is_set():
                    enqueue(ConnectionError("上游连接已关闭：" + str(error)))

        reader = threading.Thread(target=read_stream, daemon=True)
        with self.connections_lock:
            self.readers.add(reader)
        reader.start()
        pending = b""
        terminal = False
        last_data = time.monotonic()
        try:
            while not self.cancelled.is_set():
                try:
                    chunk = chunks.get(timeout=min(self.heartbeat_seconds, self.idle_timeout))
                except queue.Empty:
                    if time.monotonic() - last_data >= self.idle_timeout:
                        raise TimeoutError("上游模型流在空闲超时内未返回数据。")
                    output.write(b": local-studio heartbeat\n\n")
                    output.flush()
                    continue
                if isinstance(chunk, Exception):
                    raise chunk
                if not chunk:
                    if not terminal:
                        raise ConnectionError("上游SSE在完成事件前结束。")
                    break
                last_data = time.monotonic()
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.startswith(b"data:"):
                        try:
                            event = json.loads(line[5:].strip())
                            if event.get("type") in {"response.completed", "response.incomplete", "response.failed", "error"}:
                                terminal = True
                                result = event.get("response") if isinstance(event.get("response"), dict) else {}
                                items = result.get("output") if isinstance(result.get("output"), list) else []
                                details = result.get("incomplete_details")
                                self.report({"type": "provider.completed", "event": event.get("type"),
                                             "status": result.get("status"),
                                             "incomplete_reason": details.get("reason") if isinstance(details, dict) else None,
                                             "output_types": [str(item.get("type")) for item in items if isinstance(item, dict)][:50]})
                        except (ValueError, AttributeError):
                            pass
                if len(pending) > 2 * 1024 * 1024:
                    raise ValueError("上游SSE单行超过2MiB。")
                output.write(chunk)
                output.flush()
                if terminal:
                    break
            if self.cancelled.is_set():
                return
        except (TimeoutError, ConnectionError, OSError, ValueError) as error:
            if not self.cancelled.is_set():
                message = str(error).replace(self.api_key, "[REDACTED]")
                self.report({"type": "provider.warning", "message": message, "code": "stream_disconnected"})
                event = {"type": "error", "code": "stream_disconnected", "message": message}
                output.write(b"\n\nevent: error\ndata: " + json.dumps(event, ensure_ascii=False).encode() + b"\n\n")
                output.flush()
        finally:
            stop.set()
            self.abort_response(response)
            reader.join(timeout=3)
            with self.connections_lock:
                self.readers.discard(reader)

    @staticmethod
    def abort_response(response):
        try:
            sock = response.fp.raw._sock
            sock.shutdown(socket.SHUT_RDWR)
        except (AttributeError, OSError):
            pass
        try:
            response.close()
        except (OSError, ValueError):
            pass

    def cancel(self):
        self.cancelled.set()
        with self.connections_lock:
            responses = list(self.responses)
        for response in responses:
            self.abort_response(response)

    def close(self):
        self.cancel()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        with self.connections_lock:
            readers = list(self.readers)
        for reader in readers:
            reader.join(timeout=3)
        self.api_key = ""
