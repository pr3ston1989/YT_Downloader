from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from yt_downloader.model import Entry, Options
from yt_downloader.organize import apply_plan, make_plan, recover_file_moves, signature
from yt_downloader.store import Store


VIDEO = Entry("ECKuiSbGWxY", "Film", source_name="Wspólna playlista")


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        base = Path(work.name).resolve()
        self.root = base / "Pobrane"
        self.root.mkdir()
        self.store = Store(base / "data")
        self.addCleanup(self.store.close)

    def file(self, path, content=b"fixture"):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content)
        return file

    def test_plan_moves_a_tracked_file_and_companions_and_updates_both_saved_paths(self):
        source = self.file("Stary kanał/Playlista/Mój film.mkv")
        subtitles = self.file("Stary kanał/Playlista/Mój film.pl.srt")
        thumbnail = self.file("Stary kanał/Playlista/Mój film.webp")
        self.store.enqueue([VIDEO], Options(folder=str(self.root)))
        job = self.store.claim()
        self.store.complete(job, str(source))
        time = self.store.history()[0]["time"]
        groups = make_plan(self.root, [VIDEO], self.store.history())
        self.assertEqual(set(groups[0].files), {source, subtitles, thumbnail})
        self.assertTrue(source.is_file())
        self.assertEqual(apply_plan(self.store, self.root, groups, set()), (3, 0, 0))
        target = self.root / VIDEO.source_name / source.name
        self.assertEqual({path.name for path in target.parent.iterdir()}, {source.name, subtitles.name, thumbnail.name})
        self.assertEqual(self.store.history()[0]["path"], str(target))
        self.assertEqual(self.store.history()[0]["time"], time)
        self.assertEqual(self.store.jobs()[0].path, str(target))
        self.assertFalse(source.exists())

    def test_conflicts_and_partial_files_are_not_moved(self):
        basename = f"Film [{VIDEO.id}]"
        source = self.file(f"Autor/{basename}.mp4", b"old")
        existing = self.file(f"{VIDEO.source_name}/FILM [{VIDEO.id}].mp4", b"keep")
        partial = self.file(f"Autor/{basename}.f137.mp4")
        groups = make_plan(self.root, [VIDEO], [])
        self.assertEqual(len(groups), 2)
        self.assertEqual({group.status for group in groups}, {"Konflikt nazw", "Już w folderze"})
        self.assertEqual(apply_plan(self.store, self.root, groups, set()), (0, 0, 0))
        self.assertEqual(source.read_bytes(), b"old")
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertTrue(partial.is_file())

    def test_files_that_change_or_are_downloading_after_planning_are_skipped(self):
        source = self.file(f"Autor/Film [{VIDEO.id}].mp4")
        groups = make_plan(self.root, [VIDEO], [])
        self.assertEqual(apply_plan(self.store, self.root, groups, {VIDEO.id}), (0, 0, 1))
        source.write_bytes(b"file changed after preview")
        with self.assertLogs("yt_downloader.organize", level="ERROR"):
            self.assertEqual(apply_plan(self.store, self.root, groups, set()), (0, 1, 0))
        self.assertEqual(source.read_bytes(), b"file changed after preview")

    def test_choosing_the_playlist_folder_keeps_it_as_the_destination(self):
        target = self.root / VIDEO.source_name
        original = self.file(f"{VIDEO.source_name}/Stary kanał/Film [{VIDEO.id}].mkv")
        groups = make_plan(target, [VIDEO], [])
        self.assertEqual(groups[0].target, target)
        self.assertEqual(apply_plan(self.store, target, groups, set()), (1, 0, 0))
        self.assertTrue((target / original.name).is_file())

    def test_recovery_rolls_back_an_interrupted_group_and_commits_a_finished_group(self):
        basename = f"Film [{VIDEO.id}]"
        source = self.file(f"Autor/{basename}.mkv")
        subtitle = self.file(f"Autor/{basename}.en.srt")
        target = self.root / VIDEO.source_name / source.name
        target_sub = target.with_name(subtitle.name)
        target.parent.mkdir()
        paths = [(str(src), str(dst), signature(src)) for src, dst in ((source, target), (subtitle, target_sub))]
        self.store.record_file_move(self.root, VIDEO.id, VIDEO.title, paths)
        source.rename(target)
        with closing(Store(self.root.parent / "data")) as reopened:
            recover_file_moves(reopened)
            self.assertTrue(source.is_file() and subtitle.is_file())
            self.assertFalse(target.exists())
            self.assertEqual(reopened.pending_file_moves(), [])

        self.store.record_file_move(self.root, VIDEO.id, VIDEO.title, paths)
        source.rename(target)
        subtitle.rename(target_sub)
        recover_file_moves(self.store)
        self.assertEqual(self.store.history()[0]["path"], str(target))
        self.assertEqual(self.store.pending_file_moves(), [])
