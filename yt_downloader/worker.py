from dataclasses import asdict, replace
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from .auth import crypt
from .model import DEFAULT_TEMPLATE, LANGUAGE_CODE, LEGACY_TEMPLATE, MEMBERS_ONLY, Entry, Options, VIDEO_ID, output_stem
from .runtime import atomic_json


PARTIAL_FILE = re.compile(r"(?:f[\w+-]+\.)?[\w]+\.(?:part(?:-Frag\d+)?|ytdl)")
SPLIT_STREAM = re.compile(r"f[\w+-]+\.[\w]+")
MIN_FREE_SPACE = 16 * 1024 ** 2


def load_engine(path):
    root = Path(path)
    if not (root / "yt_dlp" / "__init__.py").is_file():
        raise FileNotFoundError("Brak silnika yt-dlp.")
    sys.dont_write_bytecode = True
    # A Windows GUI-subsystem executable can start without standard streams.
    for name in ("stdin", "stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "r" if name == "stdin" else "w", encoding="utf-8"))
    sys.path.insert(0, str(root))
    from yt_dlp import YoutubeDL
    return YoutubeDL


def probe_engine(path, report):
    try:
        ydl = load_engine(path)
        from yt_dlp.extractor.youtube.jsc._builtin import vendor
        from yt_dlp.version import __version__
        import yt_dlp_ejs
        import yt_dlp_ejs.yt.solver

        if yt_dlp_ejs.version != vendor.VERSION:
            raise ValueError("Pakiet EJS nie odpowiada wersji silnika.")
        with ydl({"quiet": True, "plugin_dirs": []}) as instance:
            assert instance.get_info_extractor("Youtube").suitable(
                "https://www.youtube.com/watch?v=BaW_jenozKc")
        atomic_json(Path(report), {"version": __version__})
        return 0
    except Exception as exc:
        atomic_json(Path(report), {"error": f"{type(exc).__name__}: {exc}"})
        return 1


def explain(error, *, source_kind=None):
    text = str(error).casefold()
    if any(message in text for message in (MEMBERS_ONLY.casefold(), "members-only", "members only",
                                          "members_only", "join this channel to get access")):
        return "members", MEMBERS_ONLY
    if "playlist is private" in text or "private playlist" in text:
        return "auth", "Playlista jest prywatna. Zaloguj się na konto z dostępem do niej."
    if source_kind in ("playlist", "album") and any(message in text for message in (
            "playlist does not exist", "playlist is not available", "playlist is unavailable",
            "playlist is not accessible")):
        return "playlist_access", "Playlista jest niedostępna. Jeśli jest prywatna, zaloguj się na konto z dostępem."
    if re.search(r"channel does not have an? (videos|shorts|streams|live) tab", text):
        return "missing_tab", "Kanał nie ma wybranej zakładki."
    if source_kind == "channel" and "playlist does not exist" in text:
        return "unavailable", "YouTube nie udostępnia listy przesłanych materiałów tego kanału."
    patterns = (
        ("auth", "Film ma ograniczenie wiekowe. Zaloguj się na konto z dostępem do materiałów 18+.",
         ("age-restricted", "age restriction", "confirm your age", "verify your age",
          "age verification", "age-verification", "verification of your age")),
        ("auth", "Zaloguj się w przeglądarce, aby otworzyć ten materiał.",
         ("sign in", "log in", "login required", "requires authentication",
          "cookies are no longer valid", "private video", "video is private", "not a bot")),
        ("cookies", "Nie udało się odczytać ciasteczek. Sprawdź profil lub użyj Firefoksa.",
         ("failed to load cookies", "dpapi", "cookie database", "cookies database")),
        ("unavailable", "Film jest niedostępny, usunięty lub objęty blokadą regionalną.",
         ("unavailable", "has been removed", "not available in your country", "geo-restricted")),
        ("network", "Błąd sieci. Sprawdź połączenie z internetem.",
         ("timed out", "timeout", "connection", "network is unreachable", "getaddrinfo",
          "http error 429", "http error 500", "http error 502", "http error 503", "http error 504",
          "name resolution", "urlopen error", "remote end closed", "incomplete read",
          "incomplete data received")),
        ("extractor", "Silnik nie odczytał strumieni YouTube.",
         ("unable to extract", "http error 403", "signature extraction", "nsig", "challenge solving",
          "no video formats", "only images are available", "not available on this app",
          "failed to resolve album to playlist", "unable to download format")),
        ("format", "Film nie ma formatu pasującego do wybranych ustawień.",
         ("requested format is not available", "no matching format")),
        ("disk", "Nie można zapisać pliku. Sprawdź dostęp i wolne miejsce.",
         ("no space", "permission denied", "access is denied", "file name too long", "winerror 5",
          "winerror 112", "not enough space on the disk")),
    )
    for code, message, needles in patterns:
        if any(needle in text for needle in needles):
            return code, message
    return "other", "Operacja nie powiodła się. Szczegóły są w pliku logów."


class Logger:
    def __init__(self, emit):
        self.emit = emit
        self.extractor_warning = ""

    def debug(self, message):
        pass

    def warning(self, message):
        if explain(message)[0] == "extractor":
            self.extractor_warning = message
        self.emit("log", detail=message)

    error = warning


def base_parameters(request, logger):
    runtime = request["runtime"]
    result = {
        "quiet": True,
        "noprogress": True,
        "logger": logger,
        "plugin_dirs": [],
        "socket_timeout": 20,
        "retries": 1,
        "extractor_retries": 1,
        "fragment_retries": 2,
        "skip_unavailable_fragments": False,
        "ffmpeg_location": runtime["ffmpeg"],
        "js_runtimes": {"deno": {"path": runtime["deno"]}},
        "cachedir": request["cache"],
    }
    auth = request.get("auth", {})
    if auth.get("browser"):
        result["cookiesfrombrowser"] = (auth["browser"], auth.get("profile") or None)
    elif auth.get("file") and Path(auth["file"]).is_file():
        try:
            text = crypt(Path(auth["file"]).read_bytes(), decrypt=True).decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("Failed to load cookies from the encrypted file") from exc
        result["cookiefile"] = io.StringIO(text)
    return result


def publication_time(info):
    if isinstance(info.get("timestamp"), (int, float)):
        return info["timestamp"]
    try:
        return datetime.strptime(info.get("upload_date") or "", "%Y%m%d").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def scan(ydl, request, parameters, emit):
    source = request["source"]
    kind = source["kind"]
    single = kind == "video"
    music = source.get("music", False)
    album_source = kind == "album"
    artist_channel = (re.fullmatch(r"https://www\.youtube\.com/channel/UC([A-Za-z0-9_-]{22})", source["url"])
                      if music else None)
    tabs = request["tabs"] if kind == "channel" else [""]
    url_parts = urlsplit(source["url"])
    source_id = parse_qs(url_parts.query).get("list", [url_parts.path.rsplit("/", 1)[-1]])[0]
    source_name = ""
    settings = {
        **parameters,
        "extract_flat": False if single else "in_playlist",
        "ignoreerrors": False,
        "noplaylist": single,
        "skip_download": True,
    }
    if not single:
        settings.update(
            extractor_retries=3,
            retry_sleep_functions={"extractor": lambda n: min(2 ** n, 8)},
            extractor_args={"youtube": {"raise_incomplete_data": ["1"]},
                            "youtubetab": {"approximate_date": ["1"]}},
        )
    count = 0
    skipped_members = 0
    last_error = None
    for tab in tabs:
        url = f"{source['url']}/{tab}" if tab else source["url"]
        try:
            with ydl(settings) as client:
                # Processing a playlist in YoutubeDL returns entries only after exhausting
                # the continuation iterator. Read that iterator directly for the preview.
                try:
                    info = client.extract_info(url, download=False, process=single)
                except Exception as exc:
                    if not (tab == "videos" and artist_channel
                            and "channel does not have a videos tab" in str(exc).casefold()):
                        raise
                    uploads = f"https://www.youtube.com/playlist?list=UU{artist_channel[1]}"
                    info = client.extract_info(uploads, download=False, process=False)
                if album_source and info.get("_type") == "url":
                    info = client.extract_info(info["url"], ie_key=info.get("ie_key"), download=False, process=False)
                if info.get("_type") in ("url", "url_transparent"):
                    info = client.process_ie_result(info, download=False)
                collection = "" if single else info.get("title") or ""
                owner = info.get("channel") or info.get("uploader") or ""
                if album_source:
                    collection = (info.get("album") or collection).removeprefix("Album - ")
                if not source_name:
                    if kind == "channel":
                        source_name = owner or source_id
                    elif single:
                        source_name = owner or ("YouTube Music" if music else "YouTube")
                    else:
                        source_name = collection or source_id
                entries = info.get("entries", (info,))
                for index, entry in enumerate(() if entries is None else entries, 1):
                    video_id = entry.get("id") if entry else None
                    if not isinstance(video_id, str) or not VIDEO_ID.fullmatch(video_id):
                        continue
                    if request.get("skip_members", True) and entry.get("availability") == "subscriber_only":
                        skipped_members += 1
                        continue
                    number = entry.get("playlist_index") or index
                    album = (entry.get("album") or (collection if album_source else "")) if music else ""
                    title = (entry.get("track") if music else None) or entry.get("title") or video_id
                    channel = (entry.get("artist") if music else None) or entry.get("channel") or entry.get("uploader")
                    track_number = entry.get("track_number") or (number if album_source else None)
                    item = Entry(
                        id=video_id, title=title,
                        channel=(owner or "YouTube Music") if album_source else channel or owner,
                        playlist=collection or album, index=number, duration=entry.get("duration"),
                        tab=tab, music=music, album=album, track_number=track_number,
                        published_at=publication_time(entry), source_name=source_name,
                    )
                    emit("entry", entry=asdict(item))
                    count += 1
        except Exception as exc:
            if kind != "channel" or len(tabs) == 1:
                raise
            last_error = exc
            code, message = explain(exc, source_kind=kind)
            emit("scan_warning", tab=tab, code=code, message=message)
    if not count and last_error:
        raise last_error
    if skipped_members:
        emit("done", count=count, skipped_members=skipped_members)
    else:
        emit("done", count=count)


def format_expression(options):
    language = options.audio_language.strip().lower()
    lang = ""
    if LANGUAGE_CODE.fullmatch(language):
        end = "$" if options.audio_language_exact else "(?:-|$)"
        lang = f"[language~='(?i)^{language}{end}']"
    height = f"[height<={options.resolution}]" if options.resolution else ""
    if options.audio_only:
        default = "bestaudio/best"
        preferred = f"bestaudio{lang}/best{lang}"
    else:
        default = f"bestvideo{height}+bestaudio/best{height}"
        preferred = f"bestvideo{height}+bestaudio{lang}/best{height}{lang}"
    if lang:
        return preferred if options.audio_language_exact else f"{preferred}/{default}"
    return default


def inspect_media(ydl, request, parameters, emit):
    entry = Entry(**request["entry"])
    settings = {**parameters, "noplaylist": True, "skip_download": True,
                "extractor_args": {"youtube": {"skip": ["translated_subs"]}}}
    with ydl(settings) as client:
        info = client.extract_info(entry.url, download=False)
    video, audio = set(), set()
    for fmt in info.get("formats") or [info]:
        if fmt.get("has_drm") or not fmt.get("url"):
            continue
        if (codec := fmt.get("vcodec")) and codec != "none" and fmt.get("height"):
            video.add((fmt["height"], fmt.get("fps") or 0, codec, fmt.get("ext") or ""))
        if (codec := fmt.get("acodec")) and codec != "none":
            audio.add((fmt.get("language") or "", codec, fmt.get("abr") or 0, fmt.get("ext") or ""))
    subtitles = []
    for key, automatic in (("subtitles", False), ("automatic_captions", True)):
        for language, tracks in sorted((info.get(key) or {}).items()):
            if tracks:
                name = next((track["name"] for track in tracks if track.get("name")), "")
                subtitles.append((language, name, automatic))
    emit("done", video=sorted(video, reverse=True), audio=sorted(audio), subtitles=subtitles)


def add_audio_encoder(client, options):
    from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

    class EncodeAudio(FFmpegPostProcessor):
        def run(self, info):
            extension = options.audio_format
            encoder = {"mp3": "libmp3lame", "m4a": "aac", "opus": "libopus"}[extension]
            source = Path(info["filepath"])
            target = source.with_suffix(f".{extension}")
            stage = target.with_name(f"{target.stem}.encode.{extension}")
            flags = ["-map", "0:a:0", "-vn", "-c:a", encoder, "-b:a", f"{options.bitrate}k"]
            if extension == "opus":
                flags.extend(("-vbr", "off"))
            self.run_ffmpeg(str(source), str(stage), flags)
            os.replace(stage, target)
            info["filepath"] = str(target)
            info["ext"] = extension
            return ([] if source == target else [str(source)]), info

    client.add_post_processor(EncodeAudio(client))


def add_album_metadata(client, entry):
    from yt_dlp.postprocessor.common import PostProcessor

    class AlbumMetadata(PostProcessor):
        def run(self, info):
            if entry.album and not info.get("album"):
                info["album"] = entry.album
            if entry.track_number and not info.get("track_number"):
                info["track_number"] = entry.track_number
            return [], info

    client.add_post_processor(AlbumMetadata(client), when="pre_process")


def bind_partial_download(client, stem, marker, emit):
    selector = client.format_selector

    def select(context):
        for selected in selector(context):
            formats = selected.get("requested_formats") or (selected,)
            identity = [{key: fmt.get(key) for key in
                         ("format_id", "ext", "vcodec", "acodec", "language", "protocol", "filesize")}
                        for fmt in formats]
            try:
                previous = json.loads(marker.read_text(encoding="utf-8"))
                if not isinstance(previous, list):
                    previous = []
            except FileNotFoundError:
                previous = None
            except ValueError:
                previous = []
            if previous is not None and previous != identity:
                # The same filename can receive a different encoding after a retry.
                # Only remove incomplete files and yt-dlp's separate stream intermediates.
                prefix = stem.name + "."
                intermediates = {
                    f"f{fmt.get('format_id')}.{fmt.get('ext')}" for fmt in previous if isinstance(fmt, dict)
                }
                discarded = False
                for path in stem.parent.iterdir():
                    suffix = path.name.removeprefix(prefix)
                    if suffix != path.name and (suffix in intermediates or PARTIAL_FILE.fullmatch(suffix)):
                        path.unlink()
                        discarded = True
                if discarded:
                    emit("log", detail="Strumień zmienił się. Rozpoczęto pobieranie od początku.")
            if previous != identity:
                atomic_json(marker, identity)
            yield selected

    client.format_selector = select


def download_stem(entry: Entry, options: Options, job_id: int, extension: str) -> Path:
    # Jobs queued before source names were recorded keep their previous path only
    # when there is an existing download to finish at that path.
    if entry.source_name or options.template.replace("\\", "/") != LEGACY_TEMPLATE:
        return output_stem(entry, options, job_id)
    old = output_stem(entry, options, job_id)
    if old.parent.is_dir():
        prefix = old.name + "."
        for file in old.parent.iterdir():
            if not file.name.startswith(prefix):
                continue
            suffix = file.name[len(prefix):]
            existing = (suffix in (extension, "yt-resume.json") or PARTIAL_FILE.fullmatch(suffix)
                        or SPLIT_STREAM.fullmatch(suffix))
            if existing and file.is_file():
                return old
    return output_stem(entry, replace(options, template=DEFAULT_TEMPLATE), job_id)


def download(ydl, request, parameters, emit):
    from yt_dlp.postprocessor import get_postprocessor

    entry = Entry(**request["entry"])
    options = Options(**request["options"])
    options.validate()
    extension = options.audio_format if options.audio_only else options.container
    stem = download_stem(entry, options, request["job_id"], extension)
    stem.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(stem.parent).free < MIN_FREE_SPACE:
        raise OSError("No space left for this download (less than 16 MiB free).")
    final = Path(f"{stem}.{extension}")
    marker = stem.with_name(stem.name + ".yt-resume.json")
    resuming = marker.is_file()
    last_update = 0.0
    streams_done = 0
    skipped_reason = None

    def match_video(info, *, incomplete=False):
        nonlocal skipped_reason
        if request.get("skip_members", True) and info.get("availability") == "subscriber_only":
            skipped_reason = MEMBERS_ONLY
        elif not incomplete and not resuming:
            formats = info.get("requested_formats") or (info,)
            sizes = [item.get("filesize") for item in formats]
            if all(isinstance(size, int) and size > 0 for size in sizes):
                if sum(sizes) + MIN_FREE_SPACE > shutil.disk_usage(stem.parent).free:
                    skipped_reason = "No space left for the selected streams."
        return skipped_reason

    def progress(data):
        nonlocal last_update, streams_done
        now = time.monotonic()
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        if data["status"] == "finished":
            streams_done += 1
            last_update = 0.0
        elif now - last_update >= 0.2:
            last_update = now
            separate = len(data.get("info_dict", {}).get("requested_formats") or ()) > 1
            ratio = (data.get("downloaded_bytes", 0) / total) if total else 0
            value = (streams_done + ratio) / 2 if separate else ratio
            emit("progress", percent=min(99, value * 100), speed=data.get("speed"), eta=data.get("eta"))

    def processing(data):
        if data["status"] == "started":
            emit("processing", speed=None, eta=None)

    post = []
    if not options.audio_only:
        post.append({"key": "FFmpegVideoRemuxer", "preferedformat": extension})
    if options.subtitles:
        post.append({"key": "FFmpegSubtitlesConvertor", "format": "srt", "when": "before_dl"})
        if options.embed_subtitles and not options.audio_only:
            post.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
    if options.metadata:
        post.append({"key": "FFmpegMetadata", "add_metadata": True, "add_chapters": True,
                     "add_infojson": extension == "mkv"})
    if options.thumbnail:
        post.append({"key": "EmbedThumbnail", "already_have_thumbnail": options.keep_sidecars})
    languages = [re.escape(lang.strip())
                 for lang in options.subtitle_languages.split(",") if lang.strip()]
    templates = {"default": str(stem).replace("%", "%%") + ".%(ext)s"}
    if not options.keep_sidecars:
        # run_worker sets the temporary directory to the parent-owned worker folder.
        # Keep temporary inputs away from existing sidecars and clean them up on cancellation.
        temporary = Path(tempfile.gettempdir())
        templates.update(thumbnail=str(temporary / "cover").replace("%", "%%") + ".%(ext)s",
                         infojson=str(temporary / "info.json").replace("%", "%%"))
    settings = {
        **parameters,
        "format": format_expression(options),
        "merge_output_format": options.container,
        "final_ext": extension,
        "outtmpl": templates,
        "noplaylist": True,
        "windowsfilenames": True,
        "continuedl": True,
        "nopart": False,
        "overwrites": False,
        "writethumbnail": options.thumbnail,
        "writeinfojson": options.metadata and options.keep_sidecars,
        "writedescription": options.metadata and options.keep_sidecars,
        "writesubtitles": options.subtitles and not options.auto_subtitles_only,
        "writeautomaticsub": options.subtitles and (options.auto_subtitles or options.auto_subtitles_only),
        "extractor_args": {"youtube": {"skip": ["translated_subs"]}},
        "subtitleslangs": languages,
        "progress_hooks": [progress],
        "postprocessor_hooks": [processing],
        "match_filter": match_video,
    }
    if entry.music:
        settings.update(check_formats="selected", paths={"temp": tempfile.gettempdir()})
    with ydl(settings) as client:
        bind_partial_download(client, stem, marker, emit)
        if options.metadata and entry.music and (entry.album or entry.track_number):
            add_album_metadata(client, entry)
        if options.audio_only:
            add_audio_encoder(client, options)
        for processor in post:
            args = dict(processor)
            key = args.pop("key")
            when = args.pop("when", "post_process")
            client.add_post_processor(get_postprocessor(key)(client, **args), when=when)
        client.extract_info(entry.url, download=True)
    if skipped_reason:
        raise ValueError(skipped_reason)
    if not final.is_file() or not final.stat().st_size:
        raise OSError("Nie powstał końcowy plik multimedialny.")
    marker.unlink(missing_ok=True)
    emit("done", path=str(final))


def run_worker(work):
    work = Path(work)
    tempfile.tempdir = str(work)
    request_path = work / "request.json"
    for _ in range(150):
        if request_path.is_file():
            break
        time.sleep(0.04)
    request = json.loads(request_path.read_text(encoding="utf-8"))
    with (work / "events.jsonl").open("a", encoding="utf-8", buffering=1) as events:
        def emit(kind, **payload):
            events.write(json.dumps({"kind": kind, **payload}, ensure_ascii=False) + "\n")

        logger = Logger(emit)
        try:
            ydl = load_engine(request["runtime"]["engine"])
            parameters = base_parameters(request, logger)
            operation = {"scan": scan, "download": download, "inspect": inspect_media}[request["operation"]]
            operation(ydl, request, parameters, emit)
            return 0
        except Exception as exc:
            kind, message = explain(exc, source_kind=request.get("source", {}).get("kind"))
            if kind in {"format", "other"} and logger.extractor_warning:
                kind, message = explain(logger.extractor_warning)
            emit("error", code=kind, message=message, detail=str(exc))
            return 1
