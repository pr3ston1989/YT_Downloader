from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from yt_downloader import worker
from yt_downloader.model import DEFAULT_TEMPLATE, LEGACY_TEMPLATE, Entry, Options
from yt_downloader.process import Worker
from yt_downloader.updates import EngineManager


class RangeServer(BaseHTTPRequestHandler):
    def do_GET(self):
        data = self.server.payloads[self.path]
        offset = int(self.headers.get("Range", "bytes=0-").removeprefix("bytes=").split("-")[0])
        self.server.offsets.append((self.path, offset))
        self.send_response(206 if offset else 200)
        self.send_header("Content-Length", str(len(data) - offset))
        self.send_header("Accept-Ranges", "bytes")
        if offset:
            self.send_header("Content-Range", f"bytes {offset}-{len(data) - 1}/{len(data)}")
        self.end_headers()
        try:
            for start in range(offset, len(data), 16384):
                self.wfile.write(data[start:start + 16384])
                time.sleep(0.025)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def log_message(self, *_):
        pass


def run_fixture(work):
    load = worker.load_engine

    def load_fixture(path):
        fixture = json.loads((Path(work) / "request.json").read_text(encoding="utf-8"))["fixture"]

        class Fixture(load(path)):
            def extract_info(self, url, download=True):
                return self.process_ie_result({
                    "id": "jNQXAC9IVRw", "title": "Resume fixture", "url": fixture["url"],
                    "format_id": fixture["format_id"], "ext": "mp4", "height": 360,
                    "vcodec": "h264", "acodec": "aac", "extractor": "generic", "extractor_key": "Generic",
                }, download=download)

        return Fixture

    with patch.object(worker, "load_engine", load_fixture):
        return worker.run_worker(work)


class TransferTests(unittest.TestCase):
    def test_resume_keeps_same_stream_and_restarts_when_format_changes(self):
        engine = EngineManager(Path(__file__).resolve().parents[1] / "vendor" / "seed")
        engine.refresh()
        runtime = engine.snapshot()
        if not runtime.ready:
            self.skipTest("The Windows build prepares the verified media tools.")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "fixture.mp4"
            subprocess.run([Path(runtime.ffmpeg) / "ffmpeg.exe", "-v", "error", "-f", "lavfi",
                            "-i", "color=size=32x32:duration=0.2", "-pix_fmt", "yuv420p", media],
                           check=True, timeout=15, capture_output=True)
            clip = media.read_bytes()
            self.check_transfers(root, runtime, clip)

    def check_transfers(self, root, runtime, clip):
        with ThreadingHTTPServer(("127.0.0.1", 0), RangeServer) as server:
            server.payloads = {"/first": clip + b"A" * 1048576, "/second": clip + b"B" * 1048576}
            server.offsets = []
            Thread(target=server.serve_forever, daemon=True).start()
            script = Path(__file__).resolve()
            runner = "import runpy,sys; runpy.run_path(sys.argv.pop(1),run_name='__main__')"
            command = lambda flag, work: [sys.executable, "-c", runner, str(script), str(work)]
            try:
                with patch("yt_downloader.process.command", side_effect=command):
                    for different, legacy in ((False, False), (True, False), (False, True)):
                        with self.subTest(different=different, legacy=legacy):
                            self.check_resume(root / f"{different}-{legacy}", runtime, server, different, legacy)
            finally:
                server.shutdown()

    def check_resume(self, root, runtime, server, different, legacy):
        options = Options(folder=str(root / "downloads"), metadata=False, thumbnail=False,
                          template=LEGACY_TEMPLATE if legacy else DEFAULT_TEMPLATE)
        entry = Entry("jNQXAC9IVRw", "Resume fixture", "Autor", "Playlista", source_name="Playlista")
        request = {"operation": "download", "entry": asdict(entry),
                   "options": asdict(options), "job_id": 1, "runtime": asdict(runtime), "cache": False}
        folder = root / "downloads" / ("Autor/Playlista" if legacy else "Playlista")

        def launch(path, format_id):
            return Worker(root, {**request, "fixture": {
                "url": f"http://127.0.0.1:{server.server_port}{path}", "format_id": format_id,
            }})

        first = launch("/first", "18")
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and first.poll() is None:
                part = next(folder.glob("*.part"), None)
                if part and part.stat().st_size >= 65536:
                    break
                time.sleep(0.025)
            else:
                self.fail(f"Worker did not produce a partial file: {list(first.events())}")
        finally:
            first.close()

        start = len(server.offsets)
        path = "/second" if different else "/first"
        if legacy:
            # Model the persisted entry from before the new source folder field existed.
            request["entry"].pop("source_name")
        resumed = launch(path, "22" if different else "18")
        try:
            resumed.process.wait(timeout=25)
            events = list(resumed.events())
            self.assertEqual(resumed.poll(), 0, events)
            result = next(Path(event["path"]) for event in events if event["kind"] == "done")
            self.assertEqual(result.parent, folder)
            self.assertEqual(result.read_bytes(), server.payloads[path])
            self.assertFalse(list(result.parent.glob("*.yt-resume.json")))
            offsets = [offset for route, offset in server.offsets[start:] if route == path]
            self.assertTrue(offsets)
            self.assertEqual(any(offset > 0 for offset in offsets), not different)
        finally:
            resumed.close()


if __name__ == "__main__":
    if len(sys.argv) == 2:
        raise SystemExit(run_fixture(sys.argv[1]))
    unittest.main()
