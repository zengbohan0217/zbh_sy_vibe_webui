import argparse
import ast
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid
import webbrowser

from delivery import EXCLUDED_DIRS, IS_WINDOWS, excluded_file, is_link, local_path, now, redact, save_delivery, save_run_records, scrub, write_json
from provider_bridge import ResponsesBridge, normalize_roles
from task_groups import assign_task_ids, group_tasks
from run_scheduler import RunScheduler, workspace_conflict
from remote_connection import REMOTE

ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "http://39.96.222.203:3000/v1"
LOCAL = ROOT / ".local"
SETTINGS_PATH = ROOT / "setting.json"
def installed_codex(root=ROOT, windows=IS_WINDOWS):
    if not windows:
        return root / "bin" / "codex"
    try:
        record = json.loads((root / "codex-install.json").read_text(encoding="utf-8"))
        executable = root / record["executable"]
        if (record.get("platform", "").startswith("win32-")
                and executable.suffix.lower() == ".exe"
                and (root / "vendor" / "codex").resolve() in executable.resolve().parents):
            return executable
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return root / "bin" / "codex.exe"


CODEX = installed_codex()
STATIC = ROOT / "web"
LOCK = threading.RLock()
TOKEN = secrets.token_urlsafe(32)
TOKEN_DEFAULTS = {"max_output_tokens": 65536, "model_context_window": 400000,
                  "model_auto_compact_token_limit": 280000}
SETTINGS = {"base_url": DEFAULT_URL, "model": "", "workspace": "", "remember_key": False,
            "delivery_path": "", "developer_role": "system", "api_key_file": "", **TOKEN_DEFAULTS,
            "max_parallel_runs": 3, "task_timeout_seconds": 0, "stream_idle_timeout_seconds": 900,
            "max_auto_continues": 30, "max_context_images": 6}
COMPLETE_MARK = "[TASK_COMPLETE]"
NEED_INPUT_MARK = "[NEED_USER_INPUT]"
MAX_AUTO_RETRY_FAILURES = 3
CONTINUE_RULES = ("\n\n<execution_rules>\n"
                  "- 持续执行直到全部需求实现并验证完毕，不要中途停下来汇报进度或询问是否继续。\n"
                  "- 说明下一步时，必须在同一回复中直接调用工具执行；不要只描述计划就结束回复。\n"
                  "- 每完成一部分就运行构建、类型检查或测试进行验证，失败就修复后再继续。\n"
                  "- 只有全部需求完成且验证通过时，在最终回复最后一行单独输出 " + COMPLETE_MARK + "，并对照需求逐项说明结果。\n"
                  "- 只有缺少必须由用户提供的信息或权限、确实无法继续时，才在最后一行单独输出 " + NEED_INPUT_MARK + "，并说明需要什么。\n"
                  "- 没有输出以上标记时，工作台会自动让你在同一会话继续。\n"
                  "</execution_rules>")
API_KEY = ""
JOBS = {}
PROCESSES = {}
WORKERS = {}
BRIDGES = {}
EXPORTS = set()
SCHEDULER = RunScheduler()
SHUTTING_DOWN = False


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


HTTP = build_opener(NoRedirect)


KEY_WRAPPERS = " \t\r\n\u00a0\u200b\u200c\u200d\u2060\ufeff\u3000\"'`“”‘’「」『』《》"


def clean_key(value, source="API Key"):
    """API Key 会放进 HTTP 头，只能是可见 ASCII；粘贴时常带入的引号、全角空格、零宽字符在首尾自动去掉。"""
    if not isinstance(value, str):
        raise ValueError(source + " 必须是字符串。")
    key = value.strip(KEY_WRAPPERS)
    if len(key) > 4096:
        raise ValueError(source + " 过长。")
    if key.lower().startswith("bearer "):
        key = key[7:].strip(KEY_WRAPPERS)
    bad = [(index + 1, unicodedata.name(char, "U+%04X" % ord(char))) for index, char in enumerate(key)
           if not 33 <= ord(char) <= 126]
    if bad:
        detail = "、".join("第 " + str(position) + " 个字符（" + name + "）" for position, name in bad[:3])
        raise ValueError(source + " 含有不能放进 HTTP 请求头的字符：" + detail
                         + "。请重新从平台复制纯英文数字的 Key，不要带中文引号、空格或换行。")
    return key


def read_key_file(value):
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        raise ValueError("Token 配置文件必须使用绝对路径。")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == "API_KEY" for target in targets):
                    key = ast.literal_eval(node.value)
                    if isinstance(key, str) and key.strip():
                        return clean_key(key, "Token 文件中的 API_KEY")
    except (OSError, SyntaxError, TypeError):
        raise ValueError("无法读取 Token 配置文件的 API_KEY 字符串常量；不会执行该文件。") from None
    except ValueError as error:
        if "API_KEY" in str(error):
            raise
        raise ValueError("无法读取 Token 配置文件的 API_KEY 字符串常量；不会执行该文件。") from None
    raise ValueError("Token 配置文件需要包含非空的 API_KEY 字符串常量。")


def initialize():
    global API_KEY
    LOCAL.mkdir(mode=0o700, exist_ok=True)
    if not IS_WINDOWS:
        LOCAL.chmod(0o700)
    (LOCAL / "codex-home").mkdir(mode=0o700, exist_ok=True)
    path = SETTINGS_PATH
    legacy = LOCAL / "settings.json"
    migrate = not path.exists() and legacy.is_file() and path != legacy
    source = legacy if migrate else path
    if source.is_file():
        stored = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            raise ValueError("配置文件必须是 JSON 对象。")
        SETTINGS.update({k: stored[k] for k in SETTINGS if k in stored and k != "workspace"})
        key = stored.get("api_key", "")
        API_KEY = clean_key(key, "setting.json 中的 api_key") if key else ""
        if API_KEY:
            SETTINGS["remember_key"] = True
        elif SETTINGS.get("api_key_file"):
            API_KEY = read_key_file(SETTINGS["api_key_file"])
            SETTINGS["remember_key"] = False
    if SETTINGS.get("developer_role", "system") not in {"developer", "system"}:
        raise ValueError("消息角色模式必须是 developer 或 system。")
    SETTINGS.update(validate_token_limits(SETTINGS), **validate_runtime_limits(SETTINGS))
    persist_settings()
    if migrate:
        legacy.unlink()
    for path in sorted((LOCAL / "runs").glob("*.json"), key=lambda item: item.stat().st_mtime):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
            if job["status"] in {"queued", "running", "stopping"}:
                job.update(status="interrupted", finished_at=now(), error="本地服务曾退出，任务已中断；记录已保留。")
            if "max_output_tokens" in str(job.get("error", "")):
                job["error_code"] = "max_output_tokens"
            if job.get("auto_continue_pending"):
                job.update(auto_continue_pending=False, completion_state="incomplete",
                           completion_note="本地服务曾退出，自动续跑已中断；可在此任务发送“继续”。")
            JOBS[job["id"]] = job
        except (OSError, ValueError, KeyError):
            continue
    assign_task_ids(list(JOBS.values()))


def validate_url(value):
    value = str(value).strip().rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(char.isspace() for char in value)):
        raise ValueError("请输入不含账号、密码、query 或 fragment 的 HTTP(S) API URL。")
    parsed.port
    return value


def workspace_path(value):
    if not str(value).strip():
        raise ValueError("请先在页面输入项目绝对路径并点击“打开项目”。")
    path = local_path(value, "工作目录")
    if not path.is_absolute():
        raise ValueError("工作目录需要使用绝对路径。")
    path = path.resolve(strict=True)
    if not path.is_dir() or path == Path(path.anchor) or path == Path.home():
        raise ValueError("请选择具体项目目录，不要选择系统根目录或整个用户目录。")
    if path == LOCAL or LOCAL in path.parents:
        raise ValueError("本地凭据与运行记录目录不能作为工作区。")
    return path


def validate_delivery_path(value):
    value = str(value).strip()
    if not value:
        return ""
    path = local_path(value, "delivery 保存目录")
    if not path.is_absolute():
        raise ValueError("delivery 保存目录需要使用绝对路径。")
    path = path.resolve()
    if path == Path(path.anchor) or path == LOCAL or LOCAL in path.parents:
        raise ValueError("不能将 delivery 保存到系统根目录或服务私有目录。")
    if path == SETTINGS_PATH.resolve() or (path.exists() and not path.is_dir()):
        raise ValueError("delivery 保存目标必须是目录。")
    return str(path)


def validate_token_limits(data):
    defaults = TOKEN_DEFAULTS
    limits = {"max_output_tokens": (256, 65536), "model_context_window": (32768, 1000000),
              "model_auto_compact_token_limit": (4096, 1000000)}
    result = {}
    for name, default in defaults.items():
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not limits[name][0] <= value <= limits[name][1]:
            raise ValueError(name + " 必须是 " + str(limits[name][0]) + " 到 " + str(limits[name][1]) + " 的整数。")
        result[name] = value
    if result["model_auto_compact_token_limit"] + result["max_output_tokens"] >= result["model_context_window"] * 0.9:
        raise ValueError("自动总结阈值加输出预算必须低于上下文窗口的 90%，请留出工具与系统消息空间。")
    return result


def persist_job(job, key):
    if job.get("delivery_path"):
        try:
            job["delivery_records_path"] = save_run_records(job["delivery_path"], job, key)
            job.pop("delivery_error", None)
        except (OSError, ValueError) as error:
            job["delivery_error"] = redact(str(error), key)
    write_json(LOCAL / "runs" / (job["id"] + ".json"), job)


def sync_delivery_records(destination, workspace, key):
    count = 0
    for job in JOBS.values():
        if job["workspace"] == workspace and job.get("finished_at", True) and job["status"] not in {"queued", "running", "stopping"}:
            save_run_records(destination, job, key)
            job["delivery_path"] = destination
            job["delivery_records_path"] = str(Path(destination) / "agent_runs" / job["id"])
            job.pop("delivery_error", None)
            write_json(LOCAL / "runs" / (job["id"] + ".json"), job)
            count += 1
    return count


def active_jobs(workspace=None, task_id=None):
    with LOCK:
        return [job for job in JOBS.values() if not job.get("finished_at") and job["status"] in {"queued", "running", "stopping"}
                and (workspace is None or workspace_conflict(job["workspace"], workspace))
                and (task_id is None or job.get("task_id") == task_id)]


def active_job():
    return next(iter(active_jobs()), None)


def validate_runtime_limits(data):
    defaults = {"max_parallel_runs": 3, "task_timeout_seconds": 0, "stream_idle_timeout_seconds": 900,
                "max_auto_continues": 30, "max_context_images": 6}
    ranges = {"max_parallel_runs": (1, 8), "task_timeout_seconds": (0, 86400), "stream_idle_timeout_seconds": (30, 3600),
              "max_auto_continues": (0, 200), "max_context_images": (0, 50)}
    result = {}
    for name, default in defaults.items():
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not ranges[name][0] <= value <= ranges[name][1]:
            raise ValueError(name + " 必须在 " + str(ranges[name]) + " 的整数范围内。")
        result[name] = value
    return result


def persist_settings():
    data = {k: v for k, v in SETTINGS.items() if k != "workspace"}
    data["api_key"] = API_KEY if SETTINGS["remember_key"] else ""
    SETTINGS_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(SETTINGS_PATH, data)


def update_settings(data):
    global API_KEY
    with LOCK:
        base_url = validate_url(data.get("base_url", SETTINGS["base_url"]))
        model = str(data.get("model", SETTINGS["model"])).strip()
        if len(model) > 200 or any(char in model for char in "\r\n\0"):
            raise ValueError("模型名称不合法。")
        key = API_KEY
        key_file = str(data.get("api_key_file", SETTINGS.get("api_key_file", ""))).strip()
        remember_key = bool(data.get("remember_key", SETTINGS["remember_key"]))
        if data.get("clear_key"):
            key, key_file = "", ""
        elif data.get("api_key"):
            key = clean_key(str(data["api_key"]), "输入的 API Key")
            key_file = ""
        elif key_file:
            key = read_key_file(key_file)
            remember_key = False
        if key:
            key = clean_key(key)
        delivery_path = validate_delivery_path(data.get("delivery_path", SETTINGS.get("delivery_path", "")))
        developer_role = data.get("developer_role", SETTINGS.get("developer_role", "system"))
        if developer_role not in {"developer", "system"}:
            raise ValueError("消息角色模式必须是 developer 或 system。")
        token_limits = validate_token_limits({**SETTINGS, **data})
        runtime_limits = validate_runtime_limits({**SETTINGS, **data})
        target_workspace = data.get("workspace", SETTINGS["workspace"])
        if delivery_path and target_workspace:
            sync_delivery_records(delivery_path, str(workspace_path(target_workspace)), key)
        SETTINGS.update(base_url=base_url, model=model, delivery_path=delivery_path, developer_role=developer_role,
                        api_key_file=key_file, remember_key=remember_key, **token_limits, **runtime_limits)
        API_KEY = key
        persist_settings()
        return public_state()


def job_summary(job):
    return {k: job.get(k) for k in ("id", "prompt", "workspace", "model", "base_url", "status", "created_at",
                                   "finished_at", "thread_id", "error", "exit_code", "error_code", "kind", "resume_id",
                                   "summary_id", "delivery_records_path", "delivery_error", "task_id", "parent_job_id", "client_request_id", "queue_reason", "last_event_at", "started_at", "delivery_path", "api_key_file",
                                   "developer_role", "max_output_tokens", "model_context_window", "model_auto_compact_token_limit",
                                   "max_auto_continues", "auto_continue_count", "completion_state", "completion_note",
                                   "max_context_images")}


def public_state():
    with LOCK:
        return {"settings": dict(SETTINGS), "has_key": bool(API_KEY), "csrf_token": TOKEN,
                "settings_path": str(SETTINGS_PATH),
                "codex_installed": CODEX.is_file(), "codex_version": "0.167.0",
                "tasks": group_tasks(list(JOBS.values())),
                "jobs": [job_summary(job) for job in sorted(JOBS.values(), key=lambda job: job["created_at"], reverse=True)]}


def snapshot_provider(data=None):
    with LOCK:
        options, key = {**SETTINGS, **(data or {})}, API_KEY
    if options.get("api_key_file"):
        key = read_key_file(options["api_key_file"])
    options["base_url"] = validate_url(options["base_url"])
    return options, key


def provider_request(path, body=None, provider=None):
    options, key = provider or snapshot_provider()
    base_url = options["base_url"]
    developer_role = options.get("developer_role", "system")
    if body is not None and path.startswith("/responses") and developer_role == "system":
        body = normalize_roles(body)
    if not key:
        raise ValueError("请先输入并保存 API Key。")
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json", "User-Agent": "codex-local-studio/0.1.0"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(base_url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with HTTP.open(request, timeout=45) as response:
            raw = response.read(2 * 1024 * 1024)
        return json.loads(raw)
    except HTTPError as error:
        detail = redact(error.read(4096).decode(errors="replace"), key)
        raise ValueError("接口 HTTP " + str(error.code) + "：" + detail) from None
    except (URLError, TimeoutError, OSError) as error:
        raise ValueError("接口连接失败：" + redact(str(error), key)) from None
    except ValueError:
        raise ValueError("接口没有返回有效 JSON。") from None


def append_event(job, event, key):
    event = scrub(event, key)
    with LOCK:
        job["events"].append(event)
        job["last_event_at"] = now()
        if event.get("type") == "error" and "Reconnecting..." in str(event.get("message", "")):
            event["type"] = "provider.warning"
        if event.get("type") == "thread.started":
            job["thread_id"] = event.get("thread_id")
        if event.get("type") in {"turn.failed", "error"}:
            detail = event.get("error", event.get("message", "Codex 任务失败"))
            job["error"] = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            if "max_output_tokens" in job["error"]:
                job["error_code"] = "max_output_tokens"
                job["error"] = "本轮输出达到服务端 Token 上限，响应被截断。已保留运行记录；可提高输出预算，或总结上下文后继续。不会自动重复执行任务。"
            elif "content_filter" in job["error"]:
                job["error_code"] = "content_filter"
                job["error"] = ("上游网关的内容安全过滤拦截了本次请求，模型没有返回任何内容（reason: content_filter）。"
                                "同一段历史重发通常会被同样拦截，因此不会自动重试。已知触发源（回传的推理记录）已由本地代理自动去除；"
                                "若仍被拦截，可把输入框下方“截图”调为 0 再发送“继续”，或点击“总结上下文”、新建任务用文字描述进度。")
            elif "latin-1" in job["error"] or "ordinal not in range(256)" in job["error"]:
                job["error_code"] = "invalid_key_characters"
                job["error"] = "API Key 含有中文引号、全角空格等非英文字符，无法放进 HTTP 请求头。请在“连接与模型”中重新粘贴纯英文数字的 Key 后保存。"
            elif "413" in job["error"] and "Payload Too Large" in job["error"]:
                job["error_code"] = "request_too_large"
                job["error"] = "会话历史中的截图过多，请求过大（" + job["error"][:160] + "）。新版本会自动缩小并只保留最近的截图；重启服务后发送“继续”，仍失败请先“总结上下文”。"
            elif "prompt is too long" in job["error"]:
                job["error_code"] = "prompt_too_long"
                job["error"] = ("请求超过模型上下文上限（" + job["error"][:160] + "）。若由截图导致，新版本代理已自动处理，"
                                "重启服务后在此任务发送“继续”即可；若仍失败，请点击“总结上下文”后在新线程继续。")
        if job.get("kind") == "summary" and event.get("type") == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                job["context_summary"] = job.get("context_summary", "") + item.get("text", "") + "\n"
        persist_job(job, key)


def codex_command(job):
    command = [str(CODEX), "exec", "--json", "--skip-git-repo-check", "--ignore-user-config", "--color", "never"]
    options = {"model_provider": "local_gateway", "model": job["model"],
               "model_providers.local_gateway.name": "Local Gateway",
               "model_providers.local_gateway.base_url": job["base_url"],
               "model_providers.local_gateway.env_key": "LOCAL_CODEX_API_KEY",
               "model_providers.local_gateway.wire_api": "responses",
               "model_providers.local_gateway.supports_websockets": False,
               "model_providers.local_gateway.stream_idle_timeout_ms": job.get("stream_idle_timeout_seconds", 900) * 1000,
               "model_providers.local_gateway.capabilities.remote_compaction": "unsupported",
               "model_context_window": job.get("model_context_window", TOKEN_DEFAULTS["model_context_window"]),
               "model_auto_compact_token_limit": job.get("model_auto_compact_token_limit", TOKEN_DEFAULTS["model_auto_compact_token_limit"]),
               "compact_prompt": "Summarize the coding session for continuation. Preserve the user goals, constraints, decisions, files changed, exact test results, failures and remaining work. Do not perform tools or invent successes. Clearly distinguish completed from unverified actions.",
               "approval_policy": "never", "sandbox_mode": "read-only" if job.get("kind") == "summary" else "workspace-write",
               "sandbox_workspace_write.network_access": job["allow_network"],
               "shell_environment_policy.exclude": ["LOCAL_CODEX_API_KEY"],
               "web_search": "disabled"}
    for name, value in options.items():
        command.extend(["-c", name + "=" + json.dumps(value, ensure_ascii=False)])
    if job.get("resume_id"):
        command.extend(["resume", job["resume_id"], "-"])
    else:
        command.extend(["-"])
    return command


def process_group_options():
    if IS_WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def terminate_process(process, force=False):
    if process.poll() is not None:
        return
    if IS_WINDOWS:
        system_root = os.environ.get("SystemRoot", os.environ.get("WINDIR", r"C:\Windows"))
        taskkill = str(Path(system_root) / "System32" / "taskkill.exe")
        result = subprocess.run([taskkill, "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True, timeout=10)
        if result.returncode and process.poll() is None:
            raise OSError("无法结束 Windows 任务进程树：" + result.stderr.decode("utf-8", errors="replace"))
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass


def open_directory(target):
    if IS_WINDOWS:
        os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.run(["/usr/bin/open", str(target)], check=True, timeout=10)
    else:
        executable = shutil.which("xdg-open")
        if not executable or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise ValueError("服务器没有可用的桌面文件管理器；文件已保存在服务器路径，可通过 SSH/SFTP 查看。")
        subprocess.run([executable, str(target)], check=True, timeout=10)


def stop_job(job, reason="cancelled"):
    with LOCK:
        if job.get("finished_at") or job["status"] not in {"queued", "running", "stopping"}:
            return
        job.setdefault("stop_reason", reason)
        process = PROCESSES.get(job["id"])
        bridge = BRIDGES.get(job["id"])
        if process:
            job["status"] = "stopping"
    SCHEDULER.notify()
    if bridge:
        bridge.cancel()
    if process:
        terminate_process(process)
        def force_stop():
            with LOCK:
                still_ours = PROCESSES.get(job["id"]) is process
            if still_ours:
                terminate_process(process, force=True)
        timer = threading.Timer(3, force_stop)
        timer.daemon = True
        timer.start()


def last_agent_text(job):
    for event in reversed(job.get("events", [])):
        item = event.get("item", {}) if event.get("type") == "item.completed" else {}
        if isinstance(item, dict) and item.get("type") == "agent_message":
            return str(item.get("text", ""))
    return ""


def transient_failure(job):
    if job.get("stop_reason") or job.get("error_code") in {"max_output_tokens", "task_timeout", "prompt_too_long",
                                                           "request_too_large", "content_filter", "invalid_key_characters"}:
        return False
    if any(event.get("code") == "stream_disconnected" for event in job.get("events", [])[-50:]):
        return True
    text = str(job.get("error") or "").lower()
    return any(word in text for word in ("stream disconnected", "stream_disconnected", "timed out", "timeout",
                                         "connection", "502", "503", "504", "429", "上游"))


def judge_completion(job):
    job["auto_continue_pending"] = False
    if job.get("kind", "task") != "task":
        return
    limit = job.get("max_auto_continues", 0)
    count = job.get("auto_continue_count", 0)
    failures = job.get("auto_failures", 0)
    if job["status"] == "completed":
        text = last_agent_text(job)
        if COMPLETE_MARK in text:
            job.update(completion_state="complete",
                       completion_note="模型已标记全部需求完成并验证（" + COMPLETE_MARK + "）。请对照需求自行验收。")
            return
        if NEED_INPUT_MARK in text:
            job.update(completion_state="needs_input", completion_note="模型需要你补充信息或权限后才能继续，请查看上方回复。")
            return
        job["next_auto_failures"] = 0
        reason = "本轮结束但未输出完成标记"
    elif job["status"] == "failed" and transient_failure(job):
        if failures >= MAX_AUTO_RETRY_FAILURES:
            job.update(completion_state="incomplete",
                       completion_note="上游连续失败 " + str(failures) + " 次，已停止自动续跑；可稍后发送“继续”。")
            return
        job["next_auto_failures"] = failures + 1
        reason = "上游连接中断"
    else:
        return
    if not limit:
        job.update(completion_state="incomplete", completion_note=reason + "；自动续跑已关闭，可发送“继续”。")
    elif not (job.get("thread_id") or job.get("resume_id")):
        job.update(completion_state="incomplete", completion_note=reason + "；没有可继续的会话 ID，请重新提交。")
    elif SHUTTING_DOWN:
        job.update(completion_state="incomplete", completion_note=reason + "；工作台正在退出，未自动继续。")
    elif count >= limit:
        job.update(completion_state="limit_reached",
                   completion_note="已自动续跑 " + str(count) + " 次，达到上限；任务可能仍未完成，可发送“继续”或在设置中调高上限。")
    else:
        job.update(completion_state="continuing", auto_continue_pending=True,
                   completion_note=reason + "，正在同一会话自动继续（第 " + str(count + 1) + "/" + str(limit) + " 次）。")


def launch_continuation(job, key):
    delay = 15 * job.get("next_auto_failures", 0)
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline and not SHUTTING_DOWN:
        time.sleep(0.5)
    count = job.get("auto_continue_count", 0) + 1
    with LOCK:
        newer = any(item.get("task_id") == job["task_id"] and item["id"] != job["id"]
                    and item["created_at"] > job["created_at"] for item in JOBS.values())
        if SHUTTING_DOWN or newer:
            job.update(auto_continue_pending=False, completion_state="incomplete",
                       completion_note="自动续跑已取消：工作台退出或此任务已有新交互。")
            persist_job(job, key)
            return
    prompt = ("自动继续（第 " + str(count) + "/" + str(job["max_auto_continues"]) + " 次）：上一轮结束时没有输出完成标记，任务尚未完成。"
              "请先快速检查项目当前实际状态和剩余工作，不要重复已完成的部分；然后直接调用工具继续实现、构建和测试，不要只描述计划。")
    data = {name: job.get(name) for name in
            ("workspace", "model", "base_url", "developer_role", "delivery_path", "api_key_file",
             "max_output_tokens", "model_context_window", "model_auto_compact_token_limit",
             "max_parallel_runs", "task_timeout_seconds", "stream_idle_timeout_seconds", "max_auto_continues",
             "max_context_images")
            if job.get(name) is not None}
    data.update(prompt=prompt, resume_id=job.get("thread_id") or job.get("resume_id"), task_id=job["task_id"],
                parent_job_id=job["id"], allow_network=job.get("allow_network", False))
    try:
        start_job(data, auto_continue_count=count, auto_failures=job.get("next_auto_failures", 0))
    except Exception as error:
        with LOCK:
            job.update(auto_continue_pending=False, completion_state="incomplete",
                       completion_note="自动续跑未能启动：" + redact(str(error), key) + " 可手动发送“继续”。")
            persist_job(job, key)
    else:
        with LOCK:
            job["auto_continue_pending"] = False
            persist_job(job, key)


def scheduled_job(job, key):
    acquired = False
    try:
        acquired = SCHEDULER.acquire(job, job["max_parallel_runs"], lambda: bool(job.get("stop_reason")))
        if acquired:
            run_job(job, key)
        else:
            with LOCK:
                job.update(status=job.get("stop_reason", "cancelled"), finished_at=now())
                persist_job(job, key)
    except Exception as error:
        with LOCK:
            job.update(status=job.get("stop_reason", "failed"), finished_at=now(), error=redact(str(error), key))
            try:
                persist_job(job, key)
            except OSError:
                pass
    finally:
        if acquired:
            SCHEDULER.release(job["id"])
        with LOCK:
            WORKERS.pop(job["id"], None)
        if job.get("auto_continue_pending"):
            launch_continuation(job, key)


def stop_all_jobs():
    global SHUTTING_DOWN
    with LOCK:
        SHUTTING_DOWN = True
        jobs = active_jobs()
    SCHEDULER.close()
    for job in jobs:
        stop_job(job)


def process_environment():
    names = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM")
    if IS_WINDOWS:
        names += ("SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE",
                  "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
                  "TEMP", "TMP", "SYSTEMDRIVE", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
    environment = {name: os.environ[name] for name in names if name in os.environ}
    if IS_WINDOWS and "USERPROFILE" in environment:
        environment.setdefault("HOME", environment["USERPROFILE"])
    bundled_paths = [CODEX.resolve().parent, CODEX.resolve().parent.parent / "codex-path"]
    environment["PATH"] = os.pathsep.join(str(path) for path in bundled_paths if path.is_dir()) + os.pathsep + environment.get("PATH", os.defpath)
    return environment


def run_job(job, key):
    if job.get("backend", "local") != "local":
        raise ValueError("本机执行器不能执行远端项目任务。")
    process, timeout_timer, stderr_thread, bridge = None, None, None, None
    outcome = "failed"
    try:
        with LOCK:
            if job.get("stop_reason"):
                return
            job.update(status="running", started_at=now(), queue_reason=None)
            persist_job(job, key)
        environment = process_environment()
        invocation = dict(job)
        process_key = key
        bridge = ResponsesBridge(job["base_url"], key, max_output_tokens=job.get("max_output_tokens", TOKEN_DEFAULTS["max_output_tokens"]),
                                 developer_role=job.get("developer_role", "system"),
                                 idle_timeout=job.get("stream_idle_timeout_seconds", 900),
                                 max_images=lambda: job.get("max_context_images", 6),
                                 on_request=lambda event: append_event(job, event, key))
        with LOCK:
            BRIDGES[job["id"]] = bridge
        invocation["base_url"] = bridge.url
        process_key = bridge.token
        environment.update(CODEX_HOME=str(LOCAL / "codex-home"), LOCAL_CODEX_API_KEY=process_key, NO_COLOR="1")
        process = subprocess.Popen(codex_command(invocation), cwd=job["workspace"], env=environment,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace", bufsize=1, **process_group_options())
        with LOCK:
            PROCESSES[job["id"]] = process
        if job.get("stop_reason"):
            stop_job(job, job["stop_reason"])
        timeout_seconds = job.get("task_timeout_seconds", 0)
        if timeout_seconds:
            timeout_timer = threading.Timer(timeout_seconds, stop_job, args=(job, "timeout"))
            timeout_timer.daemon = True
            timeout_timer.start()
        def read_stderr():
            for line in process.stderr:
                append_event(job, {"type": "log", "text": line.rstrip()[:50000]}, key)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()
        prompt = job["prompt"]
        if job.get("summary_id"):
            prompt = "以下是上一段会话的上下文摘要，仅作参考；当前用户需求优先。\n<session_summary>\n" + job["context_summary"] + "\n</session_summary>\n\n当前需求：\n" + prompt
        if job.get("kind", "task") == "task" and job.get("max_auto_continues", 0):
            prompt += CONTINUE_RULES
        process.stdin.write(prompt)
        process.stdin.close()
        for line in process.stdout:
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    event = {"type": "log", "text": line[:50000]}
            except ValueError:
                event = {"type": "log", "text": line.rstrip()[:50000]}
            append_event(job, event, key)
        code = process.wait()
        stderr_thread.join(timeout=3)
        with LOCK:
            job["exit_code"] = code
            outcome = "completed" if code == 0 and not job.get("error") else "failed"
            if code != 0 and not job.get("error") and not job.get("stop_reason"):
                job["error"] = "Codex 退出码 " + str(code) + "，请查看运行日志。"
    except Exception as error:
        with LOCK:
            job["error"] = redact(str(error), key)
    finally:
        if timeout_timer:
            timeout_timer.cancel()
        if process and process.poll() is None:
            try:
                terminate_process(process, force=True)
                process.wait(timeout=5)
            except (OSError, subprocess.SubprocessError) as error:
                outcome = "failed"
                job["cleanup_error"] = redact(str(error), key)
        if stderr_thread:
            stderr_thread.join(timeout=5)
        if process:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        try:
            if bridge:
                bridge.close()
        except Exception as error:
            outcome = "failed"
            job["cleanup_error"] = redact(str(error), key)
        finally:
            with LOCK:
                job["status"] = job.get("stop_reason") or outcome
                if job.get("stop_reason") == "timeout":
                    job.update(error="达到设置的任务执行时间上限；可提高 task_timeout_seconds 或设为0。", error_code="task_timeout")
                if job.get("kind") == "summary" and job["status"] == "completed" and not job.get("context_summary", "").strip():
                    job.update(status="failed", error="模型未返回上下文摘要，原会话已保留。")
                judge_completion(job)
                job["finished_at"] = now()
                PROCESSES.pop(job["id"], None)
                BRIDGES.pop(job["id"], None)
                persist_job(job, key)


def start_job(data, auto_continue_count=0, auto_failures=0):
    if data.get("backend", "local") != "local":
        raise ValueError("远端文件模式不使用本机 Codex 执行，避免误改同名本机目录；远端任务执行器尚未接入。")
    with LOCK:
        if SHUTTING_DOWN:
            raise ValueError("工作台正在退出，不能提交新任务。")
        request_id = data.get("client_request_id")
        if request_id:
            if not isinstance(request_id, str) or len(request_id) > 128:
                raise ValueError("提交请求 ID 不合法。")
            previous = next((job for job in JOBS.values() if job.get("client_request_id") == request_id), None)
            if previous:
                if previous.get("kind", "task") != data.get("kind", "task") or (data.get("kind", "task") != "summary" and previous["prompt"] != redact(str(data.get("prompt", "")).strip(), API_KEY)):
                    raise ValueError("同一提交 ID 不能用于不同需求。")
                return job_summary(previous)
        options = {**SETTINGS, **{name: data[name] for name in
            ("workspace", "model", "base_url", "developer_role", "delivery_path", "api_key_file",
             "max_output_tokens", "model_context_window", "model_auto_compact_token_limit",
             "max_parallel_runs", "task_timeout_seconds", "stream_idle_timeout_seconds", "max_auto_continues",
             "max_context_images")
             if name in data}}
        options["base_url"] = validate_url(options["base_url"])
        options["model"] = str(options["model"]).strip()
        if len(options["model"]) > 200 or any(c in options["model"] for c in "\r\n\0"):
            raise ValueError("模型名称不合法。")
        if options.get("developer_role") not in {"system", "developer"}:
            raise ValueError("消息角色模式不合法。")
        key = read_key_file(options["api_key_file"]) if options.get("api_key_file") else API_KEY
        if not CODEX.is_file():
            raise ValueError("Codex 未安装，请运行 python3 install_codex.py。")
        if not key or not options["model"]:
            raise ValueError("请先保存 API Key 并选择模型。")
        kind = data.get("kind", "task")
        if kind not in {"task", "summary"}:
            raise ValueError("任务类型不合法。")
        prompt = str(data.get("prompt", "")).strip()
        if kind == "summary":
            prompt = "请只总结当前编码会话，禁止运行工具或修改文件。用中文保留用户目标、约束、已做决策、修改过的路径、验证结果、失败原因和下一步。不要虚构完成结果，不输出密钥。控制在约2000字内，方便新会话继续。"
            if not data.get("resume_id"):
                raise ValueError("请先打开有 thread_id 的会话再总结。")
        if not prompt or len(prompt) > 100000:
            raise ValueError("需求不能为空，且最多 100000 字符。")
        workspace = str(workspace_path(options["workspace"]))
        if any(workspace_conflict(workspace, path) for path in EXPORTS):
            raise ValueError("该项目正在生成源码快照，请完成后再提交；其他项目可正常运行。")
        resume_id = data.get("resume_id")
        if resume_id and not any(job.get("thread_id") == resume_id and job["workspace"] == workspace
                                 and job["base_url"] == options["base_url"] for job in JOBS.values()):
            raise ValueError("该会话不属于当前工作目录或接口，请新建会话。")
        summary_id = data.get("summary_id")
        context_summary = ""
        if summary_id:
            summary = JOBS.get(summary_id)
            if (resume_id or not summary or summary.get("kind") != "summary" or summary["status"] != "completed"
                    or summary["workspace"] != workspace or summary["base_url"] != options["base_url"]
                    or not summary.get("context_summary")):
                raise ValueError("该摘要不能用于当前项目或接口，请选择已完成的总结。")
            context_summary = summary["context_summary"]
        task_id = data.get("task_id")
        parent_id = data.get("parent_job_id")
        if task_id is not None and not isinstance(task_id, str):
            raise ValueError("任务 ID 不合法。")
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError("父运行 ID 不合法。")
        parent = JOBS.get(parent_id) if parent_id else None
        if parent_id and not parent:
            raise ValueError("原任务运行不存在。")
        related = JOBS.get(summary_id) if summary_id else None
        if resume_id:
            related = max((item for item in JOBS.values() if item.get("thread_id") == resume_id
                           and item["workspace"] == workspace and item["base_url"] == options["base_url"]),
                          key=lambda item: (item["created_at"], item["id"]))
        if parent and related and parent["task_id"] != related["task_id"]:
            raise ValueError("续聊或摘要不属于所选任务。")
        parent = parent or related
        if task_id:
            members = [item for item in JOBS.values() if item.get("task_id") == task_id
                       and item["workspace"] == workspace and item["base_url"] == options["base_url"]]
            if not members or (parent and parent.get("task_id") != task_id):
                raise ValueError("任务不属于当前项目或接口。")
            parent = parent or max(members, key=lambda item: (item["created_at"], item["id"]))
        if parent and (parent["workspace"] != workspace or parent["base_url"] != options["base_url"]):
            raise ValueError("原任务不属于当前项目或接口。")
        run_id = str(uuid.uuid4())
        task_id = parent["task_id"] if parent else run_id
        if active_jobs(task_id=task_id):
            raise ValueError("此任务已有运行或排队的交互，请在其他任务窗口提交。")
        job = {"id": run_id, "task_id": task_id, "parent_job_id": parent["id"] if parent else None,
               "client_request_id": request_id, "api_key_file": options.get("api_key_file", ""),
               "prompt": redact(prompt, key), "workspace": workspace,
               "model": options["model"], "base_url": options["base_url"], "status": "queued",
               "created_at": now(), "events": [], "resume_id": resume_id,
               "developer_role": options.get("developer_role", "system"),
               "kind": kind, "summary_id": summary_id, "context_summary": context_summary,
               "delivery_path": validate_delivery_path(options.get("delivery_path", "")),
               **validate_token_limits(options), **validate_runtime_limits(options),
               "queue_reason": "等待并发名额或同目录任务结束", 
               "allow_network": bool(data.get("allow_network", False)) if kind == "task" else False,
               "thread_id": None, "exit_code": None,
               "auto_continue_count": auto_continue_count, "auto_failures": auto_failures}
        JOBS[job["id"]] = job
        persist_job(job, key)
        worker = threading.Thread(target=scheduled_job, args=(job, key), daemon=True)
        WORKERS[job["id"]] = worker
        worker.start()
        return job_summary(job)


def safe_file(relative, directory=False, workspace=None):
    root = workspace_path(SETTINGS["workspace"] if workspace is None else workspace)
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("路径越界。")
    current = root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink() or (current.exists() and is_link(current)):
            raise ValueError("网页不读取符号链接或 junction。")
    path = (root / candidate).resolve(strict=True)
    if path != root and root not in path.parents:
        raise ValueError("路径越界。")
    if any(part.lower() in EXCLUDED_DIRS for part in candidate.parts) or excluded_file(path):
        raise ValueError("该路径属于凭据或排除目录，不在网页中展示。")
    key_file = Path(SETTINGS["api_key_file"]).expanduser().resolve() if SETTINGS.get("api_key_file") else None
    if path == LOCAL or LOCAL in path.parents or path == SETTINGS_PATH.resolve() or path == key_file:
        raise ValueError("不能读取服务私有目录或凭据配置文件。")
    if directory and not path.is_dir():
        raise ValueError("不是目录。")
    return path, root


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def local_request(self, mutation=False):
        hosts = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
        host = self.headers.get("Host", "")
        if host not in hosts:
            raise PermissionError("仅允许本地访问。")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + item for item in hosts}:
            raise PermissionError("拒绝跨站请求。")
        if mutation and not secrets.compare_digest(self.headers.get("X-Local-Token", ""), TOKEN):
            raise PermissionError("本地令牌无效，请刷新页面。")

    def send(self, value, status=200, content_type="application/json; charset=utf-8"):
        data = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        try:
            self.local_request()
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/api/state":
                return self.send(public_state())
            if parsed.path == "/api/identity":
                return self.send({"application": "codex-local-studio", "root": str(ROOT), "instance": TOKEN})
            if parsed.path == "/api/remote/hosts":
                return self.send(REMOTE.hosts())
            if parsed.path == "/api/remote/status":
                return self.send(REMOTE.status())
            if parsed.path.startswith("/api/jobs/"):
                job_id = parsed.path.rsplit("/", 1)[-1]
                cursor = max(0, int(query.get("cursor", ["0"])[0]))
                with LOCK:
                    if job_id not in JOBS:
                        raise FileNotFoundError("任务不存在。")
                    job = JOBS[job_id]
                    return self.send({"job": job_summary(job), "events": job["events"][cursor:cursor + 200],
                                      "cursor": min(cursor + 200, len(job["events"])), "total": len(job["events"])})
            if parsed.path in ("/api/tree", "/api/file") and query.get("backend", ["local"])[0] != "local":
                raise ValueError("远端路径只能通过 SSH 文件接口操作，不会读取同名本机路径。")
            if parsed.path == "/api/tree":
                workspace = query.get("workspace", [SETTINGS["workspace"]])[0]
                if not workspace:
                    return self.send({"path": "", "children": []})
                path, root = safe_file(query.get("path", [""])[0], directory=True, workspace=workspace)
                children = []
                for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
                    if is_link(child) or child.name.lower() in EXCLUDED_DIRS or excluded_file(child):
                        continue
                    children.append({"name": child.name, "path": child.relative_to(root).as_posix(), "directory": child.is_dir()})
                    if len(children) >= 500:
                        break
                return self.send({"path": path.relative_to(root).as_posix(), "children": children})
            if parsed.path == "/api/file":
                path, _ = safe_file(query.get("path", [""])[0], workspace=query.get("workspace", [SETTINGS["workspace"]])[0])
                if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                    raise ValueError("只展示 2MiB 以下的文本文件。")
                data = path.read_bytes()
                if b"\0" in data[:8192]:
                    raise ValueError("二进制文件不支持文本预览。")
                return self.send({"text": redact(data.decode("utf-8", errors="replace"), API_KEY)})
            names = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
            if parsed.path not in names:
                raise FileNotFoundError("页面不存在。")
            path = STATIC / names[parsed.path]
            return self.send(path.read_bytes(), content_type=(mimetypes.guess_type(path.name)[0] or "text/plain") + "; charset=utf-8")
        except Exception as error:
            self.fail(error)

    def do_POST(self):
        try:
            self.local_request(mutation=True)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("请求必须是 JSON。")
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            limit = 8 * 1024 * 1024 if path == "/api/remote/fs" else 1024 * 1024
            if length <= 0 or length > limit:
                raise ValueError("请求体大小不合法。")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求体必须是对象。")
            path = urlsplit(self.path).path
            if path == "/api/remote/hosts":
                return self.send(REMOTE.hosts(data.get("ssh_config")))
            if path == "/api/remote/connect":
                return self.send(REMOTE.start(data.get("ssh_config"), data.get("host")))
            if path == "/api/remote/fs":
                args = data.get("args", {})
                if not isinstance(args, dict):
                    raise ValueError("远端参数必须是对象。")
                args = dict(args)
                if data.get("operation") == "run":
                    options = args.get("data", {})
                    if not isinstance(options, dict):
                        raise ValueError("远端任务参数必须是对象。")
                    options = dict(options)
                    source = str(options.get("api_key_file", "")).strip()
                    args["credential"] = read_key_file(source) if source else API_KEY
                    options.pop("api_key_file", None)
                    args["data"] = options
                else:
                    args.pop("credential", None)
                result = REMOTE.rpc(data.get("target_id"), data.get("operation"), args)
                if data.get("operation") == "file" and isinstance(result, dict):
                    original = result.get("text", "")
                    result["text"] = redact(original, API_KEY)
                    if result["text"] != original:
                        result["editable"] = False
                return self.send(result)
            if path == "/api/remote/answer":
                return self.send(REMOTE.answer(data.get("challenge_id"), data.get("answer")))
            if path == "/api/remote/disconnect":
                return self.send(REMOTE.disconnect())
            if data.get("backend", "local") != "local":
                raise ValueError("该接口仅操作本机；远端路径必须使用带 target_id 的文件接口，不会自动回退本机。")
            if path == "/api/settings":
                return self.send(update_settings(data))
            if path == "/api/models":
                response = provider_request("/models", provider=snapshot_provider(data))
                models = sorted({item["id"] for item in response.get("data", [])
                                 if isinstance(item, dict) and isinstance(item.get("id"), str)})
                if not models:
                    raise ValueError("接口未返回模型列表，请手动输入模型 ID。")
                return self.send({"models": models})
            if path == "/api/probe":
                provider = snapshot_provider(data)
                model = provider[0]["model"]
                if not model:
                    raise ValueError("请先选择模型。")
                response = provider_request("/responses", {"model": model, "input": [
                    {"role": "developer", "content": "Reply only OK. Do not call tools."},
                    {"role": "user", "content": "Connection test."}],
                    "tools": [{"type": "function", "name": "connection_check", "description": "Connection check",
                               "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}],
                    "max_output_tokens": 1024, "store": False}, provider=provider)
                if response.get("status") != "completed":
                    raise ValueError("Responses 检测未完成：" + str(response.get("incomplete_details", response.get("error", response.get("status")))))
                if response.get("error") or response.get("object") != "response":
                    raise ValueError("接口未返回 Responses 标准对象：" + redact(str(response)[:1000], API_KEY))
                return self.send({"supported": True, "status": response.get("status"),
                                  "message": "Responses 检测成功；实际编码还需模型支持工具调用。"})
            if path == "/api/workspace":
                with LOCK:
                    workspace = str(workspace_path(data.get("path", "")))
                    delivery = validate_delivery_path(data.get("delivery_path", SETTINGS.get("delivery_path", "")))
                    if delivery:
                        sync_delivery_records(delivery, workspace, API_KEY)
                    if not data.get("window_context"):
                        SETTINGS["workspace"] = workspace
                    result = public_state()
                    result["settings"]["workspace"] = workspace
                return self.send(result)
            if path == "/api/open":
                target = workspace_path(data.get("path", SETTINGS["workspace"]))
                open_directory(target)
                return self.send({"opened": str(target)})
            if path == "/api/shutdown":
                if data.get("instance") != TOKEN:
                    raise PermissionError("工作台实例已变化，未停止服务。")
                stop_all_jobs()
                self.send({"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if path == "/api/run":
                return self.send(start_job(data), status=202)
            if path == "/api/stop":
                with LOCK:
                    job = JOBS.get(data.get("id"))
                    if not job:
                        raise FileNotFoundError("任务不存在。")
                stop_job(job)
                return self.send({"stopping": True})
            if path == "/api/job-images":
                limit = validate_runtime_limits({"max_context_images": data.get("max_context_images")})["max_context_images"]
                with LOCK:
                    job = JOBS.get(data.get("id"))
                    if not job:
                        raise FileNotFoundError("任务不存在。")
                    job["max_context_images"] = limit
                    persist_job(job, API_KEY)
                return self.send(job_summary(job))
            if path == "/api/delivery":
                with LOCK:
                    workspace = workspace_path(data.get("workspace", SETTINGS["workspace"]))
                    if active_jobs(workspace=str(workspace)) or any(workspace_conflict(str(workspace), item) for item in EXPORTS):
                        raise ValueError("该项目仍有任务或快照生成中，请完成后保存；交互记录仍在自动保存。")
                    target = validate_delivery_path(data.get("path") or data.get("delivery_path") or SETTINGS.get("delivery_path", ""))
                    target = target or str(workspace / "delivery")
                    options, key = snapshot_provider(data)
                    jobs = [scrub(job, key) for job in JOBS.values() if job["workspace"] == str(workspace)]
                    excluded = {SETTINGS_PATH.resolve()} | ({Path(options["api_key_file"]).expanduser().resolve()}
                        if options.get("api_key_file") else set())
                    EXPORTS.add(str(workspace))
                try:
                    result = save_delivery(workspace, target, jobs, options, key, excluded_files=excluded)
                    if data.get("remember_path"):
                        with LOCK:
                            SETTINGS["delivery_path"] = target
                            persist_settings()
                finally:
                    with LOCK:
                        EXPORTS.discard(str(workspace))
                return self.send(result)
            raise FileNotFoundError("接口不存在。")
        except Exception as error:
            self.fail(error)

    def fail(self, error):
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            return
        status = 403 if isinstance(error, PermissionError) else 404 if isinstance(error, FileNotFoundError) else 400
        if not isinstance(error, (ValueError, OSError, KeyError)):
            status = 500
        self.send({"error": redact(str(error), API_KEY)}, status=status)


def listening_pids(port):
    result = subprocess.run(["lsof", "-t", "-a", "-iTCP:" + str(port), "-sTCP:LISTEN"],
                            capture_output=True, text=True, timeout=5)
    if result.returncode not in {0, 1} or result.stderr.strip():
        raise ValueError("无法确认端口监听进程，未停止任何程序。")
    return {int(value) for value in result.stdout.split()}


def stop_server_http(port):
    url = "http://127.0.0.1:" + str(port)
    try:
        with HTTP.open(url + "/api/identity", timeout=5) as response:
            identity = json.loads(response.read(65536).decode("utf-8"))
    except URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            return "端口 " + str(port) + " 没有运行中的工作台，无需停止。"
        raise ValueError("无法确认工作台身份，未停止服务：" + str(error)) from error
    if (not isinstance(identity, dict) or identity.get("application") != "codex-local-studio"
            or Path(identity.get("root", "")).resolve() != ROOT
            or not isinstance(identity.get("instance"), str) or not identity["instance"]):
        raise ValueError("端口不属于当前项目的工作台，未停止任何程序。")
    request = Request(url + "/api/shutdown",
                      data=json.dumps({"instance": identity["instance"]}).encode("utf-8"),
                      headers={"Content-Type": "application/json", "X-Local-Token": identity["instance"]})
    with HTTP.open(request, timeout=15) as response:
        result = json.loads(response.read(65536).decode("utf-8"))
    if result.get("stopping") is not True:
        raise ValueError("工作台未确认停止请求。")
    return "已请求工作台停止；正在清理任务并保存运行记录。"


def stop_server(port):
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间。")
    if IS_WINDOWS or not shutil.which("lsof"):
        return stop_server_http(port)
    pids = listening_pids(port)
    if not pids:
        return "端口 " + str(port) + " 没有运行中的工作台，无需停止。"
    if len(pids) != 1:
        raise ValueError("端口有多个监听进程，未停止任何程序。")
    pid = next(iter(pids))
    command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True, timeout=5).strip()
    parts = shlex.split(command)
    cwd_data = subprocess.check_output(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], text=True, timeout=5)
    directories = [Path(line[1:]).resolve() for line in cwd_data.splitlines() if line.startswith("n")]
    allowed_scripts = {"app.py", str(ROOT / "app.py")}
    if (len(parts) < 2 or not Path(parts[0]).name.lower().startswith("python")
            or parts[1] not in allowed_scripts or directories != [ROOT]):
        raise ValueError("端口 " + str(port) + " 不属于当前项目的工作台，拒绝停止 PID " + str(pid) + "。")
    if listening_pids(port) != pids:
        raise ValueError("端口进程已变化，未发送停止信号。")
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while pid in listening_pids(port) and time.monotonic() < deadline:
        time.sleep(0.2)
    if pid in listening_pids(port):
        raise ValueError("已请求停止 PID " + str(pid) + "，但仍在退出中；未强制结束，请稍后检查。")
    return "已停止工作台 PID " + str(pid) + "，端口 " + str(port) + " 已不再由该进程监听。"


def main():
    global SETTINGS_PATH
    parser = argparse.ArgumentParser(description="Codex 本地网页工作台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", help="原地打开的绝对项目目录")
    parser.add_argument("--settings", help="JSON 配置文件路径，默认项目根目录/setting.json")
    parser.add_argument("--delivery-path", help="默认 delivery 保存目录的绝对路径")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--stop", action="store_true", help="停止指定端口上的本项目工作台，不启动服务")
    arguments = parser.parse_args()
    if arguments.stop:
        try:
            print(stop_server(arguments.port), flush=True)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            raise SystemExit(str(error)) from None
        return
    if arguments.settings:
        SETTINGS_PATH = Path(arguments.settings).expanduser().resolve()
    os.umask(0o077)
    initialize()
    if arguments.workspace:
        SETTINGS["workspace"] = str(workspace_path(arguments.workspace))
    if arguments.delivery_path is not None:
        SETTINGS["delivery_path"] = validate_delivery_path(arguments.delivery_path)
    if arguments.workspace or arguments.delivery_path is not None:
        persist_settings()
    if SETTINGS["workspace"] and SETTINGS.get("delivery_path"):
        sync_delivery_records(SETTINGS["delivery_path"], SETTINGS["workspace"], API_KEY)
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), Handler)
    server.daemon_threads = True
    url = "http://127.0.0.1:" + str(server.server_port)
    print("Codex 本地工作台：" + url, flush=True)
    print("工作目录：" + SETTINGS["workspace"], flush=True)
    print("配置文件：" + str(SETTINGS_PATH), flush=True)
    if not arguments.no_open:
        webbrowser.open(url)
    def shutdown(signum, frame):
        stop_all_jobs()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        server.serve_forever()
    finally:
        stop_all_jobs()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            with LOCK:
                workers = list(WORKERS.values())
            if not workers:
                break
            for worker in workers:
                worker.join(timeout=0.1)
        server.server_close()
        REMOTE.disconnect()


if __name__ == "__main__":
    main()
