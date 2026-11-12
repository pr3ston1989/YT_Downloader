from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import json
import logging
from pathlib import Path
import re
from sqlite3 import Error as DatabaseError

from .model import safe_segment


LOG = logging.getLogger(__name__)
MEDIA_EXTENSIONS = {".mp4", ".mkv", ".mp3", ".m4a", ".opus", ".webm"}
SIDECAR = re.compile(r"(?:info\.json|description|webp|jpe?g|png|(?:(?:[a-z0-9_-]+)\.)?(?:srt|vtt|ass))", re.IGNORECASE)
VIDEO_MARKER = re.compile(r"\[([A-Za-z0-9_-]{11})\](?: \(\d+\))?$")


@dataclass(frozen=True)
class FileGroup:
    video_id: str
    title: str
    files: tuple[Path, ...]
    target: Path
    signatures: tuple[tuple[int, ...], ...]
    status: str = ""


def make_plan(root, entries, history):
    if not entries:
        return []
    root = root.resolve(strict=True)
    items = {entry.id: entry.title for entry in entries}
    name = safe_segment(entries[0].source_name or entries[0].playlist, 80)
    target = root if root.name.casefold() == name.casefold() else root / name
    if target.is_symlink() or target.is_junction() or target.exists() and not target.is_dir():
        raise ValueError("Folder docelowy wskazuje na dowiązanie lub istniejący plik.")
    recorded = {Path(row["path"]): row["id"] for row in history if row["path"] and row["id"] in items}
    plan = []
    for directory, dirs, names in root.walk():
        dirs[:] = [name for name in dirs if not (directory / name).is_junction()]
        files = sorted((directory / name for name in names if not (directory / name).is_symlink()),
                       key=lambda path: path.name.casefold())
        groups = defaultdict(list)
        for path in files:
            if path.suffix.lower() not in MEDIA_EXTENSIONS:
                continue
            marker = VIDEO_MARKER.search(path.stem)
            video_id = recorded.get(path) or (marker[1] if marker else None)
            if video_id in items:
                groups[path.stem, video_id].append(path)
        # Index companions by basename so each directory is traversed only once.
        companions = defaultdict(list)
        for path in files:
            if path.suffix.lower() in MEDIA_EXTENSIONS:
                continue
            stem, _, ext = path.name.rpartition(".")
            if ext.lower() in {"json", "srt", "vtt", "ass"}:
                companions[stem.rpartition(".")[0]].append(path)
            companions[stem].append(path)
        for (stem, video_id), media in groups.items():
            prefix = stem + "."
            related = [path for path in companions[stem] if SIDECAR.fullmatch(path.name[len(prefix):])]
            paths = tuple((*media, *related))
            try:
                signatures = tuple(tuple(signature(path)) for path in paths)
            except OSError:
                plan.append(FileGroup(video_id, items[video_id], paths, target, (), "Plik niedostępny"))
                continue
            plan.append(FileGroup(video_id, items[video_id], paths, target, signatures,
                                  "Już w folderze" if directory == target else ""))
    names = Counter(path.name.casefold() for group in plan if not group.status for path in group.files)
    existing = {path.name.casefold() for path in target.iterdir()} if target.is_dir() else set()
    return [replace(group, status="Konflikt nazw") if not group.status and any(
        path.name.casefold() in existing or names[path.name.casefold()] > 1 for path in group.files
    ) else group for group in plan]


def signature(path):
    stat = path.stat(follow_symlinks=False)
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def location(source, target, expected):
    try:
        if not source.is_symlink() and signature(source) == expected:
            return source
    except FileNotFoundError:
        if target.is_file() and not target.is_symlink() and signature(target) == expected:
            return target
    return None


def safe_parent(path, root):
    return path.parent.resolve() == path.parent and path.parent.is_relative_to(root)


def rename_file(source, target):
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    # Windows rename fails atomically if the destination appeared after the check.
    source.rename(target)


def settle_move(store, move):
    root = Path(move["root"])
    files = [(Path(src), Path(dst), expected) for src, dst, expected in json.loads(move["files"])]
    if not all(safe_parent(src, root) and safe_parent(dst, root) for src, dst, _ in files):
        return False
    locations = [location(*file) for file in files]
    if None in locations:
        return False
    if any(found == src for (src, _, _), found in zip(files, locations)):
        for (src, dst, _), found in reversed(list(zip(files, locations))):
            if found == dst:
                try:
                    rename_file(dst, src)
                except OSError:
                    LOG.exception("Przywracanie pliku %s", src)
        locations = [location(*file) for file in files]
        if None in locations:
            return False
    moved = [(str(src), str(dst)) for (src, dst, _), found in zip(files, locations)
             if found == dst and src.suffix.lower() in MEDIA_EXTENSIONS]
    store.finish_file_move(move["id"], move["video_id"], move["title"], moved)
    return all(found == dst for (_, dst, _), found in zip(files, locations))


def recover_file_moves(store):
    for move in store.pending_file_moves():
        try:
            settle_move(store, move)
        except (OSError, DatabaseError):
            LOG.exception("Sprawdzanie przerwanego przenoszenia plików")


def apply_plan(store, root, groups, active_ids):
    moved, errors, busy = 0, 0, 0
    for group in groups:
        if group.status:
            continue
        if group.video_id in active_ids:
            busy += len(group.files)
            continue
        try:
            files = [(path, group.target / path.name) for path in group.files]
            if not all(safe_parent(src, root) and safe_parent(dst, root) and src.is_file() and not src.is_symlink()
                       and not dst.exists() and not dst.is_symlink() for src, dst in files):
                raise OSError("The source or destination changed since the preview")
            if any(signature(src) != list(expected) for (src, _), expected in zip(files, group.signatures)):
                raise OSError("A file changed after the move preview")
            records = [(str(src), str(dst), expected) for (src, dst), expected in zip(files, group.signatures)]
            group.target.mkdir(exist_ok=True)
            move_id = store.record_file_move(root, group.video_id, group.title, records)
            try:
                for src, dst in files:
                    rename_file(src, dst)
            finally:
                move = {"id": move_id, "root": str(root), "video_id": group.video_id,
                        "title": group.title, "files": json.dumps(records)}
                completed = settle_move(store, move)
            if completed:
                moved += len(files)
            else:
                errors += 1
        except (OSError, DatabaseError):
            LOG.exception("Przenoszenie plików filmu %s", group.video_id)
            errors += 1
    return moved, errors, busy
