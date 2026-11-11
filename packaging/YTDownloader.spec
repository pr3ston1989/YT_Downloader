import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


root = Path(SPECPATH).parent
seed = root / "vendor" / "seed"
seed_files = [
    (str(path), str(Path("seed") / path.parent.relative_to(seed)))
    for path in seed.rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
]
hidden = [
    "_bz2", "_lzma", "_sqlite3", "_ssl", "_hashlib", "_ctypes", "_queue", "_decimal",
    "select", "_socket", "pyexpat", "_overlapped", "mmap", "_uuid", "unicodedata",
    "brotli", "certifi", "requests",
]
for package in ("mutagen", "websockets"):
    hidden.extend(collect_submodules(package))

analysis = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root)],
    datas=[
        (str(root / "app.ico"), "."),
        (str(root / "packaging" / "apply-update.ps1"), "."),
        (str(root / "vendor" / "runtime-stdlib.zip"), "."),
        *seed_files,
    ],
    hiddenimports=hidden,
    excludes=["yt_dlp", "yt_dlp_ejs"],
)
archive = PYZ(analysis.pure)
exe = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="YTDownloader",
          console=os.environ.get("YT_DOWNLOADER_DEBUG_CONSOLE") == "1",
          icon=str(root / "app.ico"), upx=False)
distribution = COLLECT(exe, analysis.binaries, analysis.datas, name="YTDownloader", upx=False)
