"""Exercise the public self-updater in a disposable Windows CI profile."""

from contextlib import closing
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yt_downloader.model import Entry, Options
from yt_downloader.store import Store


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def update_history(path):
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='updates'").fetchone():
            return []
        return db.execute("""
            SELECT version, status, detail FROM updates WHERE component='aplikacja' ORDER BY rowid
        """).fetchall()


def verify(previous, expected):
    for tag in (previous, expected):
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            raise ValueError(f"Unexpected release tag {tag!r}")

    scratch = Path(os.environ["RUNNER_TEMP"]) / "YT Downloader upgrade test"
    scratch.mkdir()
    target = scratch / "installation"
    data = scratch / "local data"
    data.mkdir()
    base = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/releases/download/{previous}"
    name = "YTDownloader-Setup.exe"
    installer = scratch / name
    with urlopen(f"{base}/{name}.sha256", timeout=30) as stream:
        digest = stream.read(1024).decode("ascii").split()[0]
    with urlopen(f"{base}/{name}", timeout=30) as stream, installer.open("wb") as output:
        shutil.copyfileobj(stream, output)
    if sha256(installer) != digest:
        raise RuntimeError("The previous installer does not match its published SHA-256.")
    subprocess.run([installer, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/NOICONS",
                    f"/DIR={target}"], timeout=120, check=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    executable = target / "YTDownloader.exe"
    original = sha256(executable)
    root = data / "YTDownloader"
    saved = Entry("jNQXAC9IVRw", "Paused upgrade fixture")
    with closing(Store(root)) as store:
        store.set("queue_paused", True)
        store.enqueue([saved], Options(folder=str(scratch / "downloads")))
        store.state(store.jobs()[0].id, "paused")
    # Model a launch from Explorer rather than inheriting the runner's PowerShell 7 module paths.
    env = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    env.update(LOCALAPPDATA=str(data), QT_QPA_PLATFORM="offscreen")
    app = subprocess.Popen([executable], env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    history = []
    try:
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            history = update_history(root / "data.sqlite3")
            if any(version == expected and status == "zainstalowano" for version, status, _ in history):
                if app.poll() != 0 or sha256(executable) == original:
                    raise RuntimeError("The prior process or executable was not replaced.")
                with closing(Store(root)) as store:
                    jobs = store.jobs()
                    if not (store.get("queue_paused") and len(jobs) == 1
                            and jobs[0].entry == saved and jobs[0].state == "paused"):
                        raise RuntimeError("The update did not preserve the paused queue.")
                print(f"Verified automatic upgrade, restart and queue preservation from {previous} to {expected}.")
                return
            if any(status == "błąd instalacji" for _, status, _ in history):
                break
            time.sleep(2)
        print("Application update history:", history)
        log = root / "app.log"
        if log.is_file():
            print(log.read_text(encoding="utf-8")[-4000:])
        raise RuntimeError(f"Automatic upgrade from {previous} to {expected} was not confirmed.")
    finally:
        if app.poll() is None:
            app.terminate()


if __name__ == "__main__":
    verify(*sys.argv[1:])
