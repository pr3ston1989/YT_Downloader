from base64 import b64encode
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from yt_downloader.model import Entry, Options, parse_source
from yt_downloader.updates import EngineManager
from yt_downloader.worker import download, load_engine, scan


TRACK = "https://music.youtube.com/watch?v=MgNrAu2pzNs"
PLAYLIST = "https://music.youtube.com/playlist?list=PLMusic"
ALBUM = "https://music.youtube.com/browse/MPREb_Album"
ALBUM_LIST = "https://music.youtube.com/playlist?list=OLAK5uy_Album"


class MusicTests(unittest.TestCase):
    def test_music_links_keep_the_music_host_and_canonical_type(self):
        for url, kind, canonical in (
            (TRACK, "video", TRACK),
            (TRACK + "&list=RDAMVMMgNrAu2pzNs", "video", TRACK),
            (TRACK + "&list=PLMusic", "playlist", PLAYLIST),
            ("https://music.youtube.com/browse/VLPLMusic", "playlist", PLAYLIST),
            (ALBUM, "album", ALBUM),
            (ALBUM_LIST, "album", ALBUM_LIST),
            ("https://music.youtube.com/browse/UC9ALqqC4aIeG5iDs7i90Bfw", "channel",
             "https://www.youtube.com/channel/UC9ALqqC4aIeG5iDs7i90Bfw"),
        ):
            with self.subTest(url=url):
                source = parse_source(url)
                self.assertEqual((source.kind, source.url, source.music), (kind, canonical, True))

    def test_album_redirect_yields_ordered_music_tracks_and_album_metadata(self):
        client = MagicMock()
        client.extract_info.side_effect = [
            {"_type": "url", "url": ALBUM_LIST, "ie_key": "YoutubeTab"},
            {"_type": "playlist", "title": "Album - Live", "channel": "Artysta", "entries": iter([
                {"id": "MgNrAu2pzNs", "title": "Pierwszy"},
                {"id": "jNQXAC9IVRw", "title": "Drugi", "artist": "Gość", "track_number": 5},
            ])},
        ]
        ydl = MagicMock()
        ydl.return_value.__enter__.return_value = client
        events = []
        scan(ydl, {"source": asdict(parse_source(ALBUM))}, {},
             lambda kind, **values: events.append((kind, values)))

        client.extract_info.assert_called_with(ALBUM_LIST, ie_key="YoutubeTab", download=False, process=False)
        first, second = [Entry(**values["entry"]) for kind, values in events if kind == "entry"]
        self.assertEqual(first.url, TRACK)
        self.assertEqual((first.channel, first.playlist, first.album, first.track_number),
                         ("Artysta", "Live", "Live", 1))
        self.assertEqual((second.index, second.track_number), (2, 5))
        self.assertEqual(second.channel, first.channel)
        self.assertEqual((first.source_name, second.source_name), ("Live", "Live"))

    def test_old_jobs_keep_the_standard_youtube_url(self):
        entry = Entry(**{"id": "jNQXAC9IVRw", "title": "Film"})
        self.assertEqual(entry.url, "https://www.youtube.com/watch?v=jNQXAC9IVRw")

    def test_topic_without_a_videos_tab_uses_the_uploads_playlist(self):
        source = parse_source("https://music.youtube.com/browse/UC9ALqqC4aIeG5iDs7i90Bfw")
        client = MagicMock()
        client.extract_info.side_effect = [
            ValueError("This channel does not have a videos tab"),
            {"_type": "playlist", "entries": [{"id": "MgNrAu2pzNs"}]},
        ]
        ydl = MagicMock()
        ydl.return_value.__enter__.return_value = client
        events = []
        scan(ydl, {"source": asdict(source), "tabs": ["videos"]}, {},
             lambda kind, **values: events.append((kind, values)))

        client.extract_info.assert_called_with("https://www.youtube.com/playlist?list=UU9ALqqC4aIeG5iDs7i90Bfw",
                                               download=False, process=False)
        self.assertEqual(events[-1], ("done", {"count": 1}))


class MusicEngineTests(unittest.TestCase):
    def test_music_audio_has_album_track_and_artist_tags(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "seed"
        manager = EngineManager(root)
        manager.refresh()
        runtime = manager.snapshot()
        if not runtime.ready:
            self.skipTest("The Windows build prepares the verified media tools.")

        with (tempfile.TemporaryDirectory() as directory,
              patch.object(sys, "path", sys.path.copy()), patch.object(sys, "dont_write_bytecode")):
            work = Path(directory)
            source = work / "fixture.m4a"
            thumbnail = work / "fixture.png"
            ffmpeg = Path(runtime.ffmpeg) / "ffmpeg.exe"
            subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi",
                            "-i", "sine=frequency=440:duration=0.3", "-c:a", "aac", source],
                           check=True, timeout=20, capture_output=True)
            subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=size=32x32",
                            "-frames:v", "1", thumbnail], check=True, timeout=20, capture_output=True)
            data_url = "data:audio/mp4;base64," + b64encode(source.read_bytes()).decode("ascii")
            cover_url = "data:image/png;base64," + b64encode(thumbnail.read_bytes()).decode("ascii")
            entry = Entry("MgNrAu2pzNs", "Utwór testowy", "Wykonawca", "Album testowy", 2,
                          music=True, album="Album testowy", track_number=2)
            ydl = load_engine(runtime.engine)
            extracted = []

            class Fixture(ydl):
                def extract_info(self, url, download=True):
                    extracted.append(url)
                    return self.process_ie_result({
                        "id": entry.id, "title": entry.title, "url": data_url,
                        "ext": "m4a", "acodec": "aac", "vcodec": "none",
                        "artist": entry.channel, "description": "Audio test",
                        "thumbnails": [{"url": cover_url, "ext": "png"}],
                        "extractor": "generic", "extractor_key": "Generic",
                    }, download=download)

            for keep_sidecars in (False, True):
                with self.subTest(keep_sidecars=keep_sidecars):
                    events = []
                    options = Options(folder=str(work / str(keep_sidecars)), audio_only=True,
                                      audio_format="m4a", keep_sidecars=keep_sidecars)
                    with patch("yt_downloader.worker.tempfile.gettempdir", return_value=directory):
                        download(Fixture, {"entry": asdict(entry), "options": asdict(options), "job_id": 1},
                                 {"quiet": True, "logger": MagicMock(), "ffmpeg_location": runtime.ffmpeg},
                                 lambda kind, **values: events.append((kind, values)))
                    path = Path(next(values["path"] for kind, values in events if kind == "done"))
                    files = {path}
                    if keep_sidecars:
                        files.update(path.with_suffix(suffix) for suffix in (".png", ".info.json", ".description"))
                    self.assertEqual(set(path.parent.iterdir()), files)
                    self.assertFalse((work / "cover.png").exists())
                    probe = subprocess.run([Path(runtime.ffmpeg) / "ffprobe.exe", "-v", "error",
                                            "-show_entries", "format_tags=title,artist,album,track,description:stream_disposition=attached_pic",
                                            "-of", "json", path], check=True, capture_output=True,
                                           encoding="utf-8", timeout=20)
                    info = json.loads(probe.stdout)
                    self.assertEqual(info["format"]["tags"],
                                     {"title": "Utwór testowy", "artist": "Wykonawca", "album": "Album testowy",
                                      "track": "2", "description": "Audio test"})
                    self.assertTrue(any(stream["disposition"]["attached_pic"] for stream in info["streams"]))
            self.assertEqual(extracted, [TRACK, TRACK])


if __name__ == "__main__":
    unittest.main()
