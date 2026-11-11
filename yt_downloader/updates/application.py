from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.error import HTTPError

from .. import APP_REPO, APP_VERSION
from ..runtime import FROZEN, HIDE_WINDOW, RESOURCES, external_env
from .sources import github_package


INSTALLER = "YTDownloader-Setup.exe"


@dataclass(frozen=True)
class AppUpdate:
    version: str
    installer: Path
    sha256: str


def version_tuple(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise ValueError("Wydanie aplikacji musi mieć wersję X.Y.Z.")
    return tuple(map(int, match.groups()))


def installed():
    return FROZEN and os.name == "nt" and Path(sys.executable).with_name("installed.marker").is_file()


def check_app(http, root, record):
    if not installed():
        return None
    try:
        package = github_package(http, "aplikacja", APP_REPO, INSTALLER,
                                 sums=f"{INSTALLER}.sha256")
        if version_tuple(package.version) <= version_tuple(APP_VERSION):
            return None
        folder = root / "app-update"
        folder.mkdir(parents=True, exist_ok=True)
        installer = folder / INSTALLER
        temporary = installer.with_suffix(".tmp")
        digest = package.checksum(http)
        try:
            http.fetch(package.url, temporary, digest)
            temporary.replace(installer)
        finally:
            temporary.unlink(missing_ok=True)
        record("aplikacja", package.version, "pobrano", "Instalacja po zakończeniu aktywnych zadań.")
        return AppUpdate(package.version, installer, digest)
    except HTTPError as exc:
        if exc.code != 404:
            record("aplikacja", "", "błąd sprawdzania", str(exc))
    except Exception as exc:
        record("aplikacja", "", "błąd sprawdzania", str(exc))
    return None


def read_result(root, record):
    path = root / "app-update" / "result.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        record("aplikacja", data["version"], data["status"], data["detail"])
    except FileNotFoundError:
        return
    finally:
        path.unlink(missing_ok=True)


def restart_for(update: AppUpdate, *, tray=False):
    script = update.installer.parent / "apply-update.ps1"
    shutil.copyfile(RESOURCES / "apply-update.ps1", script)
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    args = [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-ParentId", str(os.getpid()), "-AppExe", sys.executable,
            "-Installer", str(update.installer), "-ExpectedHash", update.sha256,
            "-Version", update.version]
    if tray:
        args.append("-Tray")
    subprocess.Popen(args, creationflags=HIDE_WINDOW, env=external_env(), cwd=script.parent,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
