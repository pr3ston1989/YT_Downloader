"""Exercise the installed UI and workers with local media in a disposable profile."""

from contextlib import closing
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yt_downloader.model import Entry, Options, Preferences
from yt_downloader.runtime import HIDE_WINDOW
from yt_downloader.smoke import ARCHIVED, NORMAL, PLAYLIST, PLAYLIST_TITLE, PROFILE, URGENT, URGENT_CHANNEL
from yt_downloader.store import Store


# Only the copied test-profile engine sees this adapter. The installer, the
# bundled engine and its workers are unmodified. The real downloader processes
# the synthetic media metadata and fetches the clip over HTTP.
FIXTURE_ENGINE = '''

import json as _fixture_json
import os as _fixture_os
from pathlib import Path as _FixturePath


class _QueueFixtureDL(YoutubeDL):
    def extract_info(self, url, download=True, **kwargs):
        fixture = _fixture_json.loads(_FixturePath(_fixture_os.environ["YT_DOWNLOADER_QUEUE_FIXTURE"]).read_text(encoding="utf-8"))
        if url == fixture["playlist_url"] and not download:
            return fixture["playlist"]
        video = fixture["videos"][url]
        if not download:
            return video
        return self.process_ie_result(video, download=True)


YoutubeDL = _QueueFixtureDL
'''


class MediaHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in (f"/{NORMAL}.mp4", f"/{URGENT}.mp4"):
            self.send_error(404)
            return
        self.server.requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(self.server.clip)))
        self.end_headers()
        self.wfile.write(self.server.clip)

    def log_message(self, *args):
        pass


def active_directory(seed, component):
    root = seed / component
    active = json.loads((root / "active.json").read_text(encoding="utf-8"))["active"]
    return root / active["folder"]


def prepare_profile(root, downloads, seed):
    profile = Options(folder=str(downloads), container="mkv", metadata=False, thumbnail=False)
    prefs = Preferences(options=Options(folder=str(root / "unused default folder")), parallel=1, auto_update=False)
    with closing(Store(root)) as store:
        store.set("settings", prefs.to_dict())
        store.set("download_profiles", {PROFILE: asdict(profile)})
        store.set("queue_paused", True)
        assert store.import_archive([f"youtube {ARCHIVED}"]) == (1, 0, 0)

    engine = root / "engine"
    shutil.copytree(seed, engine)
    module = active_directory(engine, "yt-dlp") / "yt_dlp" / "__init__.py"
    with module.open("a", encoding="utf-8") as stream:
        stream.write(FIXTURE_ENGINE)


def write_fixture(path, port):
    normal = {"id": NORMAL, "title": "Film z playlisty", "channel": "Inny autor"}
    urgent = {"id": URGENT, "title": "Pilny film", "channel": URGENT_CHANNEL}
    videos = {}
    for item in (normal, urgent):
        video_id = item["id"]
        videos[Entry(video_id, item["title"]).url] = {
            **item, "url": f"http://127.0.0.1:{port}/{video_id}.mp4", "format_id": "18",
            "ext": "mp4", "height": 32, "vcodec": "h264", "acodec": "aac", "extractor": "generic",
            "extractor_key": "Generic",
        }
    data = {"playlist_url": PLAYLIST, "playlist": {
        "_type": "playlist", "title": PLAYLIST_TITLE, "channel": "Autor playlisty",
        "entries": [{"id": ARCHIVED, "title": "Film z historii", "channel": "Inny kanał"}, normal],
    }, "videos": videos}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def verify_outputs(root, downloads, ffprobe):
    with closing(Store(root)) as store:
        jobs = store.jobs()
        expected = {URGENT: URGENT_CHANNEL, NORMAL: PLAYLIST_TITLE}
        paths = []
        for job in jobs:
            if job.entry.id == ARCHIVED:
                assert job.state == "skipped" and not job.path
                continue
            path = Path(job.path)
            assert job.state == "done" and path.parent == downloads / expected[job.entry.id], job
            assert path.suffix == ".mkv" and path.is_file(), path
            report = subprocess.check_output([ffprobe, "-v", "error", "-show_entries",
                                              "format=format_name:stream=codec_type", "-of", "json", path],
                                             timeout=15, creationflags=HIDE_WINDOW)
            media = json.loads(report)
            assert "matroska" in media["format"]["format_name"].split(","), media
            assert {stream["codec_type"] for stream in media["streams"]} == {"video", "audio"}, media
            paths.append(path)
    assert len(paths) == 2 and set(p for p in downloads.rglob("*") if p.is_file()) == set(paths), paths
    assert not (root / "unused default folder").exists(), "The saved profile's destination was not used exclusively"


def verify(executable):
    seed = executable.parent / "_internal" / "seed"
    ffmpeg = active_directory(seed, "ffmpeg")
    with TemporaryDirectory(prefix="YT Downloader queue test ", dir=os.environ.get("RUNNER_TEMP")) as temporary:
        scratch = Path(temporary).resolve()
        root = scratch / "app data"
        downloads = scratch / "profile downloads"
        prepare_profile(root, downloads, seed)
        clip = scratch / "fixture.mp4"
        subprocess.run([ffmpeg / "ffmpeg.exe", "-v", "error", "-f", "lavfi", "-i", "color=size=32x32:duration=0.5",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-c:v", "libx264", "-c:a", "aac", clip],
                       check=True, timeout=20, creationflags=HIDE_WINDOW)
        with ThreadingHTTPServer(("127.0.0.1", 0), MediaHandler) as server:
            server.clip = clip.read_bytes()
            server.requests = []
            fixture = scratch / "media fixture.json"
            write_fixture(fixture, server.server_port)
            env = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
            env.update(QT_QPA_PLATFORM="offscreen", YT_DOWNLOADER_QUEUE_FIXTURE=str(fixture))
            Thread(target=server.serve_forever, daemon=True).start()
            try:
                subprocess.run([executable, "--smoke-test", root, "--queue-test", "prepare"],
                               check=True, timeout=95, env=env, creationflags=HIDE_WINDOW)
                assert server.requests == [], "A download ran while the queue was paused"
                subprocess.run([executable, "--smoke-test", root, "--queue-test", "resume"],
                               check=True, timeout=95, env=env, creationflags=HIDE_WINDOW)
                assert server.requests == [f"/{URGENT}.mp4", f"/{NORMAL}.mp4"], server.requests
                verify_outputs(root, downloads, ffmpeg / "ffprobe.exe")
            except Exception:
                log = root / "app.log"
                if log.is_file():
                    print(log.read_text(encoding="utf-8")[-6000:], flush=True)
                raise
            finally:
                server.shutdown()
    print("Verified the installed playlist, profile, history skip, priority, restart and both MKV files.")


if __name__ == "__main__":
    verify(Path(sys.argv[1]).resolve(strict=True))
