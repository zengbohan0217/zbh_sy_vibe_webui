import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from datetime import datetime, timezone

EXCLUDED_DIRS = {".git", ".local", ".local-test", ".codebuddy", ".codex", "node_modules", "vendor",
                 "__pycache__", ".venv", "venv", ".cache", "dist", "build", "delivery"}
EXCLUDED_NAMES = {".DS_Store", "auth.json", "credentials.json", "id_rsa", "id_ed25519",
                  "setting.json", "settings.json", "setting.json.tmp", "settings.json.tmp", "token_set.py"}
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r'''(?i)["']?(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)["']?\s*[:=]\s*["']([^"'\n]{8,})["']'''),
]


def now():
    return datetime.now(timezone.utc).isoformat()


def redact(text, key=""):
    text = str(text)
    if key:
        text = text.replace(key, "[REDACTED]")
    text = re.sub(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
                  "[REDACTED_PRIVATE_KEY]", text, flags=re.DOTALL)
    text = re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s\"']+", r"\1[REDACTED]", text)
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED_SECRET]", text)
    return text


def scrub(value, key=""):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k.lower() in {"api_key", "authorization", "access_token", "password"}
                    else scrub(v, key)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, key) for v in value]
    return redact(value, key) if isinstance(value, str) else value


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def save_run_records(destination, job, key=""):
    destination = Path(destination).expanduser().resolve()
    run_id = str(job["id"])
    if not run_id or Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("运行记录 ID 不合法。")
    folder = destination / "agent_runs" / run_id
    for path in (destination, destination / "agent_runs", folder):
        if path.is_symlink():
            raise ValueError("交互记录目录不能使用符号链接。")
    safe = scrub(job, key)
    write_json(folder / "request.json", {k: safe.get(k) for k in
        ("id", "prompt", "workspace", "model", "base_url", "created_at", "kind", "resume_id",
         "summary_id", "task_id", "parent_job_id", "client_request_id", "max_output_tokens", "model_context_window",
         "model_auto_compact_token_limit", "task_timeout_seconds", "stream_idle_timeout_seconds", "max_parallel_runs")})
    write_json(folder / "response.json", safe.get("events", []))
    write_json(folder / "result.json", {k: safe.get(k) for k in
        ("status", "exit_code", "started_at", "finished_at", "last_event_at", "thread_id", "error", "error_code",
         "stop_reason", "queue_reason", "cleanup_error", "events_truncated")})
    if safe.get("context_summary"):
        write_json(folder / "context_summary.json", {"text": safe["context_summary"],
                                                    "source_thread": safe.get("resume_id"), "run_id": run_id})
    return str(folder)


def excluded_file(path):
    name = path.name.lower()
    return (path.name in EXCLUDED_NAMES or name == ".env" or name.startswith(".env.")
            or name.endswith((".pem", ".key", ".p12", ".pfx", ".pyc")))


def iter_payload(workspace, excluded_roots=()):
    workspace = Path(workspace).resolve()
    for current, directories, filenames in os.walk(workspace, followlinks=False):
        base = Path(current)
        directories[:] = sorted(name for name in directories
                                if name not in EXCLUDED_DIRS and not (base / name).is_symlink()
                                and (base / name).resolve() not in excluded_roots)
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink() or excluded_file(path) or not path.is_file():
                continue
            yield path


class ExportBlocked(ValueError):
    pass


def save_delivery(workspace, destination, jobs, settings, key="", excluded_files=()):
    workspace = Path(workspace).resolve(strict=True)
    destination = Path(destination).expanduser()
    if not destination.is_absolute():
        raise ValueError("交付目标需要使用绝对路径。")
    destination = destination.resolve()
    if destination == workspace or destination in workspace.parents:
        raise ValueError("交付目标不能是工作目录本身或它的上级目录。")
    if destination == Path(destination.anchor):
        raise ValueError("不能保存到系统根目录。")
    destination.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=str(destination)))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = destination / (timestamp + "-" + staging.name[-8:])
    files, skipped, findings = [], [], []
    try:
        payload = staging / "payload"
        payload.mkdir()
        for source in iter_payload(workspace, {destination, staging}):
            if source.resolve() in excluded_files:
                continue
            relative = source.relative_to(workspace)
            if source.stat().st_size > 25 * 1024 * 1024:
                skipped.append({"path": str(relative), "reason": "larger_than_25MiB"})
                continue
            data = source.read_bytes()
            is_text = b"\0" not in data[:8192]
            if is_text:
                text = data.decode("utf-8", errors="replace")
                detected = any(pattern.search(text) for pattern in SECRET_PATTERNS)
                if detected or (key and key.encode() in data):
                    findings.append(str(relative))
                    continue
            elif key and key.encode() in data:
                findings.append(str(relative))
                continue
            output = payload / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            output.chmod(source.stat().st_mode & 0o777 & ~0o022)
            files.append({"relative_path": relative.as_posix(), "file_type": source.suffix.lstrip("."),
                          "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "category": "project_payload"})
        if findings:
            raise ExportBlocked("检测到疑似密钥，交付未保存。请移除后重试：" + "、".join(findings[:12]))
        files.sort(key=lambda item: item["relative_path"])
        snapshot = hashlib.sha256("\n".join(item["relative_path"] + "\0" + item["sha256"]
                                           for item in files).encode()).hexdigest()
        selected_jobs = [scrub(job, key) for job in jobs if job["workspace"] == str(workspace)]
        requirements = "\n\n".join(job["prompt"] for job in selected_jobs)
        (staging / "original_requirement.md").write_text(requirements or "尚无任务需求记录。\n", encoding="utf-8")
        write_json(staging / "requirement_register.json", {"requirements": [
            {"run_id": job["id"], "text": job["prompt"], "created_at": job["created_at"]} for job in selected_jobs]})
        write_json(staging / "project_info.json", {"name": workspace.name, "workspace": str(workspace),
                                                  "created_at": now(), "payload_directory": "payload"})
        write_json(staging / "generation.json", {"tool": "openai/codex", "codex_version": "0.167.0",
                                                "base_url": settings["base_url"], "model": settings["model"],
                                                "runs": len(selected_jobs), "snapshot_id": snapshot})
        write_json(staging / "file_manifest.json", {"snapshot_id": snapshot,
            "hash_method": "SHA256 of sorted relative_path + NUL + file_SHA256 joined by LF",
            "scope": "payload/; excluding delivery, credentials, local state, caches, vendors, build outputs and symlinks",
            "excluded_directories": sorted(EXCLUDED_DIRS), "skipped_files": skipped, "files": files})
        dependency_files = {}
        for name in ("package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod"):
            path = payload / name
            if path.is_file():
                dependency_files[name] = path.read_text(encoding="utf-8", errors="replace")[:100000]
        write_json(staging / "dependency_sources.json", {"status": "DECLARED_NOT_AUDITED",
                                                        "manifests": dependency_files})
        commands = []
        run_root = staging / "agent_runs"
        run_root.mkdir()
        for job in selected_jobs:
            save_run_records(staging, job, key)
            for event in job.get("events", []):
                item = event.get("item", {})
                if event.get("type") == "item.completed" and item.get("type") == "command_execution":
                    commands.append({"run_id": job["id"], "command": item.get("command"),
                                     "exit_code": item.get("exit_code"),
                                     "output": item.get("aggregated_output", "")})
        write_json(staging / "validation_logs" / "commands.json", {"commands": commands})
        write_json(staging / "validation.json", {"acceptance_status": "NOT_READY",
            "automated_validation": "NOT_RUN", "reason": "保存快照不代表验收通过；命令执行记录不等于验收测试。",
            "recorded_command_count": len(commands), "failed_runs": [job["id"] for job in selected_jobs
                                                                    if job["status"] != "completed"]})
        write_json(staging / "self_check.json", {"manifest_created": True, "source_file_count": len(files),
                                                "requirements_recorded": bool(selected_jobs),
                                                "acceptance_checked": False})
        write_json(staging / "static_audit.json", {"status": "NOT_RUN", "reason": "未运行代码静态审计。"})
        for name in ("quality_review", "quality_post_review", "quality_iteration"):
            write_json(staging / (name + ".json"), {"status": "NOT_RUN", "reason": "未进行质量评审。"})
        (staging / "revision_baselines").mkdir()
        (staging / "art_rounds").mkdir()
        write_json(staging / "package_secret_scan.json", {"status": "NO_MATCH_IN_SCANNED_SCOPE",
            "checked_at": now(), "scope": "payload text: known key formats and credential assignments; all payload: active API key bytes",
            "limitations": "启发式扫描不能证明不存在秘密；未知格式及二进制嵌入秘密可能无法识别。日志已脱敏。",
            "excluded_secret_files": True, "findings": [], "skipped_files": skipped})
        staging.rename(final)
        return {"path": str(final), "files": len(files), "snapshot_id": snapshot,
                "acceptance_status": "NOT_READY", "skipped_files": len(skipped)}
    except Exception:
        shutil.rmtree(staging)
        raise
