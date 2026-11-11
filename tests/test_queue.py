from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yt_downloader.model import DEFAULT_TEMPLATE, LEGACY_TEMPLATE, Entry, Options, output_stem, parse_source
from yt_downloader.store import Store
from yt_downloader.worker import download_stem


class QueueTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name).resolve()
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

    def test_reorder_moves_selected_jobs_together_and_preserves_the_order_on_reopen(self):
        self.store.enqueue([replace(self.entry, id=f"v{i:010}") for i in range(1, 8)], self.options)
        self.assertEqual(self.store.claim().id, 1)
        self.store.state(4, "paused")
        self.store.move_jobs([4, 6], "up")
        self.assertEqual([job.id for job in self.store.jobs()], [1, 2, 4, 3, 6, 5, 7])
        self.store.move_jobs([4, 6], "down")
        self.assertEqual([job.id for job in self.store.jobs()], [1, 2, 3, 4, 5, 6, 7])
        self.store.move_jobs([6, 4, 1], "first")
        self.store.move_jobs([3], "last")
        self.store.enqueue([replace(self.entry, id="v0000000008")], self.options)
        with closing(Store(self.root)) as reopened:
            self.assertEqual([job.id for job in reopened.jobs()], [1, 4, 6, 2, 5, 7, 3, 8])
            self.assertEqual([job.state for job in reopened.jobs()[:2]], ["running", "paused"])
            self.assertEqual(reopened.claim().id, 6)

    def test_priority_batch_keeps_its_order_and_skips_tasks_waiting_for_a_retry(self):
        entries = [replace(self.entry, id=f"v{i:010}") for i in range(1, 6)]
        self.store.enqueue(entries[:2], self.options)
        self.store.enqueue(entries[2:4], self.options, first=True)
        self.store.enqueue(entries[4:], self.options, first=True)
        self.assertEqual([job.id for job in self.store.jobs()], [5, 3, 4, 1, 2])
        self.store.state(5, "queued", delay=3600)
        self.assertEqual([self.store.claim().id for _ in range(4)], [3, 4, 1, 2])
        self.assertIsNone(self.store.claim())

    def test_queue_position_migration_keeps_the_existing_insertion_order(self):
        root = self.root / "legacy-queue"
        with closing(Store(root)) as legacy:
            legacy.enqueue([replace(self.entry, id=f"v{i:010}") for i in range(1, 4)], self.options)
            legacy.db.execute("ALTER TABLE jobs DROP COLUMN queue_position")
        with closing(Store(root)) as migrated:
            self.assertEqual([job.id for job in migrated.jobs()], [1, 2, 3])
            migrated.move_jobs([3], "first")
            self.assertEqual(migrated.claim().id, 3)

    def test_login_attempt_survives_interruption(self):
        self.store.enqueue([self.entry], self.options)
        job = self.store.claim()
        self.store.state(job.id, "auth")
        self.store.retry_auth()
        self.store.claim()
        self.store.recover_interrupted()
        self.assertEqual(self.store.claim().auth_attempts, 1)

    def test_windows_path_is_safe_and_keeps_video_id(self):
        entry = replace(self.entry, title='CON: ? <>', source_name="NUL")
        path = output_stem(entry, self.options, 1)
        self.assertTrue(path.is_relative_to(self.root.resolve()))
        self.assertEqual(path.parent.name, "_NUL")
        self.assertIn(self.entry.id, path.name)
        self.assertNotIn("?", path.name)

    def test_previous_default_is_migrated_once_and_custom_settings_are_preserved(self):
        custom = "{channel}/Własne/{title}.{ext}"
        for original, expected in ((LEGACY_TEMPLATE.replace("/", "\\"), DEFAULT_TEMPLATE), (custom, custom)):
            with self.subTest(original=original):
                root = self.root / str(original == custom)
                with closing(Store(root)) as previous:
                    previous.set("settings", {"options": {"template": original, "resolution": 480}})
                    previous.db.execute("PRAGMA user_version = 0")
                with closing(Store(root)) as migrated:
                    self.assertEqual(migrated.preferences().options.template, expected)
                    self.assertEqual(migrated.preferences().options.resolution, 480)
                    migrated.set("settings", {"options": {"template": LEGACY_TEMPLATE}})
                with closing(Store(root)) as reopened:
                    self.assertEqual(reopened.preferences().options.template, LEGACY_TEMPLATE)

    def test_saved_job_uses_the_source_folder_and_preserves_custom_templates(self):
        options = replace(self.options, template=LEGACY_TEMPLATE)
        items = [replace(self.entry, channel="Autor A", playlist="Moja playlista"),
                 replace(self.entry, channel="Autor B", playlist="Moja playlista"),
                 replace(self.entry, channel="Mój kanał", playlist="Mój kanał - Filmy", tab="videos")]
        self.store.enqueue(items, options)
        paths = [download_stem(job.entry, job.options, job.id, "mp4") for job in self.store.jobs()]
        self.assertEqual([path.parent.relative_to(self.root).parts for path in paths],
                         [("Moja playlista",), ("Moja playlista",), ("Mój kanał",)])
        current = replace(items[0], source_name="Moja playlista")
        self.assertEqual(download_stem(current, options, 1, "mp4").parent.relative_to(self.root).parts,
                         ("Autor A", "Moja playlista"))

    def test_youtube_urls_are_normalized(self):
        self.assertEqual(parse_source("youtu.be/jNQXAC9IVRw").kind, "video")
        self.assertEqual(parse_source("youtube.com/watch?v=jNQXAC9IVRw&list=PLabc").kind, "playlist")
        self.assertEqual(parse_source("youtube.com/@channel/shorts").tab, "shorts")
        with self.assertRaises(ValueError):
            parse_source("youtube.com.example/watch?v=jNQXAC9IVRw")

    def test_subscription_migration_preserves_the_previous_schedule(self):
        old = self.root / "old-profile"
        old.mkdir()
        with closing(sqlite3.connect(old / "data.sqlite3")) as db:
            db.executescript("""
                CREATE TABLE subscriptions (
                    id INTEGER PRIMARY KEY, url TEXT NOT NULL, tabs TEXT NOT NULL,
                    hours INTEGER NOT NULL, due REAL NOT NULL, UNIQUE(url, tabs)
                );
                INSERT INTO subscriptions VALUES (17, 'https://www.youtube.com/@test', '["videos"]', 6, 1234);
            """)
        with closing(Store(old)) as migrated:
            row = migrated.subscriptions()[0]
            self.assertEqual((row["id"], row["hours"], row["due"]), (17, 6, 1234))
            self.assertEqual((row["needs_login"], row["music"]), (0, 0))
