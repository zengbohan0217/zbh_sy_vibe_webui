import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent
VERSION = "0.167.0"


def install():
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise SystemExit("此安装入口固定为 macOS Apple Silicon。")
    url = "https://registry.npmjs.org/@openai/codex/" + VERSION + "-darwin-arm64"
    with urllib.request.urlopen(url, timeout=45) as response:
        metadata = json.load(response)
    dist = metadata["dist"]
    print("下载官方 @openai/codex " + metadata["version"], flush=True)
    with urllib.request.urlopen(dist["tarball"], timeout=180) as response:
        archive = response.read()
    algorithm, expected = dist["integrity"].split("-", 1)
    if algorithm != "sha512" or base64.b64encode(hashlib.sha512(archive).digest()).decode() != expected:
        raise SystemExit("npm 完整性校验失败，未解压。")
    target = ROOT / "vendor" / "codex"
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        for member in bundle.getmembers():
            path = (target / member.name).resolve()
            if target.resolve() not in path.parents or not (member.isfile() or member.isdir()):
                raise SystemExit("安装包包含不安全路径或链接。")
        bundle.extractall(target)
    binaries = list(target.glob("package/vendor/*/bin/codex"))
    if not binaries:
        binaries = list(target.glob("package/vendor/*/codex/codex"))
    if len(binaries) != 1:
        raise SystemExit("未找到唯一 Codex 二进制：请检查安装包布局。")
    executable = binaries[0]
    executable.chmod(executable.stat().st_mode | 0o755)
    bin_dir = ROOT / "bin"
    bin_dir.mkdir(exist_ok=True)
    entry = bin_dir / "codex"
    if entry.exists() or entry.is_symlink():
        if not entry.is_symlink():
            raise SystemExit("bin/codex 已存在且非软链接，保留原文件。")
        entry.unlink()
    entry.symlink_to(os.path.relpath(executable, bin_dir))
    record = {"name": "@openai/codex", "version": VERSION, "platform": "darwin-arm64",
              "source": "https://github.com/openai/codex", "tarball": dist["tarball"],
              "integrity": dist["integrity"], "executable": str(executable.relative_to(ROOT))}
    (ROOT / "codex-install.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    subprocess.run([str(entry), "--version"], check=True)


if __name__ == "__main__":
    install()
