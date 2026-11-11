from dataclasses import asdict
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtWidgets import QApplication

from yt_downloader.controller import Controller
from yt_downloader.model import Entry, parse_source
from yt_downloader.ui import Window
from yt_downloader.updates import Runtime
from yt_downloader.worker import scan


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
        self.assertEqual(settings["retry_sleep_functions"]["extractor"](3), 4)
        self.assertEqual(settings["extractor_args"]["youtube"]["raise_incomplete_data"], ["1"])
        self.assertEqual([values["entry"]["index"] for kind, values in events if kind == "entry"],
                         list(range(1, 351)))
        self.assertEqual(events[-1], ("done", {"count": 350}))


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


if __name__ == "__main__":
    unittest.main()
