from dataclasses import asdict
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication, QDialog

from yt_downloader.controller import Controller
from yt_downloader.model import Entry, parse_source
from yt_downloader.ui import PreviewOptionsDialog, Window
from yt_downloader.updates import EngineManager, Runtime
from yt_downloader.worker import load_engine, scan


PLAYLIST = "https://www.youtube.com/playlist?list=PLTest"


def entry(number):
    return Entry(f"v{number:010}", f"Film {number}", "Kanał", "Playlista", number)


class PaginationWorkerTests(unittest.TestCase):
    def test_continuations_are_consumed_incrementally_and_keep_original_order(self):
        events = []

        def pages():
            for first, end in [(1, 101), (101, 201), (201, 351)]:
                self.assertEqual(len(events), first - 1)
                for number in range(first, end):
                    yield {"id": entry(number).id, "title": f"Film {number}"}

        client = MagicMock()
        client.extract_info.return_value = {"_type": "playlist", "entries": pages(),
                                            "title": "Playlista", "channel": "Kanał"}
        ydl = MagicMock()
        ydl.return_value.__enter__.return_value = client
        scan(ydl, {"source": asdict(parse_source(PLAYLIST))}, {},
             lambda kind, **values: events.append((kind, values)))

        client.extract_info.assert_called_once_with(PLAYLIST, download=False, process=False)
        settings = ydl.call_args.args[0]
        self.assertEqual(settings["extractor_retries"], 3)
        self.assertEqual(settings["retry_sleep_functions"]["extractor"](n=2), 4)
        self.assertEqual(settings["extractor_args"]["youtube"]["raise_incomplete_data"], ["1"])
        self.assertEqual([values["entry"]["index"] for kind, values in events if kind == "entry"],
                         list(range(1, 351)))
        self.assertEqual(events[-1], ("done", {"count": 350}))


class PaginationEngineTests(unittest.TestCase):
    def test_verified_engine_retries_errors_and_rejects_an_incomplete_page(self):
        root = Path(__file__).resolve().parents[1] / "vendor" / "seed"
        engine = EngineManager(root)
        engine.refresh()
        if not engine.snapshot().engine:
            self.skipTest("The Windows build prepares the verified engine.")

        capture = MagicMock()
        capture.return_value.__enter__.return_value.extract_info.return_value = {"entries": []}
        scan(capture, {"source": asdict(parse_source(PLAYLIST))}, {}, lambda *args, **kwargs: None)
        settings = capture.call_args.args[0]
        retry = settings["retry_sleep_functions"]["extractor"]
        delays = []

        def record_retry(*, n):
            delays.append(retry(n=n))
            return 0

        settings["retry_sleep_functions"]["extractor"] = record_retry
        settings.update(quiet=True, plugin_dirs=[], logger=MagicMock())
        with (patch.object(sys, "path", sys.path.copy()), patch.object(sys, "dont_write_bytecode")):
            ydl = load_engine(engine.snapshot().engine)
            from yt_dlp.networking import Response
            from yt_dlp.networking.exceptions import HTTPError
            from yt_dlp.utils import ExtractorError

            with ydl(settings) as client:
                tab = client.get_info_extractor("YoutubeTab")
                response = Response(BytesIO(), PLAYLIST, headers={}, status=503)
                fault = ExtractorError("HTTP 503", cause=HTTPError(response))
                page = {"continuationContents": {"items": ["page result"]}}
                with patch.object(tab, "_call_api", side_effect=[fault, {}, page]) as api:
                    result = tab._extract_response("fixture page 2", {}, check_get_keys="continuationContents")
                    self.assertEqual(result, page)
                    self.assertEqual(api.call_count, 3)
                    self.assertEqual(delays, [1, 1])
                with patch.object(tab, "_call_api", return_value={}) as api:
                    with self.assertRaisesRegex(ExtractorError, "Incomplete data received"):
                        tab._extract_response("fixture page 3", {}, check_get_keys="continuationContents")
                    self.assertEqual(api.call_count, 4)


class PaginationPreviewTests(unittest.TestCase):
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
        workers = patch("yt_downloader.controller.Worker")
        self.worker = workers.start().return_value
        self.addCleanup(workers.stop)
        self.window.source.setText(PLAYLIST)
        self.window.preview()

    def send_page(self, first, end, outcome=None):
        events = [{"kind": "entry", "entry": asdict(entry(number))} for number in range(first, end)]
        self.worker.poll.return_value = None if outcome is None else 1
        self.worker.events.return_value = events + ([outcome] if outcome else [])
        self.controller.poll_scan()

    def test_failed_page_keeps_visible_rows_and_allows_partial_enqueue(self):
        self.send_page(1, 101)
        self.assertEqual(self.window.preview_model.rowCount(), 100)
        self.assertFalse(self.window.add_button.isEnabled())
        self.send_page(101, 151, {"kind": "error", "code": "network", "message": "Błąd sieci."})
        self.assertEqual(self.window.preview_model.rowCount(), 150)
        self.assertIn("Niepełna lista", self.window.preview_info.text())
        self.assertTrue(self.window.add_button.isEnabled())
        self.window.enqueue()
        self.assertEqual([job.entry.index for job in self.controller.store.jobs()], list(range(1, 151)))

    def test_cancel_keeps_rows_from_completed_pages(self):
        self.send_page(1, 101)
        self.controller.cancel_scan()
        self.assertEqual(self.window.preview_model.rowCount(), 100)
        self.assertTrue(self.window.add_button.isEnabled())
        self.assertIn("Odczyt anulowany", self.window.preview_info.text())

    def test_unsuccessful_engine_retry_preserves_the_longer_preview(self):
        self.send_page(1, 151, {"kind": "error", "code": "extractor", "message": "Błąd silnika."})
        self.controller.pending_repairs.clear()
        self.controller.start_scan_worker()
        self.send_page(1, 31, {"kind": "error", "code": "network", "message": "Błąd sieci."})
        self.assertEqual(self.window.preview_model.rowCount(), 150)
        self.assertIn("Niepełna lista", self.window.preview_info.text())

    def test_large_subscription_batch_keeps_order_and_skips_duplicates(self):
        store = self.controller.store
        options = self.controller.prefs.options
        self.assertEqual(store.enqueue((entry(i) for i in range(1, 5001)), options, only_new=True), 5000)
        self.assertEqual(store.enqueue((entry(i) for i in range(4990, 5003)), options, only_new=True), 2)
        self.assertEqual([job.entry.index for job in store.jobs()], list(range(1, 5003)))

    def test_filter_adds_only_visible_checked_rows(self):
        self.send_page(1, 21, {"kind": "done"})
        self.window.preview_search.setText("film 2")
        self.assertEqual(self.window.preview_filter.rowCount(), 2)
        self.window.select_visible(False)
        self.window.select_visible(True)
        self.window.enqueue()
        self.assertEqual([job.entry.index for job in self.controller.store.jobs()], [2, 20])
        self.assertIn(0, self.window.preview_model.selected)
        self.assertNotIn(1, self.window.preview_model.selected)

    def test_music_preview_defaults_to_audio_without_changing_global_settings(self):
        self.controller.cancel_scan()
        self.window.source.setText("https://music.youtube.com/playlist?list=PLMusic")
        self.window.preview()
        self.send_page(1, 3, {"kind": "done"})
        self.assertIs(self.window.preview_mode.currentData(), True)
        self.window.enqueue()
        self.assertTrue(all(job.options.audio_only for job in self.controller.store.jobs()))
        self.assertFalse(self.controller.prefs.options.audio_only)

    def test_batches_from_the_same_preview_keep_their_own_parameters(self):
        self.send_page(1, 4, {"kind": "done"})
        defaults = self.controller.prefs.options

        def customize(dialog):
            dialog.form.resolution.setCurrentIndex(dialog.form.resolution.findData(480))
            dialog.form.container.setCurrentIndex(dialog.form.container.findData("mkv"))
            dialog.form.subs.setChecked(True)
            dialog.form.sub_lang.setText("en")
            dialog.form.embed_subs.setChecked(True)
            dialog.accept()
            return dialog.result()

        with patch.object(PreviewOptionsDialog, "exec", customize):
            self.window.preview_parameters.click()
        self.window.preview_search.setText("film 1")
        self.window.add_button.click()

        def reset(dialog):
            dialog.reset()
            dialog.accept()
            return dialog.result()

        with patch.object(PreviewOptionsDialog, "exec", reset):
            self.window.preview_parameters.click()
        self.window.preview_search.clear()
        self.window.add_button.click()

        first, *remaining = self.controller.store.jobs()
        self.assertEqual(first.options.resolution, 480)
        self.assertEqual(first.options.container, "mkv")
        self.assertTrue(first.options.subtitles and first.options.embed_subtitles)
        self.assertEqual(first.options.subtitle_languages, "en")
        self.assertEqual([job.options for job in remaining], [defaults, defaults])
        self.assertEqual(self.controller.prefs.options, defaults)
        with patch.object(PreviewOptionsDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            self.window.preview_parameters.click()
        self.assertEqual(self.window.options_for_preview(), defaults)

    def test_small_preview_window_gives_the_table_its_own_tab(self):
        self.send_page(1, 21, {"kind": "done"})
        self.window.resize(800, 500)
        self.window.show()
        self.addCleanup(self.window.hide)
        self.app.processEvents()
        self.assertEqual(self.window.height(), 500)
        self.assertIs(self.window.tabs.currentWidget(), self.window.preview_tab)
        self.assertFalse(self.window.queue_table.isVisible())
        table = self.window.preview_table
        self.assertGreaterEqual(table.rowAt(table.viewport().height() - 1), 7)

    def test_imported_archive_skips_a_playlist_entry_and_preserves_force(self):
        archive = self.controller.root / "pobrane.txt"
        archive.write_text(f"youtube {entry(1).id}\nyoutube {entry(1).id}\nniepoprawny wiersz\n",
                           encoding="utf-8-sig")
        self.window.tabs.setCurrentWidget(self.window.history_tab)
        with patch("yt_downloader.ui.QFileDialog.getOpenFileName", return_value=(str(archive), "")):
            self.window.import_history()
        self.assertEqual(self.window.history.rows[0][:2], ("Z archiwum yt-dlp", entry(1).id))
        self.assertIn("już w historii: 1", self.window.notice.text())
        self.assertIn("nierozpoznane wiersze: 1", self.window.notice.text())

        self.window.tabs.setCurrentWidget(self.window.preview_tab)
        self.send_page(1, 3, {"kind": "done"})
        self.window.add_button.click()
        self.assertEqual([job.state for job in self.controller.store.jobs()], ["skipped", "queued"])

        def force_download(dialog):
            dialog.form.force.setChecked(True)
            dialog.accept()
            return dialog.result()

        self.window.preview_search.setText("film 1")
        self.window.select_visible(True)
        with patch.object(PreviewOptionsDialog, "exec", force_download):
            self.window.preview_parameters.click()
        self.window.add_button.click()
        job = self.controller.store.jobs()[-1]
        self.assertEqual(job.state, "queued")
        self.assertTrue(job.options.force)


if __name__ == "__main__":
    unittest.main()
