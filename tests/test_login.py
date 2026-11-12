import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from yt_downloader.auth import crypt, youtube_cookie_file
from yt_downloader.controller import Controller, Running
from yt_downloader.model import Entry, parse_source
from yt_downloader.runtime import atomic_json
from yt_downloader.ui import Window
from yt_downloader.updates import Runtime
from yt_downloader.worker import base_parameters, run_worker


PRIVATE = "This playlist is private"
MISSING = "The playlist does not exist."
AGE = "This video is age-restricted and YouTube is requiring age verification."
MEMBERS = "Sign in or join this channel to get access to members-only content like this video."
PLAYLIST = "https://www.youtube.com/playlist?list=PLTest"
VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
COOKIES = (b"# Netscape HTTP Cookie File\n"
           b"#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\ttest\tsynthetic\n"
           b".example.org\tTRUE\t/\tTRUE\t0\tother\tignored\n")


class LoginWorkerTests(unittest.TestCase):
    def test_extractor_errors_reach_the_ui_as_login_events(self):
        for kind, url, error, code in (
            ("playlist", PLAYLIST, PRIVATE, "auth"),
            ("playlist", PLAYLIST, MISSING, "playlist_access"),
            ("video", VIDEO, AGE, "auth"),
            ("video", VIDEO, MEMBERS, "members"),
            ("video", VIDEO, "HTTP Error 403: Forbidden", "extractor"),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                atomic_json(root / "request.json", {
                    "operation": "scan", "source": {"kind": kind, "url": url},
                    "runtime": {"engine": "", "ffmpeg": "", "deno": ""}, "cache": "",
                })
                with (patch("yt_downloader.worker.load_engine") as load,
                      patch("yt_downloader.worker.tempfile.tempdir", tempfile.tempdir)):
                    load.return_value.return_value.__enter__.return_value.extract_info.side_effect = ValueError(error)
                    self.assertEqual(run_worker(root), 1)
                event = json.loads((root / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
                self.assertEqual((event["kind"], event["code"]), ("error", code))

    def test_imported_session_is_filtered_and_read_from_dpapi_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "cookies.dpapi"
            file.write_bytes(crypt(youtube_cookie_file(COOKIES)))
            self.assertNotIn(b"synthetic", file.read_bytes())
            params = base_parameters({
                "runtime": {"ffmpeg": "", "deno": ""}, "cache": "",
                "auth": {"file": str(file)},
            }, None)
            data = params["cookiefile"].getvalue()
            self.assertIn("#HttpOnly_.youtube.com", data)
            self.assertNotIn("example.org", data)

    def test_playlist_omits_known_members_without_renumbering_the_other_videos(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / "request.json", {
                "operation": "scan", "source": {"kind": "playlist", "url": PLAYLIST},
                "runtime": {"engine": "", "ffmpeg": "", "deno": ""}, "cache": "",
            })
            with (patch("yt_downloader.worker.load_engine") as load,
                  patch("yt_downloader.worker.tempfile.tempdir", tempfile.tempdir)):
                load.return_value.return_value.__enter__.return_value.extract_info.return_value = {
                    "title": "Playlista", "entries": [
                        {"id": "jNQXAC9IVRw", "availability": "subscriber_only"},
                        {"id": "BaW_jenozKc", "availability": "public"},
                    ],
                }
                self.assertEqual(run_worker(root), 0)
            event, done = map(json.loads, (root / "events.jsonl").read_text(encoding="utf-8").splitlines())
            self.assertEqual((event["entry"]["id"], event["entry"]["index"]), ("BaW_jenozKc", 2))
            self.assertEqual(done, {"kind": "done", "count": 1, "skipped_members": 1})


class LoginUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        with patch.object(Controller, "update"):
            self.controller = Controller(Path(work.name))
        self.controller.timer.stop()
        self.addCleanup(self.controller.close)
        self.controller.manager.current = Runtime("engine", "deno", "ffmpeg")
        self.window = Window(self.app, self.controller)
        self.addCleanup(self.window.deleteLater)

    def test_private_playlist_opens_its_url_and_retries_with_selected_browser(self):
        with (patch("yt_downloader.controller.Worker") as worker,
              patch("yt_downloader.ui.QDesktopServices.openUrl") as open_url):
            self.window.source.setText(PLAYLIST)
            self.window.preview()
            self.controller.finish_scan({"kind": "error", "code": "auth", "message": PRIVATE})
            self.assertEqual(open_url.call_args.args[0].toString(), PLAYLIST)
            self.window.retry_after_login()
            self.assertEqual(worker.call_count, 1)
            self.assertFalse(self.window.retry_login.isHidden())
            self.window.source.setText(VIDEO)
            self.controller.prefs.browser = "firefox"
            self.controller.prefs.browser_profile = "test-profile"
            self.window.retry_after_login()
            request = worker.call_args.args[1]
            self.assertEqual(request["source"]["url"], PLAYLIST)
            self.assertEqual(request["auth"]["browser"], "firefox")
            self.assertEqual(request["auth"]["profile"], "test-profile")

    def test_channel_retry_preserves_the_tabs_selected_at_submission(self):
        with (patch("yt_downloader.controller.Worker") as worker,
              patch("yt_downloader.ui.QDesktopServices.openUrl")):
            source = parse_source("https://www.youtube.com/@test")
            self.window.start_preview(source, ["videos", "streams"])
            self.controller.finish_scan({"kind": "error", "code": "auth", "message": "Zaloguj się."})
            self.window.channel_tabs["shorts"].setChecked(True)
            self.controller.prefs.browser = "firefox"
            self.window.retry_after_login()
            self.assertEqual(worker.call_args.args[1]["tabs"], ("videos", "streams"))

    def test_ambiguous_playlist_error_offers_login_without_opening_the_browser(self):
        with (patch("yt_downloader.controller.Worker"),
              patch("yt_downloader.ui.QDesktopServices.openUrl") as open_url):
            self.window.source.setText(PLAYLIST)
            self.window.preview()
            self.controller.finish_scan({"kind": "error", "code": "playlist_access", "message": MISSING})
            open_url.assert_not_called()
            self.assertFalse(self.window.retry_login.isHidden())

    def test_age_restricted_job_waits_for_login_and_allows_other_jobs_to_start(self):
        c = self.controller
        c.store.enqueue([Entry("jNQXAC9IVRw", "18+"), Entry("BaW_jenozKc", "Publiczny")], c.prefs.options)
        job = c.store.claim()
        with patch("yt_downloader.ui.QDesktopServices.openUrl") as open_url:
            result = {"kind": "error", "code": "auth", "message": AGE}
            c.finish_job(Running(None, job, "", result))
            self.assertEqual(c.store.jobs()[0].state, "auth")
            self.assertEqual(open_url.call_args.args[0].toString(), VIDEO)
        self.assertEqual(c.store.claim().entry.title, "Publiczny")
        c.prefs.browser = "firefox"
        self.window.retry_after_login()
        c.finish_job(Running(None, c.store.claim(), "", result))
        self.assertEqual(c.store.jobs()[0].state, "failed")

    def test_members_job_is_skipped_without_opening_a_browser_or_marking_it_downloaded(self):
        c = self.controller
        entry = Entry("jNQXAC9IVRw", "Dla wspierających")
        c.store.enqueue([entry], c.prefs.options)
        result = {"kind": "error", "code": "members", "message": MEMBERS}
        with patch("yt_downloader.ui.QDesktopServices.openUrl") as open_url:
            c.finish_job(Running(None, c.store.claim(), "", result))
            self.assertEqual(c.store.jobs()[0].state, "skipped")
            self.assertEqual(c.store.history(), [])
            c.prefs.skip_members = False
            c.store.enqueue([entry], c.prefs.options)
            c.finish_job(Running(None, c.store.claim(), "", result))
            self.assertEqual(c.store.jobs()[-1].state, "auth")
            open_url.assert_not_called()


if __name__ == "__main__":
    unittest.main()
