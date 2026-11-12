"""本机工作台经 OpenSSH stdio 操作远端文件，不依赖远端 HTTP 工作台。"""
import atexit
import glob
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from typing import Any

ALIAS = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z")
OPERATIONS = {"browse", "workspace", "tree", "file", "save", "mkdir", "upload", "download", "delivery",
              "execution-state", "run", "job", "stop", "job-images", "delivery-path", "install-codex", "takeover"}
FRAME_LIMIT = 8 * 1024 * 1024


def ssh_config_path(value=None):
    if value is not None and not isinstance(value, (str, Path)):
        raise ValueError("SSH 配置路径必须是字符串。")
    path = Path(value or Path.home() / ".ssh" / "config").expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError("SSH 配置文件需为本机存在的绝对文件路径。")
    return path.resolve()


def ssh_hosts(config):
    config = ssh_config_path(config)
    hosts, visited = set(), set()

    def read(path, depth=0):
        path = Path(path).resolve()
        if path in visited:
            return
        if depth > 8 or len(visited) >= 64:
            raise ValueError("SSH Include 层级或文件数量过多。")
        visited.add(path)
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("SSH 配置文件过大。")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r"(\w+)(?:\s*=\s*|\s+)(.*)", line)
            if not match:
                continue
            keyword, values = match.groups()
            if keyword.lower() not in {"host", "include"}:
                continue
            lexer = shlex.shlex(values, posix=True)
            lexer.escape = ""
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
            if keyword.lower() == "host":
                hosts.update(token for token in tokens if ALIAS.fullmatch(token))
            else:
                for token in tokens:
                    include = Path(token).expanduser()
                    if not include.is_absolute():
                        include = config.parent / include
                    for filename in sorted(glob.glob(str(include))):
                        if Path(filename).is_file():
                            read(filename, depth + 1)

    read(config)
    return sorted(hosts)


def bootstrap():
    loader = """import sys,json,types,os
from pathlib import Path
assert sys.version_info >= (3,9), 'Python 3.9+ required'
p=json.loads(sys.stdin.buffer.readline())
root=Path(os.environ.get('STUDIO_REMOTE_STATE_DIR', str(Path.home()/'.codex-local-studio-remote')))
if not root.is_absolute(): raise ValueError('remote state directory must be absolute')
stub=types.ModuleType('remote_connection')
stub.REMOTE=None
sys.modules['remote_connection']=stub
for name in ('delivery','task_groups','run_scheduler','provider_bridge','install_codex','app','remote_files','remote_execution'):
 m=types.ModuleType(name);m.__file__=str(root/(name+'.py'));sys.modules[name]=m
 exec(compile(p[name],m.__file__,'exec'),m.__dict__)
app=sys.modules['app'];app.ROOT=root;app.LOCAL=root/'.local';app.SETTINGS_PATH=root/'setting.json'
sys.modules['install_codex'].ROOT=root
worker=sys.modules['remote_files']
worker.serve()
"""
    names = ("delivery", "task_groups", "run_scheduler", "provider_bridge", "install_codex", "app", "remote_files", "remote_execution")
    package = {name: (Path(__file__).parent / (name + ".py")).read_text(encoding="utf-8") for name in names}
    return "python3 -u -c " + shlex.quote(loader), (json.dumps(package, ensure_ascii=True) + "\n").encode()


class RemoteConnection:
    def __init__(self):
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.process = None
        self.details = {}
        self.last_error = ""
        self.askpass = None
        self.connecting = False
        self.worker = None
        self.cancelled = threading.Event()
        self.password_mode = os.name != "nt"
        self.ready = threading.Event()
        self.pending = {}
        self.logs = []
        self.readers = []

    def _stop(self):
        if self.askpass is not None:
            self.askpass.close()
            self.askpass = None
        process = self.process
        self.process = None
        self.ready.clear()
        for pending in self.pending.values():
            pending.put({"error": "SSH 连接已断开，操作结果可能未知；写入不会自动重试。"})
        self.pending.clear()
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdin is not None:
                process.stdin.close()
        self.details = {}

    def disconnect(self):
        with self.lock:
            self.cancelled.set()
            self._stop()
            self.last_error = ""
            worker = self.worker
            readers = list(self.readers)
        for thread in [worker, *readers]:
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=3)
        return self.status()

    def status(self) -> dict[str, Any]:
        with self.lock:
            alive = self.process is not None and self.process.poll() is None
            if self.process is not None and not alive and not self.connecting:
                self._stop()
                self.last_error = "SSH 连接已退出，不会退回本机文件操作。"
            return {"connected": alive and self.ready.is_set() and not self.connecting,
                    "connecting": self.connecting,
                    "prompts": self.askpass.prompts() if self.askpass is not None else [],
                    **self.details, "error": self.last_error}

    def validate(self, config, host):
        path = ssh_config_path(config)
        if not isinstance(host, str) or not ALIAS.fullmatch(host) or host not in ssh_hosts(path):
            raise ValueError("请选择配置中的具体 SSH Host 别名。")
        executable = shutil.which("ssh")
        if not executable:
            raise ValueError("未找到系统 OpenSSH 客户端。")
        return path, executable

    def start(self, config, host):
        self.validate(config, host)
        with self.lock:
            if self.connecting or self.status()["connected"]:
                raise ValueError("已有连接或正在认证，请先断开。")
            self.cancelled.clear()
            self.connecting = True
            self.ready.clear()
            self.last_error = ""
            self.logs = []

            def run():
                try:
                    self.connect(config, host)
                except Exception as problem:
                    with self.lock:
                        self.last_error = str(problem)
                finally:
                    with self.lock:
                        self.connecting = False

            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()
            return self.status()

    def answer(self, challenge_id, value):
        with self.lock:
            if self.askpass is None or not self.connecting:
                raise ValueError("当前没有等待认证的 SSH 连接。")
            self.askpass.answer(challenge_id, value)
        return {"submitted": True}

    def hosts(self, config=None):
        if config is not None and not isinstance(config, (str, Path)):
            raise ValueError("SSH 配置路径必须是字符串。")
        path = Path(config or Path.home() / ".ssh" / "config").expanduser()
        available = shutil.which("ssh") is not None
        try:
            aliases = ssh_hosts(path)
            error = "" if available else "未找到系统 OpenSSH 客户端。"
        except (OSError, ValueError) as problem:
            aliases, error = [], str(problem)
        return {"ssh_config": str(path), "hosts": aliases, "ssh_available": available,
                "password_supported": self.password_mode, "error": error, "connection": self.status()}

    def connect(self, config, host):
        path, executable = self.validate(config, host)
        with self.lock:
            if self.cancelled.is_set():
                return self.status()
            environment = None
            if self.password_mode:
                from ssh_askpass import AskpassBroker
                self.askpass = AskpassBroker()
                environment = dict(os.environ, **self.askpass.environment())
            remote_command, package = bootstrap()
            command = [executable, "-F", str(path), "-T",
                       "-o", "BatchMode=no" if self.password_mode else "BatchMode=yes",
                       "-o", "StrictHostKeyChecking=ask" if self.password_mode else "StrictHostKeyChecking=yes",
                       "-o", "ConnectTimeout=300", "-o", "ServerAliveInterval=30",
                       "-o", "ServerAliveCountMax=3", "-o", "ControlMaster=no", "-o", "ControlPath=none",
                       "-o", "ForkAfterAuthentication=no", "-o", "RemoteCommand=none", host, remote_command]
            try:
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, env=environment,
                                                **({"start_new_session": True} if os.name != "nt" else {}))
            except Exception:
                self._stop()
                raise
            process = self.process
            self.details = {"host": host, "ssh_config": str(path), "target_id": secrets.token_urlsafe(24)}

            def read_errors():
                try:
                    while True:
                        raw = process.stderr.readline(8192)
                        if not raw:
                            break
                        with self.lock:
                            if self.process is process:
                                self.logs.append(raw.decode("utf-8", errors="replace").strip()[:2000])
                                del self.logs[:-12]
                finally:
                    process.stderr.close()

            def read_frames():
                try:
                    while True:
                        raw = process.stdout.readline(FRAME_LIMIT + 1)
                        if not raw:
                            break
                        if len(raw) > FRAME_LIMIT:
                            raise ValueError("远端响应超过上限。")
                        if not raw.startswith(b"STUDIO_RPC "):
                            continue
                        frame = json.loads(raw[len(b"STUDIO_RPC "):])
                        with self.lock:
                            if self.process is not process:
                                break
                            if frame.get("ready") == 1:
                                if not isinstance(frame.get("home"), str) or frame.get("platform") not in ("linux", "darwin"):
                                    raise ValueError("远端文件模式目前支持 Linux/macOS，需可用的 Python 3.9+。")
                                self.details.update({"home": frame["home"], "python": frame.get("python", ""),
                                                     "capabilities": sorted(OPERATIONS)})
                                self.ready.set()
                            elif frame.get("id") in self.pending:
                                self.pending[frame["id"]].put(frame)
                except Exception as problem:
                    with self.lock:
                        self.last_error = str(problem)
                finally:
                    process.stdout.close()
                    with self.lock:
                        if self.process is process:
                            self.ready.clear()
                            for waiting in self.pending.values():
                                waiting.put({"error": "远端文件通道已关闭，不会改为操作本机。"})

            self.readers = [threading.Thread(target=read_errors, daemon=True), threading.Thread(target=read_frames, daemon=True)]
            for reader in self.readers:
                reader.start()
        try:
            process.stdin.write(package)
            process.stdin.flush()
            deadline = time.monotonic() + (360 if self.password_mode else 30)
            while time.monotonic() < deadline:
                with self.lock:
                    if self.process is not process:
                        return self.status()
                    if self.ready.is_set():
                        self.connecting = False
                        self.last_error = ""
                        return self.status()
                    if process.poll() is not None or self.last_error:
                        raise ValueError(self.last_error or "SSH/远端 Python 启动失败：" + "\n".join(self.logs))
                self.ready.wait(0.1)
            raise ValueError("SSH 认证或远端 Python 启动超时：" + "\n".join(self.logs)
                             + "。本模式无需远端 app.py 或 8767 端口；请检查 SSH 与 python3 是否可用。")
        except Exception as problem:
            if isinstance(problem, (BrokenPipeError, ConnectionError)):
                for reader in self.readers:
                    reader.join(timeout=0.2)
                problem = ValueError("SSH/远端 Python 启动期间通道关闭：" + ("\n".join(self.logs) or str(problem)))
            with self.lock:
                if self.process is process:
                    self._stop()
                    self.details = {"host": host, "ssh_config": str(path)}
                    self.last_error = str(problem)
            raise problem

    def rpc(self, target_id, operation, args):
        if operation not in OPERATIONS or not isinstance(args, dict):
            raise ValueError("不支持的远端操作。")
        with self.lock:
            if not self.status()["connected"] or target_id != self.details.get("target_id"):
                raise ValueError("服务器连接已断开或已切换，请重新连接并打开远端项目；不会操作本机。")
            if len(self.pending) >= 16:
                raise ValueError("远端操作过多，请稍后重试。")
            request_id = secrets.token_hex(16)
            waiting = queue.Queue()
            self.pending[request_id] = waiting
            process = self.process
        try:
            data = (json.dumps({"id": request_id, "operation": operation, "args": args}, ensure_ascii=True) + "\n").encode()
            if len(data) > FRAME_LIMIT:
                raise ValueError("远端请求超过 8MiB。")
            with self.write_lock:
                with self.lock:
                    if self.process is not process:
                        raise ValueError("服务器连接已切换。")
                process.stdin.write(data)
                process.stdin.flush()
            try:
                frame = waiting.get(timeout=110 if operation == "delivery" else 40 if operation == "takeover" else 15)
            except queue.Empty:
                raise ValueError("远端操作等待超时，结果可能未知；请刷新确认，写入不会自动重试。")
            if frame.get("error"):
                raise ValueError(frame["error"])
            return frame["result"]
        finally:
            with self.lock:
                self.pending.pop(request_id, None)


REMOTE = RemoteConnection()
atexit.register(REMOTE.disconnect)
