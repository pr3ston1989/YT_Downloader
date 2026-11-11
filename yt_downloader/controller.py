from concurrent.futures import Future
from dataclasses import asdict, dataclass, field, replace
import json
import logging
from pathlib import Path
import shutil
from threading import Thread
import time

from PySide6.QtCore import QObject, QTimer, Signal

from .model import Entry, Source, parse_source
from .process import Worker
from .store import Job, Store
from .updates import EngineManager
from .updates.application import check_app, read_result
from .updates.manager import COMPONENTS


LOG = logging.getLogger(__name__)
CHECK_INTERVAL = 6 * 3600
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


@dataclass
class Scan:
    source: Source
    tabs: tuple
    subscription: dict | None
    recovery: int = 0
    worker: Worker | None = None
    entries: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    outcome: dict = field(default_factory=dict)


class Controller(QObject):
    changed = Signal()
    progress = Signal(int, object)
    scanned = Signal(object)
    scan_progress = Signal(int)
    message = Signal(str)
    login_needed = Signal(str, str, bool)
    update_ready = Signal(object)

    def __init__(self, root: Path, *, smoke=False):
        super().__init__()
        self.root = root
        for path in (root / "workers").glob("worker-*"):
            shutil.rmtree(path, ignore_errors=True)
        self.store = Store(root)
        self.prefs = self.store.preferences()
        self.manager = EngineManager(root / "engine", self.record)
        self.active = {}
        self.scan = None
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
            if self.scan and not self.scan.worker:
                self.start_scan_worker()
            return
        if mode == "repair":
            if None in repairs and self.scan and not self.scan.worker:
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
            scan.entries.clear()
            scan.warnings.clear()
            scan.outcome = {}
            scan.worker = Worker(self.root / "workers", self.request(
                "scan", source=asdict(scan.source), tabs=scan.tabs))
        except Exception:
            LOG.exception("Nie udało się uruchomić odczytu listy")
            self.finish_scan({"kind": "error", "message": "Nie udało się uruchomić odczytu listy."})

    def cancel_scan(self):
        scan, self.scan = self.scan, None
        self.pending_repairs.pop(None, None)
        if scan and scan.worker:
            scan.worker.close()
        self.scanned.emit({"cancelled": True})

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
            self.scan_progress.emit(len(scan.entries))
        if finished:
            worker.close()
            scan.worker = None
            event = scan.outcome or {"kind": "error", "message": "Proces podglądu został przerwany."}
            if event.get("code") == "extractor" and self.prefs.auto_update and scan.recovery < 2:
                self.pending_repairs[None] = self.recovery_channel(scan.recovery)
                scan.recovery += 1
                self.message.emit("Sprawdzam silnik przed ponownym odczytem listy.")
                return
            self.finish_scan(event)

    def finish_scan(self, event):
        scan, self.scan = self.scan, None
        if event.get("detail"):
            LOG.warning("Odczyt listy / %s", event["detail"])
        successful = event["kind"] == "done"
        code = event.get("code")
        if scan.subscription:
            row = scan.subscription
            self.store.postpone_subscription(row["id"], row["hours"] * 3600 if successful else 900)
            if successful and scan.entries:
                options = replace(self.prefs.options, force=False)
                count = self.store.enqueue(scan.entries, options, only_new=True)
                if count:
                    self.message.emit(f"Subskrypcja dodała {count} nowych pozycji.")
            self.changed.emit()
        else:
            needs_login = code in ("auth", "cookies", "playlist_access")
            self.scanned.emit({**event, "entries": scan.entries, "warnings": scan.warnings,
                               "login_retry": (scan.source, scan.tabs) if needs_login else None})
        if code in ("auth", "cookies") or (code == "playlist_access" and not scan.subscription):
            self.login_needed.emit(scan.source.url, event["message"], code == "auth")

    def recovery_channel(self, recovery):
        return "nightly" if recovery or self.prefs.channel == "nightly" else "stable"

    def poll_jobs(self):
        changed = False
        for job_id, item in list(self.active.items()):
            finished = item.worker.poll() is not None
            for event in item.worker.events():
                kind = event["kind"]
                if kind in ("progress", "processing") and not item.stopped:
                    self.progress.emit(job_id, event)
                elif kind == "log":
                    LOG.warning("Film %s / %s", item.job.entry.id, event["detail"])
                elif kind in ("done", "error"):
                    item.outcome = event
            if not finished:
                continue
            item.worker.close()
            del self.active[job_id]
            if not item.stopped:
                self.finish_job(item)
            changed = True
        if changed:
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
            self.login_needed.emit(f"https://www.youtube.com/watch?v={job.entry.id}", reason, code == "auth")
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

    def retry_auth(self):
        self.store.retry_auth()
        self.changed.emit()

    def tick(self):
        try:
            self.step()
        except Exception:
            LOG.exception("Błąd sterowania kolejką")
            self.message.emit("Błąd kolejki. Szczegóły są w pliku logów.")

    def step(self):
        self.poll_jobs()
        self.poll_scan()
        self.poll_update()
        if self.install_pending:
            if not self.active and not self.scan and self.future is None:
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
            self.preview(parse_source(row["url"]), json.loads(row["tabs"]), dict(row))
        if self.paused or self.install_pending or any(item.stopped for item in self.active.values()):
            return
        changed = self.store.skip_downloaded()
        while len(self.active) < self.prefs.parallel:
            job = self.store.claim()
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
        if self.scan and self.scan.worker:
            self.scan.worker.close()
        for item in self.active.values():
            item.worker.close()
        self.store.recover_interrupted()
        self.store.close()
