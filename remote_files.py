"""仅通过 SSH 标准输入输出运行的远端文件操作器，无 HTTP 服务或安装步骤。"""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from delivery import EXCLUDED_DIRS, SECRET_PATTERNS, excluded_file, is_link, redact, save_delivery, write_json

LIMIT = 2 * 1024 * 1024


def absolute(value):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("请输入服务器绝对路径。")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError("请输入服务器绝对路径，不能使用本机 Windows 盘符。")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if part == "..":
            raise ValueError("路径不能包含 ..。")
        current /= part
        if current.is_symlink() or (current.exists() and is_link(current)):
            raise ValueError("不操作符号链接或 junction 路径。")
    return path


def workspace(value):
    path = absolute(value).resolve(strict=True)
    if not path.is_dir() or path in (Path(path.anchor), Path.home().resolve()):
        raise ValueError("请选择具体项目文件夹，不能把系统根目录或整个 home 作为项目。")
    if any(part.lower() in {".ssh", ".aws", ".codex", ".local", ".codebuddy"} for part in path.parts):
        raise ValueError("不能选择凭据或工作台私有目录作为项目。")
    return path


def relative(value):
    if not isinstance(value, str) or "\0" in value or "\\" in value:
        raise ValueError("文件相对路径需使用 / 分隔。")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("文件路径不能越出项目。")
    if any(part.lower() in EXCLUDED_DIRS for part in path.parts) or excluded_file(path):
        raise ValueError("不操作凭据、缓存或工作台私有文件。")
    return path


def safe_path(root, value):
    path = root / relative(value)
    current = root
    for part in relative(value).parts:
        current /= part
        if current.is_symlink() or (current.exists() and is_link(current)):
            raise ValueError("不操作符号链接。")
    return path


def parent_fd(root, name):
    path = relative(name)
    if not path.parts:
        raise ValueError("请选择具体文件名。")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in path.parts[:-1]:
            following = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        return descriptor, path.name
    except Exception:
        os.close(descriptor)
        raise


def read_bytes(root, name):
    descriptor, leaf = parent_fd(root, name)
    try:
        file_fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(file_fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
                raise ValueError("仅支持 2MiB 以下普通文件。")
            data = stream.read(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError("文件超过 2MiB。")
            return data
    finally:
        os.close(descriptor)


def sensitive(data):
    text = data.decode("utf-8", errors="replace")
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def write_file(root, name, data, revision):
    if len(data) > LIMIT or sensitive(data):
        raise ValueError("文件超过 2MiB 或包含疑似秘密，未保存。")
    descriptor, leaf = parent_fd(root, name)
    temporary = None
    try:
        try:
            original = read_bytes(root, name)
        except FileNotFoundError:
            original = None
        actual = hashlib.sha256(original).hexdigest() if original is not None else None
        if revision != actual:
            raise ValueError("服务器文件已变化或已存在，请重新打开再编辑；未覆盖。")
        if original is not None and sensitive(original):
            raise ValueError("文件含疑似秘密，不允许通过脱敏预览覆盖。")
        mode = 0o600 if original is None else os.stat(leaf, dir_fd=descriptor, follow_symlinks=False).st_mode & 0o777
        temporary = ".studio-write-" + os.urandom(12).hex()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=descriptor)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if original is None:
            os.link(temporary, leaf, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
            os.unlink(temporary, dir_fd=descriptor)
        else:
            os.replace(temporary, leaf, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        temporary = None
        return {"path": name, "revision": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    finally:
        if temporary:
            os.unlink(temporary, dir_fd=descriptor)
        os.close(descriptor)


def handle(operation, args):
    from remote_execution import EXECUTION_OPERATIONS, execution, export_delivery, takeover
    if operation == "takeover":
        return takeover()
    if operation in EXECUTION_OPERATIONS:
        return execution(operation, args)
    if operation == "delivery":
        return export_delivery(args)
    if operation == "browse":
        path = absolute(args.get("path") or str(Path.home())).resolve(strict=True)
        if not path.is_dir():
            raise ValueError("服务器路径不是目录。")
        children = [{"name": child.name, "path": str(child), "directory": True}
                    for child in sorted(path.iterdir(), key=lambda child: child.name.lower())
                    if not is_link(child) and child.is_dir() and child.name.lower() not in EXCLUDED_DIRS][:500]
        return {"path": str(path), "parent": str(path.parent), "children": children}
    root = workspace(args.get("workspace") or args.get("path"))
    if operation == "workspace":
        return {"workspace": str(root)}
    if operation == "tree":
        path = safe_path(root, args.get("path", ""))
        if not path.is_dir():
            raise ValueError("服务器路径不是目录。")
        children = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if is_link(child) or child.name.lower() in EXCLUDED_DIRS or excluded_file(child):
                continue
            children.append({"name": child.name, "path": child.relative_to(root).as_posix(), "directory": child.is_dir()})
            if len(children) >= 500:
                break
        return {"path": path.relative_to(root).as_posix(), "children": children}
    if operation in ("file", "download"):
        data = read_bytes(root, args["path"])
        if operation == "download":
            if sensitive(data):
                raise ValueError("文件包含疑似秘密，不允许下载。")
            return {"data": base64.b64encode(data).decode(), "name": Path(args["path"]).name}
        if b"\0" in data[:8192]:
            raise ValueError("二进制文件可下载，不支持文本编辑。")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("非 UTF-8 文件请下载查看，不支持在线编辑。")
        blocked = sensitive(data)
        return {"text": redact(text), "revision": hashlib.sha256(data).hexdigest(), "editable": not blocked}
    if operation in ("save", "upload"):
        if operation == "save":
            if not isinstance(args.get("text"), str):
                raise ValueError("文件内容必须是文本。")
            data = args["text"].encode("utf-8")
        else:
            data = base64.b64decode(args["data"], validate=True)
        return write_file(root, args["path"], data, args.get("revision"))
    if operation == "mkdir":
        descriptor, leaf = parent_fd(root, args["path"])
        try:
            os.mkdir(leaf, mode=0o700, dir_fd=descriptor)
        finally:
            os.close(descriptor)
        return {"path": args["path"]}
    raise ValueError("不支持的远端操作。")


def serve():
    from remote_execution import install_signal_handlers, shutdown
    install_signal_handlers()
    def send(value):
        sys.stdout.write("STUDIO_RPC " + json.dumps(value, ensure_ascii=True) + "\n")
        sys.stdout.flush()
    try:
        send({"ready": 1, "home": str(Path.home()), "python": sys.version.split()[0], "platform": sys.platform})
        while True:
            raw = sys.stdin.buffer.readline(8 * 1024 * 1024 + 1)
            if not raw or len(raw) > 8 * 1024 * 1024:
                break
            request_id = None
            try:
                request = json.loads(raw)
                request_id = request["id"]
                value = handle(request["operation"], request.get("args", {}))
                send({"id": request_id, "result": value})
            except Exception as problem:
                import app
                send({"id": request_id, "error": redact(str(problem), app.API_KEY)})
    finally:
        shutdown()
