import os
from pathlib import Path
import re
import sysconfig

from PyInstaller.utils.hooks import collect_all, collect_submodules


root = Path(SPECPATH).parent
seed = root / "vendor" / "seed"
seed_files = [
    (str(path), str(Path("seed") / path.parent.relative_to(seed)))
    for path in seed.rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
]
stdlib = Path(sysconfig.get_path("stdlib"))
excluded_dirs = {"site-packages", "test", "tests", "tkinter", "turtledemo", "idlelib",
                 "ensurepip", "venv", "__pycache__"}
stdlib_files = []
for directory, directories, filenames in stdlib.walk():
    directories[:] = [name for name in directories if name not in excluded_dirs]
    stdlib_files.extend((str(directory / name), str(directory.relative_to(stdlib)))
                        for name in filenames if name.endswith(".py"))
hidden = [
    "_bz2", "_lzma", "_sqlite3", "_ssl", "_hashlib", "_ctypes", "_queue", "_decimal",
    "select", "_socket", "pyexpat", "_overlapped", "mmap", "_uuid", "unicodedata",
    "_elementtree", "_multibytecodec", "_zoneinfo",
    "brotli", "certifi", "requests",
]
for package in ("mutagen", "websockets"):
    hidden.extend(collect_submodules(package))
curl_data, curl_binaries, curl_hidden = collect_all("curl_cffi")

analysis = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root)],
    datas=[
        (str(root / "app.ico"), "."),
        (str(root / "packaging" / "apply-update.ps1"), "."),
        *stdlib_files,
        *seed_files,
        *curl_data,
    ],
    binaries=curl_binaries,
    hiddenimports=hidden + curl_hidden,
    excludes=["yt_dlp", "yt_dlp_ejs"],
)
# The Windows wheels use the OS ICU. DLLs from another tool on PATH can
# shadow the Windows exports and prevent QtCore.pyd from loading.
analysis.binaries = [
    entry for entry in analysis.binaries
    if not re.fullmatch(r"icu(?:(?:dt|in|uc)\d*)?\.dll",
                        Path(entry[0]).name, re.IGNORECASE)
]
archive = PYZ(analysis.pure)
exe = EXE(archive, analysis.scripts, [], exclude_binaries=True, name="YTDownloader",
          console=os.environ.get("YT_DOWNLOADER_DEBUG_CONSOLE") == "1",
          icon=str(root / "app.ico"), upx=False)
distribution = COLLECT(exe, analysis.binaries, analysis.datas, name="YTDownloader", upx=False)
