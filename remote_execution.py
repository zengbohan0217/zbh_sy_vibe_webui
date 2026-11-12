"""SSH 内存执行器：复用任务调度/模型桥/Codex/轨迹落盘，不运行网页服务。"""
import os
from pathlib import Path
import shutil
import signal
import threading
import time

import app
from delivery import redact, save_delivery

EXECUTION_OPERATIONS = {"execution-state", "run", "job", "stop", "job-images", "delivery-path", "install-codex"}
INITIALIZED = False
STATE_LOCK = threading.RLock()
RUNTIME_LOCK = None


def refresh_codex():
    managed = app.ROOT / "bin" / "codex"
    candidates = [managed]
    configured = shutil.which("codex")
    if configured:
        candidates.append(Path(configured))
    candidates.extend([Path.home() / ".local/bin/codex", Path.home() / "bin/codex"])
    app.CODEX = next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), managed)


def initialize():
    global INITIALIZED, RUNTIME_LOCK
    with STATE_LOCK:
        if INITIALIZED:
            return
        import fcntl
        root = app.ROOT
        current = Path(root.anchor)
        for part in root.parts[1:]:
            current /= part
            if current.is_symlink():
                raise ValueError("远端运行状态目录不能经过符号链接。")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)
        descriptor = os.open(root / "runtime.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        RUNTIME_LOCK = os.fdopen(descriptor, "w")
        try:
            fcntl.flock(RUNTIME_LOCK.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            RUNTIME_LOCK.close()
            RUNTIME_LOCK = None
            raise ValueError("该服务器用户已有远端执行连接，请先断开旧连接，避免两个执行器重复运行任务。") from None
        try:
            app.initialize()
            app.API_KEY = ""
            app.SETTINGS.update(remember_key=False, api_key_file="")
            refresh_codex()
            INITIALIZED = True
        except Exception:
            RUNTIME_LOCK.close()
            RUNTIME_LOCK = None
            raise


def public_state():
    initialize()
    refresh_codex()
    value = app.public_state()
    value.pop("csrf_token", None)
    value["execution_backend"] = "ssh"
    value["codex_path"] = str(app.CODEX)
    value["runtime_path"] = str(app.LOCAL)
    value["install"] = dict(INSTALL)
    return value


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat", "rb") as stream:
            return stream.read().rsplit(b") ", 1)[1][:1] not in {b"Z", b"X"}
    except (OSError, IndexError):
        return False


def _executor_pids():
    """找到持有 runtime.lock 的其他本工具执行器进程；只返回命令行确认为本工具的进程。"""
    own = os.getpid()
    pids = set()
    try:
        inode = os.stat(app.ROOT / "runtime.lock").st_ino
        with open("/proc/locks", encoding="utf-8") as stream:
            for line in stream:
                parts = line.split()
                if len(parts) >= 6 and parts[1] == "FLOCK" and parts[5].rsplit(":", 1)[-1] == str(inode):
                    pids.add(int(parts[4]))
    except (OSError, ValueError):
        pass
    if not pids:
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as stream:
                        command = stream.read()
                except OSError:
                    continue
                if b"remote_files" in command and b"worker.serve()" in command:
                    pids.add(int(entry))
    pids.discard(own)
    result = []
    for pid in sorted(pids):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as stream:
                command = stream.read()
        except OSError:
            continue
        if b"remote_files" not in command or b"worker.serve()" not in command:
            raise ValueError(f"持有执行锁的进程 {pid} 不是本工具的执行器，已拒绝结束；请手动检查该进程。")
        result.append(pid)
    return result


def takeover():
    """结束同一服务器用户下遗留的旧执行器，再由当前连接接管。"""
    if INITIALIZED:
        return public_state()
    stopped = []
    for pid in _executor_pids():
        for signum, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
            try:
                os.kill(pid, signum)
            except ProcessLookupError:
                break
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and _alive(pid):
                time.sleep(0.1)
            if not _alive(pid):
                break
        if _alive(pid):
            raise ValueError(f"无法结束旧执行器进程 {pid}，请在服务器上手动 kill {pid}。")
        stopped.append(pid)
    value = public_state()
    value["takeover_stopped"] = stopped
    return value


INSTALL = {"running": False, "error": "", "log": ""}


def _install_worker():
    import install_codex
    lines = []
    def log(text):
        lines.append(str(text))
        INSTALL["log"] = redact("\n".join(lines), app.API_KEY)
    try:
        install_codex.install(log)
        refresh_codex()
        if not app.CODEX.is_file():
            raise ValueError("安装结束但未找到 Codex 可执行文件。")
        INSTALL["error"] = ""
    except SystemExit as problem:
        INSTALL["error"] = redact(str(problem), app.API_KEY)
    except Exception as problem:
        INSTALL["error"] = redact(str(problem) or type(problem).__name__, app.API_KEY)
    finally:
        INSTALL["running"] = False


def set_delivery(workspace, destination, remember=True):
    from remote_files import absolute
    root = app.workspace_path(str(workspace))
    destination = str(absolute(destination or str(root / "delivery")))
    destination = app.validate_delivery_path(destination)
    folder = Path(destination)
    if folder == root:
        raise ValueError("交付保存目录不能等于项目根目录，请选择项目内 delivery 子目录或独立目录。")
    folder.mkdir(parents=True, exist_ok=True)
    import tempfile
    with tempfile.NamedTemporaryFile(dir=folder, prefix=".delivery-write-check-", mode="wb") as stream:
        stream.write(b"writable")
        stream.flush()
    with app.LOCK:
        for job in app.JOBS.values():
            if job["workspace"] == str(root):
                job["delivery_path"] = destination
                app.persist_job(job, app.API_KEY)
                if job.get("delivery_error"):
                    raise ValueError("无法将已有 trajectory 写入所选保存目录：" + job["delivery_error"])
        if remember:
            app.SETTINGS.update(workspace=str(root), delivery_path=destination)
            app.persist_settings()
    return {"delivery_path": destination, "trajectory_path": str(folder / "agent_runs")}


def execution(operation, args):
    initialize()
    if operation == "execution-state":
        return public_state()
    if operation == "install-codex":
        with STATE_LOCK:
            if app.active_jobs():
                raise ValueError("远端仍有任务，不能替换 Codex。")
            refresh_codex()
            if not app.CODEX.is_file() and not INSTALL["running"]:
                INSTALL.update(running=True, error="", log="")
                threading.Thread(target=_install_worker, name="codex-install", daemon=True).start()
            return public_state()
    if operation == "run":
        from remote_files import workspace
        data = dict(args.get("data", {}))
        allowed = {"workspace", "model", "base_url", "developer_role", "delivery_path", "prompt", "kind",
                   "max_output_tokens", "model_context_window", "model_auto_compact_token_limit",
                   "max_parallel_runs", "task_timeout_seconds", "stream_idle_timeout_seconds", "max_auto_continues",
                   "max_context_images", "allow_network", "client_request_id", "resume_id", "summary_id", "task_id", "parent_job_id"}
        data = {key: value for key, value in data.items() if key in allowed}
        root = workspace(data.get("workspace"))
        with app.LOCK:
            key = app.clean_key(args.get("credential", ""))
            if not key:
                raise ValueError("请在本机连接与模型中设置 API Key；远端执行只在内存接收该 Key。")
            app.API_KEY = key
            app.SETTINGS.update(remember_key=False, api_key_file="")
            refresh_codex()
            if not app.CODEX.is_file():
                raise ValueError("服务器未找到 Codex，请点击“安装远端 Codex”；无需启动远端网页。")
            chosen = set_delivery(root, data.get("delivery_path"), remember=False)
            data.update(workspace=str(root), delivery_path=chosen["delivery_path"], api_key_file="", backend="local")
            app.SETTINGS.update({name: data[name] for name in ("base_url", "model", "developer_role") if name in data})
            app.persist_settings()
            value = app.start_job(data)
            value["execution_backend"] = "ssh"
            return value
    if operation == "job":
        with app.LOCK:
            job = app.JOBS.get(args.get("id"))
            if not job:
                raise ValueError("服务器运行记录不存在。")
            cursor = args.get("cursor", 0)
            if type(cursor) is not int or cursor < 0:
                raise ValueError("日志游标必须是非负整数。")
            events, size = [], 0
            for event in job["events"][cursor:cursor + 200]:
                import json
                count = len(json.dumps(event, ensure_ascii=True))
                if size + count > 6 * 1024 * 1024:
                    break
                events.append(event)
                size += count
            if not events and cursor < len(job["events"]):
                raise ValueError("单个运行事件超过显示上限，完整 trajectory 已保存在服务器。")
            return {"job": app.job_summary(job), "events": events,
                    "cursor": min(cursor + len(events), len(job["events"])), "total": len(job["events"])}
    if operation in {"stop", "job-images"}:
        with app.LOCK:
            job = app.JOBS.get(args.get("id"))
            if not job:
                raise ValueError("服务器任务不存在。")
            if operation == "stop":
                app.stop_job(job)
            else:
                value = args.get("max_context_images")
                if type(value) is not int or not 0 <= value <= 50:
                    raise ValueError("截图数量需为 0–50 的整数。")
                job["max_context_images"] = value
                app.persist_job(job, app.API_KEY)
            return app.job_summary(job)
    if operation == "delivery-path":
        from remote_files import workspace
        root = workspace(args.get("workspace"))
        return set_delivery(root, args.get("destination"))
    raise ValueError("不支持的远端执行操作。")


def export_delivery(args):
    from remote_files import workspace
    initialize()
    root = workspace(args.get("workspace"))
    with app.LOCK:
        if app.active_jobs(workspace=str(root)):
            raise ValueError("该服务器项目仍有任务，请先停止或等待完成再创建交付快照；trajectory 正在自动保存。")
        chosen = set_delivery(root, args.get("destination"))
        app.EXPORTS.add(str(root))
        jobs = [dict(job, events=list(job.get("events", []))) for job in app.JOBS.values() if job["workspace"] == str(root)]
        settings, key = dict(app.SETTINGS), app.API_KEY
    try:
        result = save_delivery(root, chosen["delivery_path"], jobs, settings, key, excluded_files={app.SETTINGS_PATH})
        result.update(trajectory_path=chosen["trajectory_path"], runs=len(jobs), execution_backend="ssh")
        return result
    finally:
        with app.LOCK:
            app.EXPORTS.discard(str(root))


def shutdown():
    global INITIALIZED, RUNTIME_LOCK
    if not INITIALIZED:
        return
    app.stop_all_jobs()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        workers = list(app.WORKERS.values())
        if not workers:
            break
        for worker in workers:
            worker.join(timeout=0.1)
    for process in list(app.PROCESSES.values()):
        app.terminate_process(process, force=True)
    app.API_KEY = ""
    if RUNTIME_LOCK is not None:
        RUNTIME_LOCK.close()
        RUNTIME_LOCK = None
    INITIALIZED = False


def install_signal_handlers():
    def stop(signum, frame):
        raise SystemExit(0)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop)
