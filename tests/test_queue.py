from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from yt_downloader.model import Entry, Options, output_stem, parse_source
from yt_downloader.store import Store


class QueueTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.entry = Entry("jNQXAC9IVRw", "Przykład")
        self.options = Options(folder=str(self.root))

    def test_history_deduplicates_parallel_work_and_preserves_forced_jobs(self):
        self.store.enqueue([self.entry, self.entry], self.options)
        self.store.enqueue([self.entry], replace(self.options, force=True))
        first = self.store.claim()
        self.assertIsNone(self.store.claim())
        self.store.complete(first, "video.mp4")
        self.assertEqual(self.store.skip_downloaded(), 1)
        self.assertTrue(self.store.claim().options.force)

    def test_recovery_requeues_running_jobs_and_preserves_paused_jobs(self):
        self.store.enqueue([self.entry, self.entry], self.options)
        first, second = self.store.jobs()
        self.store.state(first.id, "paused")
        self.assertEqual(self.store.claim().id, second.id)
        self.store.recover_interrupted()
        self.assertEqual([job.state for job in self.store.jobs()], ["paused", "queued"])

    def test_windows_path_is_safe_and_keeps_video_id(self):
        entry = replace(self.entry, title='CON: ? <>', channel="NUL", playlist="..")
        path = output_stem(entry, self.options, 1)
        self.assertTrue(path.is_relative_to(self.root.resolve()))
        self.assertEqual(path.parent.parent.name, "_NUL")
        self.assertIn(self.entry.id, path.name)
        self.assertNotIn("?", path.name)

    def test_youtube_urls_are_normalized(self):
        self.assertEqual(parse_source("youtu.be/jNQXAC9IVRw").kind, "video")
        self.assertEqual(parse_source("youtube.com/watch?v=jNQXAC9IVRw&list=PLabc").kind, "playlist")
        self.assertEqual(parse_source("youtube.com/@channel/shorts").tab, "shorts")
        with self.assertRaises(ValueError):
            parse_source("youtube.com.example/watch?v=jNQXAC9IVRw")
