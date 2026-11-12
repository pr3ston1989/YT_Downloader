from dataclasses import asdict, replace
from io import BytesIO
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QItemSelectionModel
from PySide6.QtWidgets import QApplication, QDialog

from yt_downloader.controller import Controller
from yt_downloader.model import Entry, Options, detect_episode_numbers, output_stem, parse_source
from yt_downloader.ui import MediaDetailsDialog, OrganizeDialog, PreviewOptionsDialog, Window
from yt_downloader.updates import EngineManager, Runtime
from yt_downloader.worker import format_expression, inspect_media, load_engine, scan


PLAYLIST = "https://www.youtube.com/playlist?list=PLTest"


def entry(number):
    return Entry(f"v{number:010}", f"Film {number}", "Kanał", "Playlista", number)


class PaginationWorkerTests(unittest.TestCase):
    def test_media_inspection_summarizes_only_downloadable_streams_and_distinguishes_captions(self):
        ydl = MagicMock()
        client = ydl.return_value.__enter__.return_value
        client.extract_info.return_value = {
            "formats": [
                {"url": "https://fixture.test/1", "height": 1080, "fps": 60, "vcodec": "vp9", "ext": "webm"},
                {"url": "https://fixture.test/2", "height": 1080, "fps": 60, "vcodec": "vp9", "ext": "webm"},
                {"url": "https://fixture.test/3", "language": "pl", "acodec": "opus", "abr": 128, "ext": "webm"},
                {"url": "https://fixture.test/4", "height": 2160, "vcodec": "av1", "has_drm": True},
            ],
            "subtitles": {"pl": [{"name": "Polski"}]},
            "automatic_captions": {"en": [{"name": "English"}]},
        }
        emit = MagicMock()
        inspect_media(ydl, {"entry": asdict(entry(1))}, {}, emit)
        client.extract_info.assert_called_once_with(entry(1).url, download=False)
        emit.assert_called_once_with("done", video=[(1080, 60, "vp9", "webm")],
                                     audio=[("pl", "opus", 128, "webm")],
                                     subtitles=[("pl", "Polski", False), ("en", "English", True)])

    def test_episode_detection_rejects_years_and_isolated_numbers(self):
        entries = [Entry(entry(i).id, title) for i, title in enumerate((
            "Podsumowanie | 2023", "Koncert 2024", "Zapowiedź #4", "Dokument (2025)", "Powrót 1999"), 1)]
        self.assertEqual(detect_episode_numbers(entries), {})

    def test_single_title_requires_an_episode_marker_or_distinct_separator(self):
        for title, expected in (
            ("Tytuł | 450.", {0: (0, 450)}),
            ("Odc. 23 – Tytuł", {0: (0, 23)}),
            ("Tytuł S02E07", {0: (2, 7)}),
            ("Tytuł 450", {}),
            ("Tytuł (450)", {}),
            ("Tytuł | 2025", {}),
        ):
            with self.subTest(title=title):
                self.assertEqual(detect_episode_numbers([Entry(entry(1).id, title)]), expected)

    def test_continuations_are_consumed_incrementally_and_keep_original_order(self):
        events = []

        def pages():
            for first, end in [(1, 101), (101, 201), (201, 351)]:
                self.assertEqual(len(events), first - 1)
                for number in range(first, end):
                    yield {"id": entry(number).id, "title": f"Film {number}", "timestamp": number * 1000,
                           "channel": f"Autor {number % 4}"}

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
        self.assertEqual(settings["extractor_args"]["youtubetab"]["approximate_date"], ["1"])
        self.assertEqual([values["entry"]["index"] for kind, values in events if kind == "entry"],
                         list(range(1, 351)))
        self.assertEqual(events[-1], ("done", {"count": 350}))
        self.assertEqual(events[-2][1]["entry"]["published_at"], 350000)
        entries = [Entry(**values["entry"]) for kind, values in events if kind == "entry"]
        self.assertEqual(len({item.channel for item in entries}), 4)
        options = Options()
        self.assertEqual({output_stem(item, options, 1).parent.name for item in entries}, {"Playlista"})

    def test_channel_tabs_share_the_channel_folder(self):
        client = MagicMock()
        client.extract_info.side_effect = [
            {"title": "Właściciel - Filmy", "channel": "Właściciel",
             "entries": [{"id": entry(1).id, "channel": "Autor filmu"}]},
            {"title": "Właściciel - Shorts", "entries": [{"id": entry(2).id, "channel": "Autor shorta"}]},
            {"title": "Właściciel - Transmisje", "uploader": "Właściciel",
             "entries": [{"id": entry(3).id, "channel": "Autor transmisji"}]},
        ]
        ydl = MagicMock()
        ydl.return_value.__enter__.return_value = client
        events = []
        source = parse_source("https://www.youtube.com/@wlasciciel")
        scan(ydl, {"source": asdict(source), "tabs": ["videos", "shorts", "streams"]}, {},
             lambda kind, **values: events.append((kind, values)))
        entries = [Entry(**values["entry"]) for kind, values in events if kind == "entry"]
        self.assertEqual([item.tab for item in entries], ["videos", "shorts", "streams"])
        self.assertEqual({item.source_name for item in entries}, {"Właściciel"})
        self.assertEqual({output_stem(item, Options(), 1).parent.name for item in entries}, {"Właściciel"})


class PaginationEngineTests(unittest.TestCase):
    def test_verified_engine_respects_an_exact_audio_language(self):
        engine = EngineManager(Path(__file__).resolve().parents[1] / "vendor" / "seed")
        engine.refresh()
        if not engine.snapshot().engine:
            self.skipTest("The Windows build prepares the verified engine.")
        with (patch.object(sys, "path", sys.path.copy()), patch.object(sys, "dont_write_bytecode")):
            ydl = load_engine(engine.snapshot().engine)
            with ydl({"quiet": True, "plugin_dirs": []}) as client:
                formats = [{"format_id": code, "language": code, "acodec": "opus", "vcodec": "none"}
                           for code in ("en-US", "en", "pl")]
                context = {"formats": formats, "incomplete_formats": True}
                options = Options(audio_only=True, audio_language="en-US", audio_language_exact=True)
                selector = client.build_format_selector(format_expression(options))
                self.assertEqual([fmt["format_id"] for fmt in selector(context)], ["en-US"])
                self.assertEqual(list(selector({**context, "formats": formats[1:]})), [])

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

    def test_organizing_from_the_playlist_preview_waits_for_confirmation(self):
        self.send_page(1, 3, {"kind": "done"})
        root = (self.controller.root / "downloads").resolve()
        original = root / "Stary kanał" / f"Film [{entry(1).id}].mp4"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"fixture")
        with patch("yt_downloader.ui.QFileDialog.getExistingDirectory", return_value=str(root)):
            with patch.object(OrganizeDialog, "exec", return_value=QDialog.DialogCode.Rejected):
                self.window.organize_button.click()
            self.assertTrue(original.is_file())
            with patch.object(OrganizeDialog, "exec", return_value=QDialog.DialogCode.Accepted):
                self.window.organize_button.click()
        target = root / "Playlista" / original.name
        self.assertTrue(target.is_file())
        self.assertEqual(self.controller.store.history()[0]["path"], str(target))

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

    def test_preview_can_add_a_visible_selection_to_the_front_of_the_queue(self):
        store = self.controller.store
        store.enqueue([entry(i) for i in range(100, 200)], self.controller.prefs.options)
        self.send_page(1, 21, {"kind": "done"})
        self.window.preview_search.setText("film 2")
        self.window.select_visible(False)
        self.window.preview_model.select_rows([19], True)
        self.window.add_first.click()
        self.assertEqual([job.entry.index for job in store.jobs()[:3]], [20, 100, 101])
        self.assertIs(self.window.tabs.currentWidget(), self.window.queue_tab)

    def test_queue_keeps_the_selected_job_when_moved_to_the_front(self):
        store = self.controller.store
        store.enqueue([entry(i) for i in range(1, 5)], self.controller.prefs.options)
        self.window.tabs.setCurrentWidget(self.window.queue_tab)
        self.window.queue_table.selectRow(3)
        self.window.move_first.click()
        self.assertEqual([job.id for job in store.jobs()], [4, 1, 2, 3])
        self.assertEqual([job.id for job in self.window.selected_jobs()], [4])
        self.assertFalse(self.window.move_first.isEnabled())
        self.window.move_down.click()
        self.assertEqual([job.id for job in store.jobs()], [1, 4, 2, 3])

    def test_collector_reads_links_and_applies_folder_per_source_without_rescanning(self):
        self.send_page(1, 1, {"kind": "done"})
        collector = self.window.collector_tab
        collector.input.setPlainText(f"{PLAYLIST}\nhttps://youtu.be/{entry(2).id}\n{PLAYLIST}")
        collector.read.click()
        self.assertEqual(len(collector.items), 2)
        self.assertIn("Powtórzone: 1", collector.notice.text())
        self.send_page(1, 3, {"kind": "done"})
        collector.start_next()
        self.send_page(2, 3, {"kind": "done"})
        collector.table.selectRow(0)
        folder = str(self.controller.root.resolve() / "Folder pakietu")
        with patch("yt_downloader.collector.QFileDialog.getExistingDirectory", return_value=folder):
            collector.folder.click()
        collector.preview.click()
        self.assertEqual(self.window.options_for_preview().folder, folder)
        self.assertIsNone(self.controller.scan)
        collector.table.selectAll()
        collector.add.click()
        jobs = self.controller.store.jobs()
        self.assertEqual([job.entry.id for job in jobs], [entry(1).id, entry(2).id])
        self.assertTrue(all(job.options.folder == folder for job in jobs))
        self.assertEqual(collector.items[1].status, "Dodano: 0 · Powtórzenia: 1")

    def test_dragging_a_package_updates_the_next_runnable_download(self):
        store = self.controller.store
        _, a = store.enqueue_batch([entry(1), entry(2)], self.controller.prefs.options, package_name="Playlista A")
        _, b = store.enqueue_batch([entry(3)], self.controller.prefs.options, package_name="Playlista B")
        self.window.tabs.setCurrentWidget(self.window.queue_tab)
        self.assertIn("Film 1", self.window.next_download.text())
        packages = store.packages()
        self.window.reorder_queue_drop([packages[b]], packages[a], False)
        self.assertEqual([job.entry.id for job in store.jobs()], [entry(i).id for i in (3, 1, 2)])
        self.assertIn("Film 3", self.window.next_download.text())
        self.controller.pause_package(b, True)
        self.assertIn("Film 1", self.window.next_download.text())

    def test_relinking_missing_history_updates_the_job_and_redownload_preserves_its_profile(self):
        store = self.controller.store
        options = replace(self.controller.prefs.options, audio_only=True)
        store.enqueue([entry(1)], options)
        old_path = str(self.controller.root.resolve() / "stary.mp3")
        store.complete(store.claim(), old_path)
        self.window.tabs.setCurrentWidget(self.window.history_tab)
        self.window.history_table.selectRow(0)
        record = self.window.history_selection()[0]
        new_path = self.controller.root.resolve() / "nowy.mp3"
        new_path.write_bytes(b"fixture")
        with patch("yt_downloader.ui.QFileDialog.getOpenFileName", return_value=(str(new_path), "")):
            self.window.relink_history(record)
        self.assertEqual(store.history()[0]["path"], str(new_path))
        self.assertEqual(store.jobs()[0].path, str(new_path))
        self.window.redownload_history(self.window.history_selection())
        self.assertTrue(store.jobs()[-1].options.audio_only)
        self.assertTrue(store.jobs()[-1].options.force)

    def test_preview_packages_can_be_collapsed_paused_and_reordered(self):
        store = self.controller.store
        self.send_page(1, 4, {"kind": "done"})
        self.window.add_button.click()
        playlist = next(iter(store.packages().values()))
        self.assertEqual(playlist.name, "Playlista")
        _, channel = store.enqueue_batch([entry(4), entry(5)], self.controller.prefs.options, package_name="Kanał")
        self.window.tabs.setCurrentWidget(self.window.queue_tab)
        self.window.queue_table.selectRow(0)
        self.window.toggle_package_at(self.window.queue_filter.index(0, 0))
        self.assertEqual(store.get("collapsed_packages"), [playlist.id])
        self.assertEqual(self.window.queue_filter.rowCount(), 4)
        self.assertEqual(self.window.selected_package().id, playlist.id)
        self.window.pause_selected.click()
        self.assertTrue(store.packages()[playlist.id].paused)
        self.window.move_last.click()
        self.assertEqual([job.package_id for job in store.jobs()], [channel, channel] + [playlist.id] * 3)
        self.window.queue_search.setText("film 2")
        self.assertEqual(self.window.queue_filter.rowCount(), 1)
        self.window.queue_table.selectRow(0)
        self.window.resume_selected.click()
        self.assertFalse(store.packages()[playlist.id].paused)
        self.window.toggle_package_at(self.window.queue_filter.index(0, 0))
        self.assertEqual(self.window.queue_filter.rowCount(), 2)

    def test_history_file_menu_reveals_and_moves_multiple_selected_downloads(self):
        store = self.controller.store
        source = self.controller.root.resolve() / "Oryginalne pliki"
        target = self.controller.root.resolve() / "Nowe pliki"
        source.mkdir()
        target.mkdir()
        files = [source / "Pierwszy film.mkv", source / "Drugi film.mkv"]
        store.enqueue([entry(1), entry(2)], self.controller.prefs.options)
        for path in files:
            path.write_bytes(b"film")
            store.complete(store.claim(), str(path))
        self.window.tabs.setCurrentWidget(self.window.history_tab)
        view = self.window.history_table
        view.selectRow(0)
        self.assertTrue(self.window.show_history_file.isEnabled())
        with patch("yt_downloader.ui.subprocess.Popen") as explorer:
            self.window.show_history_file.click()
        selected_path = self.window.history_selection()[0][2]
        self.assertIn(f'/select,"{selected_path}"', explorer.call_args.args[0])
        view.selectAll()
        self.window.refresh()
        self.assertEqual(len(self.window.history_selection()), 2)
        records = self.window.history_selection()
        with patch("yt_downloader.ui.QFileDialog.getExistingDirectory", return_value=str(target)):
            with patch.object(OrganizeDialog, "exec", return_value=QDialog.DialogCode.Rejected):
                self.window.move_selected_downloads(records)
            self.assertTrue(all(path.is_file() for path in files))
            with patch.object(OrganizeDialog, "exec", return_value=QDialog.DialogCode.Accepted):
                self.window.move_selected_downloads(records)
        self.assertEqual(self.controller.file_move.result(timeout=5), (2, 0, 0))
        self.controller.poll_file_move()
        self.assertEqual({Path(path).parent for _, _, path in self.window.history_selection()}, {target})
        self.assertTrue(all((target / path.name).is_file() for path in files))

    def test_batch_edit_and_selected_retry_keep_individual_options_and_queue_positions(self):
        store = self.controller.store
        base = self.controller.prefs.options
        store.enqueue([entry(1)], base)
        store.enqueue([entry(2)], replace(base, resolution=720))
        store.enqueue([entry(3)], base)
        for job_id in (1, 2, 3):
            store.state(job_id, "failed", "Błąd testowy", attempts=2)
        self.window.tabs.setCurrentWidget(self.window.queue_tab)
        selection = self.window.queue_table.selectionModel()
        for row in (0, 1):
            selection.select(self.window.queue_filter.index(row, 0), QItemSelectionModel.SelectionFlag.Select |
                             QItemSelectionModel.SelectionFlag.Rows)

        def edit(dialog):
            self.assertEqual(self.controller.editing_jobs, {1, 2})
            dialog.form.container.setCurrentIndex(dialog.form.container.findData("mkv"))
            dialog.accept()
            return dialog.result()

        with patch.object(PreviewOptionsDialog, "exec", edit):
            self.window.edit_selected.click()
        self.window.retry_selected.click()
        jobs = store.jobs()
        self.assertEqual([job.options.container for job in jobs], ["mkv", "mkv", "mp4"])
        self.assertEqual(jobs[1].options.resolution, 720)
        self.assertEqual([job.state for job in jobs], ["queued", "queued", "failed"])
        self.assertEqual([job.attempts for job in jobs], [0, 0, 2])
        self.assertEqual([job.id for job in jobs], [1, 2, 3])

    def test_format_details_request_the_highlighted_video_without_replacing_the_preview(self):
        self.send_page(1, 4, {"kind": "done"})
        self.window.preview_table.selectRow(1)

        def inspect(dialog):
            self.worker.events.return_value = [{"kind": "done", "video": [[1080, 60, "vp9", "webm"]],
                                                "audio": [["pl", "opus", 128, "webm"]],
                                                "subtitles": [["en", "English", False], ["en", "English", True]]}]
            self.controller.poll_media_inspection()
            self.assertEqual(dialog.models[0].rows[0][:2], ("1080p", "60"))
            self.assertEqual(dialog.models[2].rows[1], ("en", "English", "Automatyczne"))
            dialog.resolution.setCurrentIndex(1)
            dialog.language.setCurrentIndex(1)
            dialog.captions.setCurrentIndex(3)
            dialog.apply.click()
            return dialog.result()

        with (patch.object(MediaDetailsDialog, "exec", inspect),
              patch("yt_downloader.controller.Worker", return_value=self.worker) as worker):
            self.window.media_details.click()
        self.assertEqual(worker.call_args.args[1]["entry"]["id"], entry(2).id)
        self.assertEqual(self.window.preview_model.rowCount(), 3)
        self.assertIsNone(self.controller.media_worker)
        self.window.add_first.click()
        first, customized, third = self.controller.store.jobs()
        self.assertEqual([job.entry.id for job in (first, customized, third)], [entry(i).id for i in range(1, 4)])
        self.assertEqual(customized.options.audio_language, "pl")
        self.assertTrue(customized.options.audio_language_exact and customized.options.auto_subtitles_only)
        self.assertEqual(customized.options.subtitle_languages, "en")
        self.assertEqual(first.options, third.options)
        self.assertFalse(first.options.subtitles)

    def test_queue_search_and_status_filter_keep_actions_on_the_visible_job(self):
        store = self.controller.store
        store.enqueue([entry(i) for i in range(1, 25)], self.controller.prefs.options)
        store.state(2, "failed")
        store.state(20, "failed")
        self.window.tabs.setCurrentWidget(self.window.queue_tab)
        self.window.queue_search.setText(" FILM 2 ")
        self.window.queue_status.setCurrentIndex(self.window.queue_status.findData("failed"))
        self.assertEqual(self.window.queue_filter.rowCount(), 2)
        self.window.queue_table.selectRow(1)
        self.assertEqual([job.id for job in self.window.selected_jobs()], [20])
        self.window.retry_selected.click()
        self.assertEqual(self.window.queue_filter.rowCount(), 1)
        self.assertEqual(self.window.queue_filter.index(0, 0).data(), "Film 2")
        self.assertEqual([job.id for job in store.jobs() if job.state == "failed"], [2])

    def test_download_profile_persists_and_applies_without_changing_default_options(self):
        self.send_page(1, 4, {"kind": "done"})
        original = self.controller.prefs.options
        profile = replace(original, folder=str(self.controller.root / "Podcast"), audio_only=True,
                          audio_format="opus", bitrate=96)
        self.window.preview_options = profile
        self.window.preview_mode.setCurrentIndex(self.window.preview_mode.findData(True))
        with patch("yt_downloader.ui.QInputDialog.getText", return_value=("  Podcast  ", True)):
            self.window.save_download_profile()
        self.assertEqual(self.window.active_profile, "Podcast")
        other_window = Window(self.app, self.controller)
        self.addCleanup(other_window.deleteLater)
        other_window.build_profiles_menu()
        next(action for action in other_window.profiles_menu.actions() if action.text() == "Podcast").trigger()
        self.assertEqual(other_window.options_for_preview(), profile)
        self.window.add_button.click()
        self.assertTrue(all(job.options == profile for job in self.controller.store.jobs()))
        self.assertEqual(self.controller.prefs.options, original)

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
        visible = sum(table.visualRect(table.model().index(row, 0)).intersects(table.viewport().rect())
                      for row in range(table.model().rowCount()))
        self.assertGreaterEqual(visible, 8)

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

    def test_chronological_order_keeps_selection_and_uses_positions_in_the_file_names(self):
        self.worker.poll.return_value = 0
        self.worker.events.return_value = [
            {"kind": "entry", "entry": asdict(replace(entry(number), published_at=date))}
            for number, date in enumerate((300, None, 100, 200), 1)
        ] + [{"kind": "done"}]
        self.controller.poll_scan()
        self.window.preview_model.select_rows([0], False)
        order = self.window.preview_order
        order.setCurrentIndex(order.findData("oldest"))
        self.assertEqual(list(self.window.visible_rows()), [2, 3, 0, 1])
        self.assertIn("Pozycje bez daty (1)", self.window.notice.text())
        order.setCurrentIndex(order.findData("newest"))
        self.assertEqual(list(self.window.visible_rows()), [0, 3, 2, 1])
        order.setCurrentIndex(order.findData("oldest"))
        self.window.add_button.click()
        jobs = self.controller.store.jobs()
        self.assertEqual([job.entry.title for job in jobs], ["Film 3", "Film 4", "Film 2"])
        self.assertEqual([job.entry.index for job in jobs], [1, 2, 4])
        self.assertTrue(output_stem(jobs[0].entry, jobs[0].options, jobs[0].id).name.startswith("01 - Film 3"))

    def test_reverse_order_preserves_numbers_after_filtering(self):
        self.send_page(1, 102, {"kind": "done"})
        order = self.window.preview_order
        order.setCurrentIndex(order.findData("reverse"))
        self.assertEqual(list(self.window.visible_rows()), list(range(100, -1, -1)))
        self.window.preview_search.setText("Film 100")
        self.window.add_button.click()
        job, = self.controller.store.jobs()
        self.assertEqual(job.entry.index, 2)
        self.assertTrue(output_stem(job.entry, job.options, job.id).name.startswith("002 - Film 100"))

    def test_episode_order_uses_recognized_numbers_anywhere_in_titles(self):
        titles = ["Sprawa na wybrzeżu | 451.", "Odc. 45 — Nowy trop", "Materiał specjalny",
                  "Inny trop #104 Kryminatorium", "Dalsza historia | 284. VIDEO PODCAST",
                  "S02E07 Gość sezonu", "Raport (2025)"]
        self.worker.poll.return_value = 0
        self.worker.events.return_value = [
            {"kind": "entry", "entry": asdict(replace(entry(number), title=title))}
            for number, title in enumerate(titles, 1)
        ] + [{"kind": "done"}]
        self.controller.poll_scan()
        self.assertEqual(self.window.preview_order.currentData(), "episode")
        self.window.add_button.click()
        jobs = self.controller.store.jobs()
        self.assertEqual([job.entry.title for job in jobs], [titles[i] for i in (1, 3, 4, 0, 5, 2, 6)])
        self.assertEqual([job.entry.index_label for job in jobs],
                         ["045", "104", "284", "451", "S02E007", "Bez numeru 001", "Bez numeru 002"])
        self.assertTrue(output_stem(jobs[3].entry, jobs[3].options, jobs[3].id).name.startswith("451 - "))

    def test_direct_episode_link_uses_the_episode_number_in_the_file_name(self):
        video = Entry("ECKuiSbGWxY", "Wśród nocnej ciszy. Prawdziwa historia o zbrodni połanieckiej | 450.")
        self.controller.cancel_scan()
        self.window.source.setText(video.url)
        self.window.preview()
        self.worker.poll.return_value = 0
        self.worker.events.return_value = [{"kind": "entry", "entry": asdict(video)}, {"kind": "done"}]
        self.controller.poll_scan()

        order = self.window.preview_order
        self.assertTrue(order.isEnabled())
        self.assertEqual(order.currentData(), "episode")
        self.assertFalse(order.model().item(order.findData("reverse")).isEnabled())
        self.assertEqual(self.window.preview_filter.index(0, 1).data(), "450")
        self.window.add_button.click()
        job, = self.controller.store.jobs()
        self.assertEqual(job.entry.index_label, "450")
        self.assertEqual(output_stem(job.entry, job.options, job.id).name,
                         "450 - Wśród nocnej ciszy. Prawdziwa historia o zbrodni połanieckiej _ 450 [ECKuiSbGWxY]")


if __name__ == "__main__":
    unittest.main()
