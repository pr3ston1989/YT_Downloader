import hashlib
import io
from pathlib import Path
import tempfile
import unittest
import zipfile

from yt_downloader.updates.manager import EngineManager, extract
from yt_downloader.updates.sources import Http, Package


def package(version):
    name = "yt-dlp"
    return Package(name, version, "stable", "https://test.invalid/asset", name,
                   "https://test.invalid/SHA2-256SUMS")


class FakeHttp(Http):
    def __init__(self):
        self.data = b""
        self.digest = ""

    def open(self, url):
        if url.endswith("SHA2-256SUMS"):
            return io.BytesIO(f"{self.digest}  yt-dlp\n".encode())
        return io.BytesIO(self.data)

    def release(self, contents):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("yt_dlp/__init__.py", contents)
        self.data = buffer.getvalue()
        self.digest = hashlib.sha256(self.data).hexdigest()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name) / "engine"
        self.http = FakeHttp()
        self.records = []
        self.manager = EngineManager(self.root, lambda *args: self.records.append(args),
                                     seed=Path(self.work.name) / "seed",
                                     http=self.http, probe=self.probe)

    def probe(self, component, root, expected):
        self.assertEqual(component, "yt-dlp")
        value = (root / "yt_dlp" / "__init__.py").read_text()
        if value != expected:
            raise ValueError("Test uruchomienia nie powiódł się.")

    def install(self, version):
        self.http.release(version)
        return self.manager.install(package(version))

    def test_successful_update_keeps_previous_immutable(self):
        self.assertTrue(self.install("1"))
        old = self.manager.snapshot()
        self.assertTrue(self.install("2"))
        self.assertEqual(self.manager.snapshot().version, "2")
        self.assertEqual((Path(old.engine) / "yt_dlp" / "__init__.py").read_text(), "1")
        self.assertEqual(self.manager.read("yt-dlp")["previous"]["version"], "1")

    def test_bad_checksum_preserves_active_version(self):
        self.install("1")
        old = self.manager.snapshot()
        self.http.release("2")
        self.http.digest = "0" * 64
        self.assertFalse(self.manager.install(package("2")))
        self.assertEqual(self.manager.snapshot(), old)
        self.assertEqual(self.records[-1][2], "rollback")
        self.assertIn("SHA-256", self.records[-1][3])

    def test_failed_startup_test_never_activates_candidate(self):
        self.install("1")
        old = self.manager.snapshot()
        self.http.release("broken")
        self.assertFalse(self.manager.install(package("2")))
        self.assertEqual(self.manager.snapshot(), old)
        self.assertEqual(self.manager.read("yt-dlp")["active"]["version"], "1")
        self.assertEqual(self.records[-1][2], "rollback")

    def test_diagnostics_rolls_back_a_corrupted_installed_version(self):
        self.install("1")
        self.install("2")
        path = Path(self.manager.snapshot().engine) / "yt_dlp" / "__init__.py"
        path.write_text("corrupt")
        self.manager.diagnose()
        self.assertEqual(self.manager.snapshot().version, "1")
        self.assertEqual(self.records[-1][2], "rollback")

    def test_archive_cannot_escape_staging_directory(self):
        archive = Path(self.work.name) / "asset"
        with zipfile.ZipFile(archive, "w") as stream:
            stream.writestr("yt_dlp/../../escape.py", "")
        with self.assertRaises(ValueError):
            extract(archive, self.root, "yt-dlp")
        self.assertFalse((self.root.parent / "escape.py").exists())


if __name__ == "__main__":
    unittest.main()
