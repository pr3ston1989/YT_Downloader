from dataclasses import asdict, dataclass, field
from pathlib import Path
import os
import re
from string import Formatter
from urllib.parse import parse_qs, urlsplit


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "YTDownloader"
LEGACY_TEMPLATE = "{channel}/{playlist}/{index} - {title} [{id}].{ext}"
DEFAULT_TEMPLATE = "{source}/{index} - {title} [{id}].{ext}"
FIELDS = {"source", "channel", "playlist", "index", "title", "id", "ext"}
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
LANGUAGE_CODE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]+)*")
CHANNEL_TABS = ("videos", "shorts", "streams")
TERMINAL = ("done", "skipped", "failed")
WAITING_STATES = ("queued", "paused", "repair", "auth")
EDITABLE_STATES = (*WAITING_STATES, "failed")
SEASON_EPISODE = re.compile(r"(?<!\w)S([0-9]{1,2})\s*E([0-9]{1,4})(?!\w)", re.IGNORECASE)
MARKED_EPISODE = re.compile(
    r"(?:(?<!\w)(?:odcinek|odc\.?|episode|ep\.?)\s*[:#-]?\s*|(?<!\w)#\s*)"
    r"([0-9]{1,4})(?!\w|[.,][0-9])", re.IGNORECASE)
STRUCTURED_EPISODE = re.compile(
    r"^([0-9]{1,4})(?:\.\s+|\s+[-–—|]\s+)|"
    r"(?:[|–—]\s*|\s-\s+)([0-9]{1,4})\s*\.?(?:\s+(?:(?:video|wideo)\s+)?podcast)?\s*$|"
    r"[\[(](?P<bracketed>[0-9]{1,4})[\])]\s*$", re.IGNORECASE)
TRAILING_NUMBER = re.compile(r"\s([0-9]{1,4})\.?\s*$")


@dataclass(frozen=True)
class Source:
    kind: str
    url: str
    tab: str | None = None
    music: bool = False


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
    music = host == "music.youtube.com"
    media_host = "music.youtube.com" if music else "www.youtube.com"
    watch_id = query.get("v", [""])[0]
    playlist = query.get("list", [""])[0]
    if music and len(parts) == 2 and parts[0] == "browse":
        browse_id = parts[1]
        if re.fullmatch(r"MPRE[A-Za-z0-9_-]+", browse_id):
            return Source("album", f"https://{media_host}/browse/{browse_id}", music=True)
        if re.fullmatch(r"VL[A-Za-z0-9_-]+", browse_id):
            playlist = browse_id[2:]
        elif re.fullmatch(r"UC[A-Za-z0-9_-]{22}", browse_id):
            parts = ["channel", browse_id]
        else:
            raise ValueError("W YouTube Music otwórz konkretny utwór, album, playlistę lub stronę wykonawcy.")
    if re.fullmatch(r"[A-Za-z0-9_-]+", playlist) and not (
            music and parts[0] == "watch" and playlist.startswith("RD") and VIDEO_ID.fullmatch(watch_id)):
        is_album = playlist.startswith("OLAK5uy_")
        return Source("album" if is_album else "playlist", f"https://{media_host}/playlist?list={playlist}",
                      music=music or is_album)
    if host == "youtu.be":
        video = parts[0]
    elif parts[0] == "watch":
        video = watch_id
    elif parts[0] in ("shorts", "live", "embed", "v") and len(parts) == 2:
        video = parts[1]
    else:
        video = ""
    if VIDEO_ID.fullmatch(video):
        return Source("video", f"https://{media_host}/watch?v={video}", music=music)
    if host != "youtu.be" and parts[0].startswith("@"):
        base, suffix = parts[:1], parts[1:]
    elif host != "youtu.be" and parts[0] in ("channel", "c", "user") and len(parts) >= 2:
        base, suffix = parts[:2], parts[2:]
    else:
        raise ValueError("Podaj link do filmu, playlisty, kanału lub albumu YouTube Music.")
    if any(not part or part in (".", "..") or "\\" in part for part in base):
        raise ValueError("Nieprawidłowy adres kanału.")
    return Source("channel", "https://www.youtube.com/" + "/".join(base),
                  suffix[0] if suffix and suffix[0] in CHANNEL_TABS else None, music=music)


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    channel: str = ""
    playlist: str = ""
    index: int = 1
    duration: int | None = None
    tab: str = ""
    music: bool = False
    album: str = ""
    track_number: int | None = None
    published_at: float | None = None
    index_label: str = ""
    source_name: str = ""

    @property
    def url(self):
        host = "music.youtube.com" if self.music else "www.youtube.com"
        return f"https://{host}/watch?v={self.id}"


def detect_episode_numbers(entries):
    single = len(entries) == 1
    marked, structured, trailing = {}, {}, {}
    for row, entry in enumerate(entries):
        title = entry.title.strip()
        if match := SEASON_EPISODE.search(title):
            marked[row] = (int(match[1]), int(match[2]))
        elif match := MARKED_EPISODE.search(title):
            marked[row] = (0, int(match[1]))
        elif match := STRUCTURED_EPISODE.search(title):
            number = int(next(group for group in match.groups() if group))
            if not 1900 <= number <= 2099 and (not single or match["bracketed"] is None):
                structured[row] = (0, number)
        elif match := TRAILING_NUMBER.search(title):
            number = int(match[1])
            if not 1900 <= number <= 2099:
                trailing[row] = (0, number)

    numbers = marked | structured
    if single:
        return numbers
    distinct = len(set(numbers.values()))
    if len(set(marked.values())) >= 2 and distinct * 4 >= len(entries):
        return numbers
    if distinct >= 3 and distinct * 2 >= len(entries):
        return numbers
    numbers.update(trailing)
    sequence = sorted({number for season, number in numbers.values() if not season})
    if len(sequence) >= 3 and len(sequence) * 3 >= len(entries) * 2:
        if sequence[-1] - sequence[0] <= 3 * (len(sequence) - 1):
            return numbers
    return {}


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
    keep_sidecars: bool = False
    audio_language_exact: bool = False
    auto_subtitles_only: bool = False

    def validate(self):
        if not Path(self.folder).is_absolute():
            raise ValueError("Wybierz bezwzględną ścieżkę do folderu pobierania.")
        if self.audio_language_exact and not self.audio_language:
            raise ValueError("Podaj kod wymaganego języka audio.")
        if self.audio_language and not LANGUAGE_CODE.fullmatch(self.audio_language):
            raise ValueError("Podaj kod języka audio, np. pl lub en-US.")
        if self.subtitles and not any(language.strip() for language in self.subtitle_languages.split(",")):
            raise ValueError("Podaj przynajmniej jeden język napisów.")
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
    if not template.endswith(".{ext}") or template.count("{ext}") != 1:
        raise ValueError("Zakończ nazwę pliku polem .{ext}.")
    try:
        for part in parts:
            for _, name, spec, conversion in Formatter().parse(part):
                if name is not None and (name not in FIELDS or spec or conversion):
                    raise ValueError
    except ValueError:
        raise ValueError("Użyj pól {source}, {channel}, {playlist}, {index}, {title}, {id}, {ext}.") from None


def safe_segment(value: str, limit: int = 100) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", value):
        value = "_" + value
    # Windows measures path components in UTF-16 units.
    value = value.encode("utf-16-le", errors="replace")[:2 * limit].decode("utf-16-le", errors="ignore")
    return value.rstrip(" .") or "_"


def output_stem(entry: Entry, options: Options, job_id: int) -> Path:
    saved_source = entry.channel if entry.tab and entry.channel else entry.playlist or entry.album or entry.channel
    values = {
        "source": safe_segment(entry.source_name or saved_source or ("YouTube Music" if entry.music else "YouTube"), 80),
        "channel": safe_segment(entry.channel or "YouTube", 60),
        "playlist": safe_segment(entry.playlist or ("Utwory" if entry.music else "Filmy"), 60),
        "index": entry.index_label or f"{entry.index:02d}",
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
