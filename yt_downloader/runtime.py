import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


FROZEN = getattr(sys, "frozen", False)
RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
HIDE_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def atomic_json(path: Path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def command(*args):
    prefix = [sys.executable] if FROZEN else [sys.executable, "-m", "yt_downloader"]
    return [*prefix, *map(str, args)]


def external_env():
    env = dict(os.environ)
    if FROZEN:
        env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                       if p and not Path(p).is_relative_to(RESOURCES))
    return env
