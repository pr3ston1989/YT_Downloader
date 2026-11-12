from collections import Counter, defaultdict
from dataclasses import dataclass, replace
import json
import logging
import os
from pathlib import Path
import re
import shutil
from sqlite3 import Error as DatabaseError
from uuid import uuid4

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
    return flag_conflicts(plan, target)


def flag_conflicts(plan, target):
    names = Counter(path.name.casefold() for group in plan if not group.status for path in group.files)
    existing = {path.name.casefold() for path in target.iterdir()} if target.is_dir() else set()
    return [replace(group, status="Konflikt nazw") if not group.status and any(
        path.name.casefold() in existing or names[path.name.casefold()] > 1 for path in group.files
    ) else group for group in plan]


def plan_selected(records, target):
    target = target.resolve(strict=True)
    if not target.is_dir():
        raise ValueError("Wybierz folder docelowy.")
    directories = {}
    groups = []
    seen = set()
    for video_id, title, filename in records:
        if not filename:
            continue
        path = Path(filename)
        if path in seen:
            continue
        seen.add(path)
        status = ""
        files = (path,)
        signatures = ()
        try:
            if not path.is_absolute() or not safe_parent(path, path.parent) or not path.is_file() or path.is_symlink():
                raise OSError("Invalid recorded path")
            if path.parent not in directories:
                directories[path.parent] = defaultdict(list)
                for companion in path.parent.iterdir():
                    stem, _, suffix = companion.name.rpartition(".")
                    if suffix.lower() in {"json", "srt", "vtt", "ass"}:
                        directories[path.parent][stem.rpartition(".")[0]].append(companion)
                    directories[path.parent][stem].append(companion)
            files += tuple(companion for companion in directories[path.parent][path.stem]
                           if SIDECAR.fullmatch(companion.name[len(path.stem) + 1:])
                           and companion.is_file() and not companion.is_symlink())
            signatures = tuple(tuple(signature(file)) for file in files)
            if path.parent == target:
                status = "Już w folderze"
        except OSError:
            status = "Plik niedostępny"
        groups.append(FileGroup(video_id, title, files, target, signatures, status))
    return flag_conflicts(groups, target)


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


def has_signature(path, expected):
    return path.is_file() and not path.is_symlink() and signature(path) == expected


def settle_selected(store, move, transfer):
    source_dir = Path(move["root"])
    stage = Path(transfer["stage"])
    target_dir = stage.parent
    files = [(Path(src), Path(dst), original, staged) for src, dst, original, staged in transfer["files"]]
    if (stage.is_symlink() or stage.is_junction() or not stage.name.startswith(".yt-downloader-move-") or
            not all(safe_parent(src, source_dir) and safe_parent(dst, target_dir) and dst.parent == target_dir
                    for src, dst, _, _ in files)):
        raise OSError("A transfer directory has changed")
    committed = transfer["phase"] == "commit"
    if committed and any(not has_signature(dst, staged) for _, dst, _, staged in files):
        raise OSError("A published file has changed")
    moved = []
    for src, dst, original, staged in files:
        staged_path = stage / dst.name
        if committed:
            if has_signature(src, original):
                src.unlink()
            if src == files[0][0]:
                moved.append((str(src), str(dst)))
        elif staged and not staged_path.exists() and has_signature(dst, staged):
            dst.unlink()
        staged_path.unlink(missing_ok=True)
    if stage.exists():
        stage.rmdir()
    store.finish_file_move(move["id"], move["video_id"], move["title"], moved)


def stage_file(source, staged):
    try:
        os.link(source, staged)
    except OSError:
        # FAT/exFAT, network shares and other volumes require a copy.
        with source.open("rb") as reader, staged.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        shutil.copystat(source, staged)


def move_selected_group(store, group):
    source_dir = group.files[0].parent
    stage = group.target / f".yt-downloader-move-{uuid4().hex}"
    files = [[str(src), str(group.target / src.name), list(expected), None]
             for src, expected in zip(group.files, group.signatures, strict=True)]
    for src, dst, expected, _ in files:
        src, dst = Path(src), Path(dst)
        if (not safe_parent(src, source_dir) or not safe_parent(dst, group.target) or
                not has_signature(src, expected) or dst.exists() or dst.is_symlink()):
            raise OSError("The source or destination changed since the preview")
    transfer = {"phase": "stage", "stage": str(stage), "files": files}
    move_id = store.record_file_move(source_dir, group.video_id, group.title, transfer)
    move = {"id": move_id, "root": str(source_dir), "video_id": group.video_id, "title": group.title}
    stage.mkdir()
    try:
        for file in files:
            src, dst, expected, _ = file
            staged = stage / Path(dst).name
            stage_file(Path(src), staged)
            file[3] = signature(staged)
            if file[3][2] != expected[2] or not has_signature(Path(src), expected):
                raise OSError(f"The source file changed during transfer: {src}")
        store.update_file_move(move_id, transfer)
        for _, dst, _, _ in files:
            rename_file(stage / Path(dst).name, Path(dst))
        store.update_file_move(move_id, {**transfer, "phase": "commit"})
        transfer["phase"] = "commit"
    finally:
        settle_selected(store, move, transfer)


def recover_file_moves(store):
    for move in store.pending_file_moves():
        try:
            files = json.loads(move["files"])
            if isinstance(files, dict):
                settle_selected(store, move, files)
            else:
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
            if root is None:
                move_selected_group(store, group)
                moved += len(group.files)
                continue
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
