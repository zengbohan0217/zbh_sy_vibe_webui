"""OpenSSH askpass 到本机网页的单次挑战桥；不记录或持久化认证回答。"""
import hmac
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import socketserver
import sys
import tempfile
import threading
import time


class AskpassBroker:
    def __init__(self, timeout=180):
        if os.name == "nt":
            raise ValueError("原生 Windows 暂不支持网页密码桥；请使用密钥认证，或在 WSL 运行工作台。")
        self.lock = threading.Lock()
        self.pending = {}
        self.closed = threading.Event()
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        broker = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(broker.timeout + 5)
                try:
                    raw = self.rfile.readline(8193)
                    if len(raw) > 8192:
                        return
                    request = json.loads(raw)
                    token = request.get("token")
                    if not isinstance(token, str) or not hmac.compare_digest(token, broker.token):
                        return
                    prompt = request.get("prompt")
                    if not isinstance(prompt, str) or not prompt or len(prompt) > 4096:
                        return
                    answer = broker.request(prompt)
                    self.wfile.write(json.dumps({"answer": answer}, ensure_ascii=False).encode("utf-8") + b"\n")
                except (OSError, ValueError, TypeError):
                    return

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="studio-askpass-")
        self.launcher = Path(self.temporary.name) / "askpass"
        self.launcher.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                                 + shlex.quote(str(Path(__file__).resolve())) + ' "$@"\n', encoding="utf-8")
        self.launcher.chmod(0o700)
        self.thread.start()

    def environment(self):
        return {"SSH_ASKPASS": str(self.launcher), "SSH_ASKPASS_REQUIRE": "force",
                "STUDIO_ASKPASS_PORT": str(self.server.server_address[1]),
                "STUDIO_ASKPASS_TOKEN": self.token,
                "DISPLAY": os.environ.get("DISPLAY") or "studio:0"}

    def request(self, text):
        challenge_id = secrets.token_urlsafe(24)
        entry = {"id": challenge_id, "prompt": text, "event": threading.Event(), "answer": None,
                 "expires": time.monotonic() + self.timeout}
        with self.lock:
            if self.closed.is_set():
                return None
            self.pending[challenge_id] = entry
        try:
            entry["event"].wait(self.timeout)
            with self.lock:
                return entry["answer"]
        finally:
            with self.lock:
                self.pending.pop(challenge_id, None)
                entry["answer"] = None

    def prompts(self):
        with self.lock:
            return [{"id": entry["id"], "prompt": entry["prompt"],
                     "seconds_remaining": max(0, int(entry["expires"] - time.monotonic()))}
                    for entry in self.pending.values() if not entry["event"].is_set()]

    def answer(self, challenge_id, value):
        if not isinstance(value, str) or len(value) > 4096 or any(char in value for char in "\r\n\0"):
            raise ValueError("认证回答必须是无换行的字符串，且长度不超过 4096。")
        with self.lock:
            entry = self.pending.get(challenge_id) if isinstance(challenge_id, str) else None
            if entry is None or entry["event"].is_set() or entry["expires"] <= time.monotonic():
                raise ValueError("认证提示已失效，请等待新的密码提示或重新连接。")
            entry["answer"] = value
            entry["event"].set()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        with self.lock:
            for entry in self.pending.values():
                entry["answer"] = None
                entry["event"].set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temporary.cleanup()
        self.token = ""


def main():
    try:
        request = {"token": os.environ["STUDIO_ASKPASS_TOKEN"], "prompt": " ".join(sys.argv[1:])}
        with socket.create_connection(("127.0.0.1", int(os.environ["STUDIO_ASKPASS_PORT"])), timeout=5) as client:
            client.settimeout(190)
            client.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            with client.makefile("rb") as stream:
                raw = stream.readline(32769)
            if len(raw) > 32768:
                return 1
            answer = json.loads(raw).get("answer")
            if not isinstance(answer, str):
                return 1
            sys.stdout.write(answer + "\n")
            return 0
    except (OSError, ValueError, KeyError, TypeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
