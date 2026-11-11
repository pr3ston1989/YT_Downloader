from dataclasses import dataclass
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile
from threading import Lock
from uuid import uuid4
import zipfile

from ..runtime import HIDE_WINDOW, RESOURCES, atomic_json, command, external_env
from .sources import Http, Package, latest


COMPONENTS = ("deno", "ffmpeg", "yt-dlp")


@dataclass(frozen=True)
class Runtime:
    engine: str = ""
    deno: str = ""
    ffmpeg: str = ""
    version: str = ""
    channel: str = ""

    @property
    def ready(self):
        return bool(self.engine and self.deno and self.ffmpeg)


def extract(package: Path, target: Path, component: str):
    wanted = {"deno": {"deno.exe"}, "ffmpeg": {"ffmpeg.exe", "ffprobe.exe"}}.get(component)
    kept = set()
    size = 0
    with zipfile.ZipFile(package) as archive:
        for item in archive.infolist():
            if item.is_dir():
                continue
            name = PurePosixPath(item.filename)
            if wanted is None:
                if name.parts[0] not in {"yt_dlp", "yt_dlp_ejs"} and name.name not in {
                    "LICENSE", "UNLICENSE", "THIRD_PARTY_LICENSES.txt",
                }:
                    continue
                if (name.is_absolute() or ".." in name.parts
                        or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in name.parts)):
                    raise ValueError("Nieprawidłowa ścieżka w paczce yt-dlp.")
                path = target.joinpath(*name.parts)
            elif name.name.lower() in wanted:
                path = target / name.name.lower()
                kept.add(path.name)
            elif name.name.lower() in {"license", "license.txt", "copying", "copying.txt"}:
                path = target / name.name.lower()
            else:
                continue
            size += item.file_size
            if size > 650 * 1024 * 1024:
                raise ValueError("Rozpakowana aktualizacja jest zbyt duża.")
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, path.open("wb") as output:
                shutil.copyfileobj(source, output)
    if wanted and kept != wanted:
        raise ValueError(f"Paczka {component} nie zawiera wymaganych plików wykonywalnych.")
    if wanted is None and not (target / "yt_dlp" / "__init__.py").is_file():
        raise ValueError("Paczka nie zawiera modułu yt_dlp.")


def verify(component, root, expected=""):
    if component == "yt-dlp":
        # Launch a fresh process so the candidate never replaces an imported module.
        with tempfile.TemporaryDirectory(dir=root.parent) as work:
            report = Path(work) / "diagnostic.json"
            result = subprocess.run(command("--probe-engine", root, report), timeout=45,
                                    creationflags=HIDE_WINDOW, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            data = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
        if result.returncode or not data.get("version"):
            raise ValueError(data.get("error", "Silnik nie przeszedł testu uruchomienia."))
        if expected and data["version"] != expected:
            raise ValueError(f"Silnik zgłosił wersję {data['version']} zamiast {expected}.")
        return
    names = ("deno",) if component == "deno" else ("ffmpeg", "ffprobe")
    for name in names:
        flag = "--version" if name == "deno" else "-version"
        result = subprocess.run([str(root / (name + ".exe")), flag], timeout=20,
                                creationflags=HIDE_WINDOW, env=external_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=True)
        line = result.stdout.decode("utf-8", errors="replace").splitlines()[0]
        match = re.match(r"deno (\d+)\.(\d+)\.(\d+)", line)
        if name == "deno" and (not match or tuple(map(int, match.groups())) < (2, 3, 0)):
            raise ValueError("Wymagane jest Deno 2.3.0 lub nowsze.")
        if not line.startswith(name + (" " if name == "deno" else " version ")):
            raise ValueError(f"{name} nie zgłosił prawidłowej wersji.")


class EngineManager:
    def __init__(self, root: Path, record=lambda *args: None, *, seed=None, http=None, probe=verify):
        self.root = root
        self.seed = seed if seed is not None else RESOURCES / "seed"
        self.record = record
        self.http = http if http is not None else Http()
        self.probe = probe
        self.lock = Lock()
        self.current = Runtime()

    def read(self, component, *, seed=False):
        directory = (self.seed if seed else self.root) / component
        try:
            values = json.loads((directory / "active.json").read_text(encoding="utf-8"))
            return {key: value for key, value in values.items()
                    if isinstance(value, dict) and isinstance(value.get("version"), str)
                    and value.get("channel") in ("stable", "nightly")
                    and isinstance(value.get("folder"), str)
                    and re.fullmatch(r"[a-f0-9]{32}", value["folder"])}
        except (OSError, ValueError, AttributeError):
            return {}

    def snapshot(self):
        with self.lock:
            return self.current

    def refresh(self):
        yt = self.read("yt-dlp").get("active", {})
        paths = {}
        for component in COMPONENTS:
            active = self.read(component).get("active")
            paths[component] = str(self.root / component / active["folder"]) if active else ""
        with self.lock:
            self.current = Runtime(paths["yt-dlp"],
                                   str(Path(paths["deno"]) / "deno.exe") if paths["deno"] else "",
                                   paths["ffmpeg"], yt.get("version", ""), yt.get("channel", ""))

    def publish(self, component, candidate, previous=None):
        values = {"active": candidate}
        if previous:
            values["previous"] = previous
        atomic_json(self.root / component / "active.json", values)
        self.refresh()

    def stage(self, component, version, channel, populate):
        directory = self.root / component
        directory.mkdir(parents=True, exist_ok=True)
        candidate = {"version": version, "channel": channel, "folder": uuid4().hex}
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            stage = Path(temporary) / "payload"
            stage.mkdir()
            populate(stage)
            self.probe(component, stage, version)
            stage.replace(directory / candidate["folder"])
        return candidate

    def install(self, package: Package):
        component = package.component
        active = self.read(component).get("active")
        if active and (active["version"], active["channel"]) == (package.version, package.channel):
            return False

        def populate(stage):
            downloaded = stage.parent / "package"
            self.http.fetch(package.url, downloaded, package.checksum(self.http))
            extract(downloaded, stage, component)

        try:
            candidate = self.stage(component, package.version, package.channel, populate)
            self.publish(component, candidate, active)
        except Exception as exc:
            self.record(component, package.version, "rollback" if active else "błąd", str(exc))
            return False
        self.record(component, package.version, "zainstalowano", package.channel)
        return True

    def diagnose(self):
        for component in COMPONENTS:
            values = self.read(component)
            active = values.get("active")
            directory = self.root / component
            for name in ("active", "previous"):
                version = values.get(name)
                if not version:
                    continue
                try:
                    self.probe(component, directory / version["folder"], version["version"])
                except Exception as exc:
                    self.record(component, version["version"], "uszkodzono", str(exc))
                    continue
                if name == "previous":
                    self.publish(component, version)
                    self.record(component, version["version"], "rollback", "Przywrócono poprzednią wersję.")
                break
            else:
                if active:
                    (directory / "active.json").unlink(missing_ok=True)
                seed = self.read(component, seed=True).get("active")
                if seed:
                    try:
                        candidate = self.stage(component, seed["version"], seed["channel"],
                                               lambda path: shutil.copytree(
                                                   self.seed / component / seed["folder"], path,
                                                   dirs_exist_ok=True))
                        self.publish(component, candidate)
                        self.record(component, seed["version"], "przywrócono", "Kopia z instalatora.")
                    except Exception as exc:
                        self.record(component, seed["version"], "błąd", str(exc))
            # Diagnose runs before any workers start, so old versions are no longer in use.
            retained = {item["folder"] for item in self.read(component).values()}
            if directory.is_dir():
                for child in directory.iterdir():
                    if (re.fullmatch(r"[a-f0-9]{32}", child.name) or child.name.startswith("tmp")
                            ) and child.name not in retained:
                        shutil.rmtree(child, ignore_errors=True)
        self.refresh()
        return self.snapshot()

    def check(self, component, channel="stable"):
        try:
            package = latest(self.http, component, channel)
            return self.install(package)
        except Exception as exc:
            self.record(component, "", "błąd sprawdzania", str(exc))
            return False
