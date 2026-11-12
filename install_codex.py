import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
VERSION = "0.167.0"


def detect_platform():
    system = platform.system()
    machine = platform.machine().lower()
    systems = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}
    machines = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}
    if system not in systems or machine not in machines:
        raise SystemExit("不支持的平台：" + system + "/" + machine
                         + "。支持 Linux/macOS/Windows 的 x86_64 和 ARM64。")
    return systems[system] + "-" + machines[machine]


def install(log=print):
    platform_tag = detect_platform()
    target = ROOT / "vendor" / "codex"
    bin_dir = ROOT / "bin"
    windows = platform_tag.startswith("win32-")
    entry = bin_dir / ("codex.cmd" if windows else "codex")
    launcher_prefix = "@echo off\nrem codex-local-studio managed launcher\n"
    if windows:
        if entry.is_symlink() or (entry.exists() and (not entry.is_file() or
                not entry.read_text(encoding="utf-8").startswith(launcher_prefix))):
            raise SystemExit("bin/codex.cmd 已存在且不是工作台启动器，保留原文件。")
    elif (entry.exists() or entry.is_symlink()) and not entry.is_symlink():
        raise SystemExit("bin/codex 已存在且非软链接，保留原文件。")
    if target.is_symlink():
        raise SystemExit("vendor/codex 是软链接，保留原目录。")
    log("检测到平台：" + platform_tag)
    url = "https://registry.npmjs.org/@openai/codex/" + VERSION + "-" + platform_tag
    try:
        with urllib.request.urlopen(url, timeout=45) as response:
            metadata = json.load(response)
        dist = metadata["dist"]
        log("下载官方 @openai/codex " + metadata["version"])
        with urllib.request.urlopen(dist["tarball"], timeout=180) as response:
            archive = response.read()
    except OSError as error:
        raise SystemExit("服务器无法访问 npm 官方源 registry.npmjs.org（" + str(error)
                         + "）。请检查服务器外网/代理，或手动把 codex 放到 ~/.local/bin/codex。") from error
    algorithm, expected = dist["integrity"].split("-", 1)
    if algorithm != "sha512" or base64.b64encode(hashlib.sha512(archive).digest()).decode() != expected:
        raise SystemExit("npm 完整性校验失败，未解压。")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".codex-install-", dir=target.parent) as temporary:
        staging = Path(temporary)
        unpacked = staging / "new"
        unpacked.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
            for member in bundle.getmembers():
                path = (unpacked / member.name).resolve()
                if unpacked.resolve() not in path.parents or not (member.isfile() or member.isdir()):
                    raise SystemExit("安装包包含不安全路径或链接。")
            bundle.extractall(unpacked)
        binary_name = "codex.exe" if windows else "codex"
        binaries = list(unpacked.glob("package/vendor/*/bin/" + binary_name))
        if not binaries:
            binaries = list(unpacked.glob("package/vendor/*/codex/" + binary_name))
        if len(binaries) != 1:
            raise SystemExit("未找到唯一 Codex 二进制：请检查安装包布局。")
        executable = binaries[0]
        if not windows:
            executable.chmod(executable.stat().st_mode | 0o755)
        try:
            result = subprocess.run([str(executable), "--version"], check=True,
                                    capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            detail = getattr(error, "stderr", None) or str(error)
            raise SystemExit("Codex 无法在当前系统启动，未替换原安装。请检查系统运行库与架构：\n"
                             + str(detail).strip()) from error
        executable = target / executable.relative_to(unpacked)
        record = {"name": "@openai/codex", "version": VERSION, "platform": platform_tag,
                  "source": "https://github.com/openai/codex", "tarball": dist["tarball"],
                  "integrity": dist["integrity"], "executable": str(executable.relative_to(ROOT))}
        staged_record = staging / "codex-install.json"
        staged_record.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        bin_dir.mkdir(exist_ok=True)
        old_link = os.readlink(entry) if entry.is_symlink() else None
        old_launcher = entry.read_bytes() if windows and entry.is_file() else None
        previous = staging / "previous"
        if target.exists():
            target.rename(previous)
        try:
            unpacked.rename(target)
            if windows:
                relative = os.path.relpath(executable, bin_dir).replace("/", "\\")
                launcher = launcher_prefix + '"%~dp0' + relative + '" %*\n'
                staged_launcher = staging / "codex.cmd"
                staged_launcher.write_text(launcher, encoding="utf-8")
                staged_launcher.replace(entry)
            else:
                if entry.is_symlink():
                    entry.unlink()
                entry.symlink_to(os.path.relpath(executable, bin_dir))
            staged_record.replace(ROOT / "codex-install.json")
        except OSError:
            if windows:
                if old_launcher is not None:
                    entry.write_bytes(old_launcher)
                elif entry.exists():
                    entry.unlink()
            else:
                if entry.is_symlink():
                    entry.unlink()
                if old_link is not None:
                    entry.symlink_to(old_link)
            if target.exists():
                target.rename(staging / "failed")
            if previous.exists():
                previous.rename(target)
            raise
        log(result.stdout.strip())


if __name__ == "__main__":
    install()
