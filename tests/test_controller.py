from concurrent.futures import Future
from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from yt_downloader.controller import Controller
from yt_downloader.model import Entry, parse_source
from yt_downloader.runtime import command
from yt_downloader.store import Store
from yt_downloader.ui import autostart
from yt_downloader.updates import Runtime


VIDEO = Entry("jNQXAC9IVRw", "Film testowy")
NEXT_VIDEO = Entry("MgNrAu2pzNs", "Kolejny film")
CHANNEL = "https://www.youtube.com/@test"


class FixtureWorker:
    def __init__(self, directory, request):
        self.request = request
        self.exit_code = None
        self.pending = []
        self.closed = False

    def finish(self, **event):
        self.pending.append(event)
        self.exit_code = 0 if event["kind"] == "done" else 1

    def events(self):
        batch, self.pending = self.pending, []
        return batch

    def poll(self):
        return self.exit_code

    def stop(self):
        if self.exit_code is None:
            self.exit_code = 1

    def close(self):
        self.stop()
        self.closed = True


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        with patch.object(Controller, "update"):
            self.c = Controller(self.root, smoke=True)
        self.c.timer.stop()
        self.addCleanup(self.close_controller)
        self.c.manager.current = Runtime("engine", "deno", "ffmpeg", "1")
        self.c.prefs.parallel = 1
        workers = patch("yt_downloader.controller.Worker", FixtureWorker)
        workers.start()
        self.addCleanup(workers.stop)

    def close_controller(self):
        if self.c is not None:
            self.c.close()
            self.c = None

    def enqueue(self, *entries):
        self.c.store.enqueue(entries, self.c.prefs.options)
        self.c.step()
        return self.c.active[1].worker

    def subscribe(self, url=CHANNEL, *, music=False):
        self.c.store.add_subscription(url, ["videos"], 3, music=music)
        return self.c.store.subscriptions()[-1]["id"]

    def finish_subscription(self, *entries):
        worker = self.c.scan.worker
        worker.pending.extend({"kind": "entry", "entry": asdict(entry)} for entry in entries)
        worker.finish(kind="done", count=len(entries))
        self.c.poll_scan()

    def test_completion_is_saved_if_pause_arrives_before_the_next_tick(self):
        worker = self.enqueue(VIDEO)
        worker.finish(kind="done", path="film.mp4")
        self.c.pause_job(1)
        self.c.poll_jobs()
        self.assertEqual(self.c.store.jobs()[0].state, "done")
        self.assertEqual(self.c.store.history()[0]["id"], VIDEO.id)

    def test_removing_active_and_waiting_jobs_does_not_restart_them(self):
        current = self.enqueue(VIDEO, NEXT_VIDEO, Entry("BaW_jenozKc", "Trzeci film"))
        self.assertEqual(self.c.remove_jobs([1, 2]), 2)
        current.pending.append({"kind": "error", "code": "network", "message": "Błąd testowy"})
        self.c.step()
        self.assertTrue(current.closed)
        self.assertEqual([job.id for job in self.c.store.jobs()], [3])
        self.assertEqual(list(self.c.active), [3])

    def test_removing_a_just_completed_job_keeps_its_history(self):
        worker = self.enqueue(VIDEO)
        worker.finish(kind="done", path="gotowy.mp4")
        self.c.remove_jobs([1])
        self.c.step()
        self.assertEqual(self.c.store.jobs(), [])
        self.assertEqual(self.c.store.history()[0]["path"], "gotowy.mp4")

    def test_jobs_being_edited_wait_while_the_rest_of_the_queue_runs(self):
        self.c.store.enqueue([VIDEO, NEXT_VIDEO], self.c.prefs.options)
        self.c.store.import_archive([f"youtube {VIDEO.id}"])
        self.c.editing_jobs.add(1)
        self.c.step()
        self.assertEqual(list(self.c.active), [2])
        edited = self.c.store.jobs()[0]
        self.assertEqual(edited.state, "queued")
        self.c.store.edit_job_options([edited], {"force": True})
        self.c.editing_jobs.clear()
        self.c.active[2].worker.finish(kind="done", path="gotowy.mp4")
        self.c.step()
        self.assertEqual(list(self.c.active), [1])

    def test_shutdown_keeps_finished_downloads_and_requeues_interrupted_ones(self):
        self.c.prefs.parallel = 2
        first = self.enqueue(VIDEO, NEXT_VIDEO)
        first.finish(kind="done", path="film.mp4")
        self.close_controller()
        store = Store(self.root)
        self.addCleanup(store.close)
        self.assertEqual([job.state for job in store.jobs()], ["done", "queued"])

    def test_update_waits_for_the_active_transfer_and_leaves_waiting_jobs_in_queue(self):
        worker = self.enqueue(VIDEO, NEXT_VIDEO)
        ready = []
        self.c.update_ready.connect(ready.append)
        self.c.future = Future()
        self.c.future.set_result(("routine", {}, "new installer", time.time()))
        self.c.step()
        self.assertEqual(ready, [])
        self.assertFalse(worker.closed)
        worker.finish(kind="done", path="film.mp4")
        self.c.step()
        self.assertEqual(ready, ["new installer"])
        self.assertEqual([job.state for job in self.c.store.jobs()], ["done", "queued"])

    def test_priority_download_gets_the_first_free_slot(self):
        waiting = [Entry(f"v{i:010}", f"Film {i}") for i in range(100)]
        current = self.enqueue(VIDEO, *waiting)
        self.c.store.enqueue([NEXT_VIDEO], self.c.prefs.options, first=True)
        self.c.step()
        self.assertEqual(list(self.c.active), [1])
        self.assertFalse(current.closed)

        current.finish(kind="done", path="film.mp4")
        self.c.step()
        urgent, = self.c.active.values()
        self.assertEqual(urgent.job.entry, NEXT_VIDEO)
        urgent.worker.finish(kind="done", path="pilny.mp4")
        self.c.step()
        self.assertEqual(self.c.active[2].job.entry, waiting[0])

    def test_subscription_rescans_without_duplicates_and_preserves_music_mode(self):
        sub_id = self.subscribe(music=True)
        self.c.step()
        self.assertTrue(self.c.scan.worker.request["source"]["music"])
        self.finish_subscription(VIDEO)
        self.assertIsNone(self.c.store.subscription_due())
        self.c.store.postpone_subscription(sub_id, 0)
        self.c.step()
        self.finish_subscription(VIDEO, NEXT_VIDEO)
        jobs = self.c.store.jobs()
        self.assertEqual([job.entry.id for job in jobs], [VIDEO.id, NEXT_VIDEO.id])
        self.assertTrue(all(job.options.audio_only for job in jobs))

    def test_subscription_login_waits_without_blocking_others_and_retries_immediately(self):
        first = self.subscribe()
        second = self.subscribe("https://www.youtube.com/@other")
        self.c.step()
        self.c.scan.worker.finish(kind="error", code="auth", message="Zaloguj się.")
        self.c.poll_scan()
        self.assertEqual(self.c.store.subscription_needing_login()["id"], first)
        self.assertEqual(self.c.store.subscription_due()["id"], second)
        self.close_controller()
        store = Store(self.root)
        self.addCleanup(store.close)
        self.assertEqual(store.subscription_needing_login()["id"], first)
        store.retry_auth()
        self.assertIsNone(store.subscription_needing_login())
        self.assertEqual(store.subscription_due()["id"], first)

    def test_deleting_an_active_subscription_stops_its_scan(self):
        sub_id = self.subscribe()
        self.c.step()
        worker = self.c.scan.worker
        worker.pending.append({"kind": "entry", "entry": asdict(VIDEO)})
        worker.finish(kind="done", count=1)
        self.c.remove_subscriptions([sub_id])
        self.assertTrue(worker.closed)
        self.assertIsNone(self.c.scan)
        self.assertEqual(self.c.store.jobs(), [])

    def test_cancelled_scan_repair_cannot_restart_a_different_scan(self):
        self.c.preview(parse_source(CHANNEL), ["videos"])
        old = self.c.scan
        old.worker.finish(kind="error", code="extractor", message="Błąd ekstraktora.")
        self.c.poll_scan()
        self.c.future = Future()
        self.c.future.set_result(("repair", dict(self.c.pending_repairs), None, None))
        self.c.pending_repairs.clear()
        self.c.cancel_scan()
        self.c.preview(parse_source(CHANNEL), ["shorts"])
        self.c.scan.worker.finish(kind="error", code="extractor", message="Błąd ekstraktora.")
        self.c.poll_scan()
        self.c.poll_update()
        self.assertIsNone(self.c.scan.worker)
        self.assertIn(self.c.scan, self.c.pending_repairs)

    def test_autostart_registers_a_quoted_launch_command_and_removes_it_when_disabled(self):
        with (patch("yt_downloader.ui.winreg.CreateKey") as key,
              patch("yt_downloader.ui.winreg.SetValueEx") as write,
              patch("yt_downloader.ui.winreg.DeleteValue") as delete):
            autostart(True, True)
            self.assertEqual(write.call_args.args[1], "YTDownloader")
            self.assertEqual(write.call_args.args[-1], subprocess.list2cmdline(command("--tray")))
            autostart(False, False)
            delete.assert_called_once_with(key.return_value.__enter__.return_value, "YTDownloader")


if __name__ == "__main__":
    unittest.main()
