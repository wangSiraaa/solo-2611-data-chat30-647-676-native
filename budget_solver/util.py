"""持久化与哈希小工具：原子写 JSON、规范 JSON、UTC 时间戳。"""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone


def canonical_json(obj):
    """排序键、紧凑分隔的规范 JSON 文本，用于哈希与稳定复读。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj):
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_dir(path):
    os.makedirs(path, exist_ok=True)


def write_json_atomic(path, obj):
    """临时文件 + os.replace，保证目录服务崩溃不留半截 JSON。"""
    ensure_dir(os.path.dirname(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
