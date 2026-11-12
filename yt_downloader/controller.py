from concurrent.futures import Future
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
import json
import logging
from pathlib import Path
import shutil
from threading import Thread
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .model import Entry, Source, parse_source
from .organize import apply_plan, recover_file_moves
from .process import Worker
from .store import Job, Store
from .updates import EngineManager
from .updates.application import check_app, read_result
from .updates.manager import COMPONENTS


LOG = logging.getLogger(__name__)
CHECK_INTERVAL = 6 * 3600
LOGIN_ERRORS = ("auth", "cookies")
LABELS = {"queued": "W kolejce", "running": "Pobieranie", "paused": "Wstrzymano",
          "repair": "Aktualizacja silnika", "auth": "Wymaga logowania",
          "done": "Gotowe", "skipped": "Pominięto", "failed": "Nieudane"}


@dataclass
class Running:
    worker: Worker
    job: Job
    engine_version: str
    outcome: dict = field(default_factory=dict)
    stopped: bool = False


@dataclass(eq=False)
class Scan:
    source: Source
    tabs: tuple
    subscription: dict | None
    recovery: int = 0
    worker: Worker | None = None
    entries: list = field(default_factory=list)
    previous_entries: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    outcome: dict = field(default_factory=dict)


class Controller(QObject):
    changed = Signal()
    progress = Signal(int, object)
    scanned = Signal(object)
    scan_entries = Signal(int, object)
    media_inspected = Signal(object)
    message = Signal(str)
    login_needed = Signal(str, str, bool)
    update_ready = Signal(object)

    def __init__(self, root: Path, *, smoke=False):
        super().__init__()
        self.root = root
        for path in (root / "workers").glob("worker-*"):
            shutil.rmtree(path, ignore_errors=True)
        self.store = Store(root)
        recover_file_moves(self.store)
        self.prefs = self.store.preferences()
        self.manager = EngineManager(root / "engine", self.record)
        self.active = {}
        self.editing_jobs = set()
        self.moving_videos = set()
        self.file_move = None
        self.scan = None
        self.media_worker = None
        self.pending_repairs = {}
        self.smoke = smoke
        self.future = None
        self.next_check = time.time()
        self.manual_check = False
        self.install_pending = None
        self.paused = bool(self.store.get("queue_paused", False))
        self.store.recover_interrupted()
        self.timer = QTimer(self)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.update("boot")

    def record(self, component, version, status, detail=""):
        db = Store(self.root)
        try:
            db.record_update(component, version, status, detail)
        finally:
            db.close()
        LOG.info("Aktualizacje / %s %s / %s / %s", component, version, status, detail)

    def save(self):
        self.store.set("settings", self.prefs.to_dict())

    def check_now(self):
        self.manual_check = True
        self.message.emit("Sprawdzanie aktualizacji jest zaplanowane.")

    def update(self, mode):
        future = Future()
        self.future = future
        auto = self.prefs.auto_update
        selected = self.prefs.channel
        repairs = {}
        if mode == "repair":
            repairs, self.pending_repairs = self.pending_repairs, {}

        def work():
            try:
                candidate = None
                if mode == "boot":
                    read_result(self.root, self.record)
                    self.manager.diagnose()
                if mode == "repair":
                    for channel in dict.fromkeys(repairs.values()):
                        self.manager.check("yt-dlp", channel)
                else:
                    db = Store(self.root)
                    try:
                        last = db.get("last_check", 0)
                        due = time.time() - last >= CHECK_INTERVAL
                        if not self.smoke and (mode == "manual" or not self.manager.snapshot().ready or auto and due):
                            for component in COMPONENTS:
                                self.manager.check(component, selected if component == "yt-dlp" else "stable")
                            if auto or mode == "manual":
                                candidate = check_app(self.manager.http, self.root, self.record)
                            db.set("last_check", time.time())
                            last = time.time()
                    finally:
                        db.close()
                    future.set_result((mode, repairs, candidate, last))
                    return
                future.set_result((mode, repairs, candidate, None))
            except Exception as exc:
                future.set_exception(exc)

        Thread(target=work, daemon=True, name="Aktualizacje").start()

    def poll_update(self):
        if self.future is None or not self.future.done():
            return
        future, self.future = self.future, None
        try:
            mode, repairs, candidate, last = future.result()
        except Exception:
            LOG.exception("Aktualizacja w tle")
            self.next_check = time.time() + 1800
            self.message.emit("Sprawdzanie aktualizacji zakończyło się błędem.")
            for job in self.store.jobs():
                if job.state == "repair":
                    self.store.state(job.id, "queued")
            if self.scan and not self.scan.worker and self.scan not in self.pending_repairs:
                self.start_scan_worker()
            return
        if mode == "repair":
            if self.scan in repairs and not self.scan.worker:
                self.start_scan_worker()
            for job in self.store.jobs():
                if job.id in repairs and job.state == "repair":
                    self.store.state(job.id, "queued")
        else:
            ready = self.manager.snapshot().ready
            self.next_check = max(time.time() + 60, last + CHECK_INTERVAL) if ready else time.time() + 1800
            if mode in ("boot", "manual") or not ready:
                message = ("Zależności są gotowe. Wyniki sprawdzenia znajdziesz w historii aktualizacji."
                           if ready else "Brakuje zależności. Aplikacja ponowi próbę automatycznie.")
                self.message.emit(message)
        if candidate and not self.install_pending and (self.prefs.auto_update or mode == "manual"):
            self.install_pending = candidate
            self.message.emit("Pobrano nową wersję. Aplikacja zrestartuje się po aktywnych zadaniach.")
        self.changed.emit()

    def request(self, operation, **kwargs):
        return {
            "operation": operation,
            "runtime": asdict(self.manager.snapshot()),
            "cache": str(self.root / "cache"),
            "auth": {"browser": self.prefs.browser, "profile": self.prefs.browser_profile,
                     "file": str(self.root / "cookies.dpapi")},
            **kwargs,
        }

    def preview(self, source, tabs, subscription=None):
        if self.scan:
            raise ValueError("Trwa odczytywanie innej listy.")
        if not self.manager.snapshot().ready:
            raise ValueError("Trwa przygotowanie zależności. Spróbuj za chwilę.")
        self.scan = Scan(source, tuple(tabs), subscription)
        self.start_scan_worker()

    def start_scan_worker(self):
        scan = self.scan
        try:
            if len(scan.entries) > len(scan.previous_entries):
                scan.previous_entries = scan.entries
            scan.entries = []
            scan.warnings.clear()
            scan.outcome = {}
            scan.worker = Worker(self.root / "workers", self.request(
                "scan", source=asdict(scan.source), tabs=scan.tabs))
        except Exception:
            LOG.exception("Nie udało się uruchomić odczytu listy")
            self.finish_scan({"kind": "error", "message": "Nie udało się uruchomić odczytu listy."})

    def inspect_media(self, entry):
        if self.media_worker is not None:
            raise ValueError("Trwa odczytywanie formatów.")
        if not self.manager.snapshot().ready:
            raise ValueError("Trwa przygotowanie zależności. Spróbuj za chwilę.")
        try:
            self.media_worker = Worker(self.root / "workers", self.request("inspect", entry=asdict(entry)))
        except OSError as exc:
            LOG.exception("Nie udało się uruchomić odczytu formatów")
            raise ValueError("Nie udało się uruchomić odczytu formatów.") from exc

    def cancel_media_inspection(self):
        if self.media_worker is not None:
            worker, self.media_worker = self.media_worker, None
            worker.close()

    def poll_media_inspection(self):
        if self.media_worker is None or self.media_worker.poll() is None:
            return
        result = {"kind": "error", "message": "Odczyt formatów został przerwany."}
        for event in self.media_worker.events():
            if event["kind"] in ("done", "error"):
                result = event
            elif event["kind"] == "log":
                LOG.warning("Odczyt formatów / %s", event["detail"])
        self.cancel_media_inspection()
        self.media_inspected.emit(result)

    def cancel_scan(self):
        scan = self.scan
        if not scan:
            return
        self.pending_repairs.pop(scan, None)
        if scan.worker:
            scan.worker.close()
        self.finish_scan({"kind": "cancelled", "message": "Odczyt anulowany."})

    def poll_scan(self):
        scan = self.scan
        if not scan or not scan.worker:
            return
        worker = scan.worker
        finished = worker.poll() is not None
        size = len(scan.entries)
        for event in worker.events():
            kind = event["kind"]
            if kind == "entry":
                scan.entries.append(Entry(**event["entry"]))
            elif kind == "scan_warning":
                scan.warnings.append(event)
            elif kind == "log":
                LOG.warning("Podgląd / %s", event["detail"])
            else:
                scan.outcome = event
        if len(scan.entries) != size and not scan.subscription:
            self.scan_entries.emit(size, scan.entries[size:])
        if finished:
            worker.close()
            scan.worker = None
            event = scan.outcome or {"kind": "error", "message": "Proces podglądu został przerwany."}
            if event.get("code") == "extractor" and self.prefs.auto_update and scan.recovery < 2:
                self.pending_repairs[scan] = self.recovery_channel(scan.recovery)
                scan.recovery += 1
                self.message.emit("Sprawdzam silnik przed ponownym odczytem listy.")
                return
            self.finish_scan(event)

    def finish_scan(self, event):
        scan, self.scan = self.scan, None
        if event.get("detail"):
            LOG.warning("Odczyt listy / %s", event["detail"])
        successful = event["kind"] == "done"
        complete = successful and all(warning.get("code") == "missing_tab" for warning in scan.warnings)
        entries = scan.entries
        if not complete and len(scan.previous_entries) > len(entries):
            entries = scan.previous_entries
        code = event.get("code")
        login = event if code in LOGIN_ERRORS else next(
            (warning for warning in scan.warnings if warning.get("code") in LOGIN_ERRORS), None)
        if scan.subscription:
            row = scan.subscription
            active = self.store.postpone_subscription(row["id"], row["hours"] * 3600 if complete else 900,
                                                      needs_login=bool(login))
            if not active:
                self.changed.emit()
                return
            if successful and scan.entries:
                options = replace(self.prefs.options, force=False,
                                  audio_only=scan.source.music or self.prefs.options.audio_only)
                count = self.store.enqueue(scan.entries, options, only_new=True)
                if count:
                    self.message.emit(f"Subskrypcja dodała {count} nowych pozycji.")
            self.changed.emit()
        else:
            if code == "playlist_access":
                login = event
            self.scanned.emit({**event, "entries": entries, "warnings": scan.warnings, "complete": complete,
                               "login_retry": (scan.source, scan.tabs) if login else None})
        if login:
            self.login_needed.emit(scan.source.url, login["message"], login["code"] == "auth")

    def recovery_channel(self, recovery):
        return "nightly" if recovery or self.prefs.channel == "nightly" else "stable"

    def poll_jobs(self, *, closing=False):
        changed = False
        for job_id, item in list(self.active.items()):
            finished = item.worker.poll() is not None
            for event in item.worker.events():
                kind = event["kind"]
                if kind in ("progress", "processing") and not (item.stopped or closing):
                    self.progress.emit(job_id, event)
                elif kind == "log":
                    LOG.warning("Film %s / %s", item.job.entry.id, event["detail"])
                elif kind in ("done", "error"):
                    item.outcome = event
            completed = item.outcome.get("kind") == "done"
            if not finished and not (closing and completed):
                continue
            item.worker.close()
            del self.active[job_id]
            # A pause may arrive after the worker published its completed file but
            # before the next timer tick read the completion event.
            if completed or not (item.stopped or closing):
                self.finish_job(item)
            changed = True
        if changed and not closing:
            self.changed.emit()

    def finish_job(self, item: Running):
        job = item.job
        result = item.outcome
        if result.get("kind") == "done":
            self.store.complete(job, result["path"])
            return
        reason = result.get("message", "Proces pobierania został przerwany.")
        code = result.get("code")
        LOG.warning("Film %s / %s", job.entry.id, result.get("detail", reason))
        if code == "network" and job.attempts < 3:
            delay = 5 * 3 ** job.attempts
            self.store.state(job.id, "queued", f"Ponowienie za {delay} s. {reason}", delay=delay,
                             attempts=job.attempts + 1)
        elif code == "extractor" and self.prefs.auto_update and job.recovery < 2:
            if item.engine_version != self.manager.snapshot().version:
                self.store.state(job.id, "queued", recovery=job.recovery + 1)
                return
            self.store.state(job.id, "repair", reason, recovery=job.recovery + 1)
            self.pending_repairs[job.id] = self.recovery_channel(job.recovery)
        elif code in ("auth", "cookies") and not job.auth_attempts:
            self.store.state(job.id, "auth", reason)
            self.login_needed.emit(job.entry.url, reason, code == "auth")
        else:
            if code == "auth":
                reason += " Ponowienie po zalogowaniu nie dało dostępu."
            self.store.state(job.id, "failed", reason)

    def pause_job(self, job_id):
        item = self.active.get(job_id)
        if item:
            item.stopped = True
            item.worker.stop()
        self.pending_repairs.pop(job_id, None)
        self.store.state(job_id, "paused")
        self.changed.emit()

    def resume_job(self, job_id):
        self.store.state(job_id, "queued")
        self.changed.emit()

    def remove_jobs(self, ids):
        ids = tuple(ids)
        count = self.store.remove_jobs(ids)
        for job_id in ids:
            item = self.active.get(job_id)
            if item and not item.stopped:
                item.stopped = True
                try:
                    item.worker.stop()
                except OSError:
                    LOG.exception("Nie udało się zatrzymać usuwanego zadania %s", job_id)
            self.pending_repairs.pop(job_id, None)
        if count:
            self.changed.emit()
        return count

    def pause_all(self, paused):
        self.paused = paused
        self.store.set("queue_paused", paused)
        if paused:
            for job_id, item in self.active.items():
                if not item.stopped:
                    item.stopped = True
                    item.worker.stop()
                    self.store.state(job_id, "queued")
        self.changed.emit()

    def pause_package(self, package_id, paused):
        self.store.pause_package(package_id, paused)
        if paused:
            for item in self.active.values():
                if item.job.package_id == package_id and not item.stopped:
                    item.stopped = True
                    item.worker.stop()
                    self.store.state(item.job.id, "queued")
        self.changed.emit()

    def move_downloads(self, root, groups):
        if self.file_move is not None:
            self.message.emit("Trwa już przenoszenie plików.")
            return
        active = {item.job.entry.id for item in self.active.values()}
        self.moving_videos = {group.video_id for group in groups if not group.status}
        result = self.file_move = Future()

        def transfer():
            try:
                with closing(Store(self.root)) as store:
                    counts = apply_plan(store, root, groups, active)
                result.set_result(counts)
            except Exception as exc:
                result.set_exception(exc)

        Thread(target=transfer, name="file-move").start()
        self.message.emit("Przenoszenie pobranych plików…")

    def poll_file_move(self):
        if self.file_move is None or not self.file_move.done():
            return
        try:
            moved, errors, busy = self.file_move.result()
            message = f"Przeniesiono plików: {moved}."
            if errors:
                message += f" Grupy z błędem: {errors}. Szczegóły są w logu."
            if busy:
                message += f" Pliki pominięte podczas pobierania: {busy}."
        except Exception:
            LOG.exception("Przenoszenie pobranych plików")
            message = "Nie udało się przenieść plików. Szczegóły są w logu."
        self.file_move = None
        self.moving_videos.clear()
        self.message.emit(message)
        self.changed.emit()

    def retry_auth(self):
        self.store.retry_auth()
        self.changed.emit()

    def remove_subscriptions(self, ids):
        for sub_id in ids:
            self.store.remove_subscription(sub_id)
        if self.scan and self.scan.subscription and self.scan.subscription["id"] in ids:
            self.cancel_scan()
        else:
            self.changed.emit()

    def tick(self):
        try:
            self.step()
        except Exception:
            LOG.exception("Błąd sterowania kolejką")
            self.message.emit("Błąd kolejki. Szczegóły są w pliku logów.")

    def step(self):
        self.poll_jobs()
        self.poll_file_move()
        self.poll_scan()
        self.poll_media_inspection()
        self.poll_update()
        if self.install_pending:
            if not (self.active or self.scan or self.editing_jobs or self.media_worker or self.file_move) and self.future is None:
                candidate, self.install_pending = self.install_pending, None
                self.update_ready.emit(candidate)
            if not self.scan or self.scan.worker:
                return
        if self.future is None:
            if self.pending_repairs:
                self.update("repair")
            elif self.manual_check:
                self.manual_check = False
                self.update("manual")
            elif not self.smoke and time.time() >= self.next_check and (
                    self.prefs.auto_update or not self.manager.snapshot().ready):
                self.update("routine")
        if not self.manager.snapshot().ready:
            return
        if not self.scan and not self.install_pending and (row := self.store.subscription_due()):
            source = replace(parse_source(row["url"]), music=bool(row["music"]))
            self.preview(source, json.loads(row["tabs"]), dict(row))
        if self.paused or self.install_pending or any(item.stopped for item in self.active.values()):
            return
        changed = self.store.skip_downloaded(self.editing_jobs)
        while len(self.active) < self.prefs.parallel:
            job = self.store.claim(self.editing_jobs, self.moving_videos)
            if not job:
                break
            try:
                request = self.request("download", entry=asdict(job.entry), options=asdict(job.options),
                                       job_id=job.id)
                worker = Worker(self.root / "workers", request)
                self.active[job.id] = Running(worker, job, request["runtime"]["version"])
            except Exception:
                LOG.exception("Nie udało się uruchomić zadania %s", job.id)
                self.store.state(job.id, "failed", "Nie udało się uruchomić procesu pobierania.")
            changed = True
        if changed:
            self.changed.emit()

    def close(self):
        self.timer.stop()
        self.poll_jobs(closing=True)
        self.cancel_media_inspection()
        if self.scan and self.scan.worker:
            self.scan.worker.close()
        for item in self.active.values():
            item.worker.close()
        if self.file_move is not None:
            try:
                self.file_move.result()
            except Exception:
                LOG.exception("Zamykanie po błędzie przenoszenia plików")
        self.store.recover_interrupted()
        self.store.close()
