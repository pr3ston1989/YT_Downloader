from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SHA256 = re.compile(r"[a-fA-F0-9]{64}")
API = "https://api.github.com/repos"
GITHUB = "https://github.com"


class HTTPSRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise ValueError("Przekierowanie aktualizacji bez HTTPS.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    def __init__(self):
        self.opener = build_opener(HTTPSRedirect())

    def open(self, url):
        if urlsplit(url).scheme != "https":
            raise ValueError("Źródło aktualizacji wymaga HTTPS.")
        request = Request(url, headers={"User-Agent": "YTDownloader/1", "Accept": "*/*"})
        return self.opener.open(request, timeout=15)

    def text(self, url):
        with self.open(url) as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("Odpowiedź serwera aktualizacji jest zbyt duża.")
        return data.decode("utf-8-sig")

    def json(self, url):
        return json.loads(self.text(url))

    def fetch(self, url, path: Path, digest: str):
        if not SHA256.fullmatch(digest):
            raise ValueError("Brak prawidłowej sumy SHA-256.")
        hashed = hashlib.sha256()
        length = 0
        deadline = time.monotonic() + 180
        with self.open(url) as source, path.open("wb") as target:
            while chunk := source.read(1024 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError("Przekroczono czas pobierania aktualizacji.")
                length += len(chunk)
                if length > 450 * 1024 * 1024:
                    raise ValueError("Paczka aktualizacji jest zbyt duża.")
                hashed.update(chunk)
                target.write(chunk)
        if hashed.hexdigest() != digest.lower():
            raise ValueError("Pobrany plik ma inną sumę SHA-256 niż wydanie.")


@dataclass(frozen=True)
class Package:
    component: str
    version: str
    channel: str
    url: str
    name: str
    sums: str | None
    digest: str = ""

    def checksum(self, http: Http):
        if self.sums:
            text = http.text(self.sums)
            for line in text.splitlines():
                match = re.fullmatch(r"([a-fA-F0-9]{64})[ \t]+[* ]?(\S+)[ \t]*", line.strip())
                if match and match[2].removeprefix("./") == self.name:
                    return match[1]
            if SHA256.fullmatch(text.strip()):
                return text.strip()
            raise ValueError(f"Nie znaleziono sumy SHA-256 dla {self.name}.")
        if not SHA256.fullmatch(self.digest):
            raise ValueError(f"Wydanie nie zawiera sumy SHA-256 dla {self.name}.")
        return self.digest


def github_package(http, component, repo, name, channel="stable", sums=None):
    release = http.json(f"{API}/{repo}/releases/latest")
    tag = release["tag_name"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", tag) or tag in (".", ".."):
        raise ValueError("Nieprawidłowy identyfikator wydania.")
    name = name.format(tag=tag)
    assets = {asset["name"]: asset for asset in release["assets"]}
    base = f"{GITHUB}/{repo}/releases/download/{tag}"
    if name not in assets or (sums and sums not in assets):
        raise ValueError(f"Wydanie {tag} nie zawiera wymaganych plików.")
    digest = (assets[name].get("digest") or "").removeprefix("sha256:")
    return Package(component, tag, channel, f"{base}/{name}", name,
                   f"{base}/{sums}" if sums else None, digest)


def latest(http: Http, component: str, channel="stable"):
    if component == "yt-dlp":
        repo = "yt-dlp/yt-dlp" if channel == "stable" else "yt-dlp/yt-dlp-nightly-builds"
        # Official zipimport distribution also contains the matching yt_dlp_ejs package.
        return github_package(http, component, repo, "yt-dlp", channel, "SHA2-256SUMS")
    if component == "deno":
        return github_package(http, component, "denoland/deno", "deno-x86_64-pc-windows-msvc.zip")
    if component == "ffmpeg":
        return github_package(http, component, "GyanD/codexffmpeg", "ffmpeg-{tag}-essentials_build.zip")
    raise ValueError(f"Nieznany komponent {component}.")
