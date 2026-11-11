from dataclasses import asdict, dataclass, field
from pathlib import Path
import os
import re
from string import Formatter
from urllib.parse import parse_qs, urlsplit


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YTDownloader"
DEFAULT_TEMPLATE = "{channel}/{playlist}/{index} - {title} [{id}].{ext}"
FIELDS = {"channel", "playlist", "index", "title", "id", "ext"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHANNEL_TABS = ("videos", "shorts", "streams")
TERMINAL = ("done", "skipped", "failed")


@dataclass(frozen=True)
class Source:
    kind: str
    url: str
    tab: str | None = None


def parse_source(text: str) -> Source:
    value = text.strip()
    if not value:
        raise ValueError("Wklej link do YouTube.")
    if "://" not in value:
        value = "https://" + value
    url = urlsplit(value)
    host = url.hostname
    if (url.scheme not in ("http", "https") or url.username or url.password
            or url.port not in (None, 80, 443)
            or host not in {"youtube.com", "www.youtube.com", "m.youtube.com",
                            "music.youtube.com", "youtu.be"}):
        raise ValueError("Podaj prawidłowy link do YouTube.")
    query = parse_qs(url.query)
    parts = url.path.strip("/").split("/")
    playlist = query.get("list", [""])[0]
    if re.fullmatch(r"[A-Za-z0-9_-]+", playlist):
        return Source("playlist", f"https://www.youtube.com/playlist?list={playlist}")
    if host == "youtu.be":
        video = parts[0]
    elif parts[0] == "watch":
        video = query.get("v", [""])[0]
    elif parts[0] in ("shorts", "live", "embed", "v") and len(parts) == 2:
        video = parts[1]
    else:
        video = ""
    if VIDEO_ID.fullmatch(video):
        return Source("video", f"https://www.youtube.com/watch?v={video}")
    if host != "youtu.be" and parts[0].startswith("@"):
        base, suffix = parts[:1], parts[1:]
    elif host != "youtu.be" and parts[0] in ("channel", "c", "user") and len(parts) >= 2:
        base, suffix = parts[:2], parts[2:]
    else:
        raise ValueError("Podaj link do filmu, playlisty lub kanału YouTube.")
    if any(not part or part in (".", "..") or "\\" in part for part in base):
        raise ValueError("Nieprawidłowy adres kanału.")
    return Source("channel", "https://www.youtube.com/" + "/".join(base),
                  suffix[0] if suffix and suffix[0] in CHANNEL_TABS else None)


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    channel: str = ""
    playlist: str = ""
    index: int = 1
    duration: int | None = None
    tab: str = ""


@dataclass
class Options:
    folder: str = field(default_factory=lambda: str(Path.home() / "Videos" / "YT Downloader"))
    template: str = DEFAULT_TEMPLATE
    resolution: int = 1080
    container: str = "mp4"
    audio_only: bool = False
    audio_format: str = "mp3"
    bitrate: int = 192
    audio_language: str = ""
    subtitles: bool = False
    auto_subtitles: bool = False
    subtitle_languages: str = "pl,en"
    embed_subtitles: bool = False
    thumbnail: bool = True
    metadata: bool = True
    force: bool = False

    def validate(self):
        if not Path(self.folder).is_absolute():
            raise ValueError("Wybierz bezwzględną ścieżkę do folderu pobierania.")
        validate_template(self.template)


@dataclass
class Preferences:
    options: Options = field(default_factory=Options)
    parallel: int = 2
    theme: str = "system"
    auto_update: bool = True
    channel: str = "stable"
    browser: str = ""
    browser_profile: str = ""
    tray: bool = False
    autostart: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, values):
        values = dict(values)
        values["options"] = Options(**values.get("options", {}))
        return cls(**values)


def validate_template(template: str):
    parts = template.replace("\\", "/").split("/")
    if not all(part and part not in (".", "..") for part in parts):
        raise ValueError("Szablon musi być ścieżką względną w folderze pobierania.")
    if not template.endswith(".{ext}") or any("{ext}" in part for part in parts[:-1]):
        raise ValueError("Zakończ nazwę pliku polem .{ext}.")
    try:
        for part in parts:
            for _, name, spec, conversion in Formatter().parse(part):
                if name is not None and (name not in FIELDS or spec or conversion):
                    raise ValueError
    except ValueError:
        raise ValueError("Użyj pól {channel}, {playlist}, {index}, {title}, {id}, {ext}.") from None


def safe_segment(value: str, limit: int = 100) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", value):
        value = "_" + value
    # Windows measures path components in UTF-16 units.
    value = value.encode("utf-16-le", errors="replace")[:2 * limit].decode("utf-16-le", errors="ignore")
    return value.rstrip(" .") or "_"


def output_stem(entry: Entry, options: Options, job_id: int) -> Path:
    values = {
        "channel": safe_segment(entry.channel or "YouTube", 60),
        "playlist": safe_segment(entry.playlist or "Filmy", 60),
        "index": f"{entry.index:02d}",
        "title": safe_segment(entry.title, 95),
        "id": entry.id,
    }
    *directories, name = options.template[:-len(".{ext}")].replace("\\", "/").split("/")
    stem = safe_segment(name.format(**values), 150)
    if entry.id not in stem:
        stem += f" [{entry.id}]"
    if options.force:
        stem += f" ({job_id})"
    root = Path(options.folder).resolve()
    result = root.joinpath(*(safe_segment(part.format(**values), 80) for part in directories), stem)
    if not result.parent.resolve().is_relative_to(root):
        raise ValueError("Ścieżka z szablonu prowadzi poza folder pobierania.")
    return result
