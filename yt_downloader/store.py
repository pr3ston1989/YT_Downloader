from dataclasses import asdict, dataclass, replace
from itertools import repeat
import json
from pathlib import Path
import sqlite3
import time

from .model import DEFAULT_TEMPLATE, EDITABLE_STATES, LEGACY_TEMPLATE, Entry, Options, Preferences, VIDEO_ID, WAITING_STATES


@dataclass(frozen=True)
class Job:
    id: int
    entry: Entry
    options: Options
    state: str
    attempts: int
    recovery: int
    auth_attempts: int
    reason: str
    path: str
    package_id: int | None = None


@dataclass(frozen=True)
class Package:
    id: int
    name: str
    paused: bool


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "data.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            PRAGMA foreign_keys = ON;
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, paused INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL, entry TEXT NOT NULL, options TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                recovery INTEGER NOT NULL DEFAULT 0, auth_attempts INTEGER NOT NULL DEFAULT 0,
                ready REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '', queue_position INTEGER
            );
            CREATE INDEX IF NOT EXISTS job_state ON jobs (state, ready);
            CREATE INDEX IF NOT EXISTS job_video ON jobs (video_id, state);
            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL, time REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_moves (
                id INTEGER PRIMARY KEY, root TEXT NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL,
                files TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS updates (
                time REAL NOT NULL, component TEXT NOT NULL, version TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, tabs TEXT NOT NULL,
                hours INTEGER NOT NULL, due REAL NOT NULL,
                needs_login INTEGER NOT NULL DEFAULT 0, music INTEGER NOT NULL DEFAULT 0,
                UNIQUE(url, tabs)
            );
        """)
        columns = {column["name"] for column in self.db.execute("PRAGMA table_info(jobs)")}
        if "auth_attempts" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN auth_attempts INTEGER NOT NULL DEFAULT 0")
        if "queue_position" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN queue_position INTEGER")
        if "package_id" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN package_id INTEGER REFERENCES packages(id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS job_package ON jobs (package_id)")
        columns = {column["name"] for column in self.db.execute("PRAGMA table_info(subscriptions)")}
        for name in ("needs_login", "music"):
            if name not in columns:
                self.db.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        if self.db.execute("PRAGMA user_version").fetchone()[0] == 0:
            with self.db:
                self.db.execute("""
                    UPDATE preferences SET value=json_set(value, '$.options.template', ?)
                    WHERE key='settings' AND replace(json_extract(value, '$.options.template'), char(92), '/')=?
                """, (DEFAULT_TEMPLATE, LEGACY_TEMPLATE))
                self.db.execute("PRAGMA user_version = 1")

    def close(self):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM preferences WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO preferences VALUES (?,?)", (key, json.dumps(value)))

    def preferences(self):
        return Preferences.from_dict(self.get("settings", {}))

    def packages(self):
        return {row["id"]: Package(row["id"], row["name"], bool(row["paused"]))
                for row in self.db.execute("SELECT * FROM packages ORDER BY id")}

    def pause_package(self, package_id, paused):
        with self.db:
            self.db.execute("UPDATE packages SET paused=? WHERE id=?", (paused, package_id))

    def assign_package(self, ids, package_id, *, name=None):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.waiting_rows()
            selected = set(ids)
            if name is not None:
                package_id = self.db.execute("INSERT INTO packages (name) VALUES (?)", (name,)).lastrowid
            count = self.db.execute("""
                UPDATE jobs SET package_id=? WHERE state!='running' AND id IN (SELECT value FROM json_each(?))
            """, (package_id, json.dumps(list(selected)))).rowcount
            if package_id is not None:
                selected_waiting = {row["id"] for row in rows if row["id"] in selected and row["package_id"] != package_id}
                members = [row["id"] for row in rows if row["package_id"] == package_id]
                self.join_waiting(rows, selected_waiting, members)
            self.delete_empty_packages()
            return count

    def enqueue(self, entries, options: Options, *, only_new=False, first=False, overrides=None, package_id=None):
        count, _ = self.enqueue_batch(entries, options, only_new=only_new, first=first, overrides=overrides,
                                      package_id=package_id)
        return count

    def enqueue_batch(self, entries, options: Options, *, only_new=False, first=False, overrides=None,
                      package_id=None, package_name=None):
        encoded = json.dumps(asdict(options), ensure_ascii=False)
        per_entry = {video_id: json.dumps(asdict(value), ensure_ascii=False)
                     for video_id, value in (overrides or {}).items()}
        with self.db:
            positions = repeat(None)
            existing = []
            if first or package_id is not None:
                self.db.execute("BEGIN IMMEDIATE")
            if first:
                entries = list(entries)
                # Normal jobs sort by their positive ID. Reserve earlier positions for this batch.
                smallest = self.db.execute("SELECT MIN(queue_position) FROM jobs").fetchone()[0]
                end = min(0, smallest or 0)
                positions = range(end - len(entries), end)
            elif package_id is not None:
                existing = [row["id"] for row in self.waiting_rows() if row["package_id"] == package_id]
            if existing:
                previous_id = self.db.execute("SELECT MAX(id) FROM jobs").fetchone()[0]
            if package_name is not None:
                package_id = self.db.execute("INSERT INTO packages (name) VALUES (?)", (package_name,)).lastrowid
            rows = ((entry.id, json.dumps(asdict(entry), ensure_ascii=False), per_entry.get(entry.id, encoded),
                     position, package_id)
                    for entry, position in zip(entries, positions))
            if only_new:
                count = self.db.executemany("""
                    INSERT INTO jobs (video_id,entry,options,queue_position,package_id)
                    SELECT ?1, ?2, ?3, ?4, ?5
                    WHERE NOT EXISTS (SELECT 1 FROM downloads WHERE id=?1)
                    AND NOT EXISTS (SELECT 1 FROM jobs WHERE video_id=?1 AND state NOT IN ('skipped','failed'))
                """, rows).rowcount
            else:
                count = self.db.executemany("""
                    INSERT INTO jobs (video_id,entry,options,queue_position,package_id) VALUES (?,?,?,?,?)
                """, rows).rowcount
            if existing and count:
                waiting = self.waiting_rows()
                self.join_waiting(waiting, {row["id"] for row in waiting if row["id"] > previous_id}, existing)
            if not count and package_name is not None:
                self.db.execute("DELETE FROM packages WHERE id=?", (package_id,))
                package_id = None
            return count, package_id

    @staticmethod
    def job(row):
        return Job(row["id"], Entry(**json.loads(row["entry"])), Options(**json.loads(row["options"])),
                   row["state"], row["attempts"], row["recovery"], row["auth_attempts"],
                   row["reason"], row["path"], row["package_id"])

    def jobs(self):
        return [self.job(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY COALESCE(queue_position, id)")]

    def waiting_rows(self):
        placeholders = ",".join("?" for _ in WAITING_STATES)
        return self.db.execute(f"""
                SELECT id, package_id, COALESCE(queue_position, id) AS position FROM jobs
                WHERE state IN ({placeholders}) ORDER BY position
        """, WAITING_STATES).fetchall()

    def write_order(self, rows, order):
        return self.db.executemany("UPDATE jobs SET queue_position=? WHERE id=?",
                                   ((row["position"], job_id) for row, job_id in zip(rows, order)
                                    if row["id"] != job_id)).rowcount

    def join_waiting(self, rows, selected, existing):
        if not selected:
            return
        order = [row["id"] for row in rows if row["id"] not in selected]
        end = (order.index(existing[-1]) + 1 if existing else
               next(i for i, row in enumerate(rows) if row["id"] in selected))
        order[end:end] = [row["id"] for row in rows if row["id"] in selected]
        self.write_order(rows, order)

    def move_jobs(self, ids, direction):
        selected = set(ids)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.waiting_rows()
            order = [row["id"] for row in rows]
            if direction == "first":
                order.sort(key=lambda job_id: job_id not in selected)
            elif direction == "last":
                order.sort(key=lambda job_id: job_id in selected)
            elif direction in ("up", "down"):
                up = direction == "up"
                indexes = range(1, len(order)) if up else range(len(order) - 2, -1, -1)
                for index in indexes:
                    neighbor = index - 1 if up else index + 1
                    if order[index] in selected and order[neighbor] not in selected:
                        order[index], order[neighbor] = order[neighbor], order[index]
            else:
                raise ValueError(f"Unknown queue move: {direction}")
            return self.write_order(rows, order)

    def move_package(self, package_id, direction):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.waiting_rows()
            members = [row["id"] for row in rows if row["package_id"] == package_id]
            if not members:
                return 0
            units = list(dict.fromkeys((row["package_id"], 0 if row["package_id"] else row["id"]) for row in rows))
            current = units.index((package_id, 0))
            target = {"first": 0, "up": max(0, current - 1), "down": min(len(units) - 1, current + 1),
                      "last": len(units) - 1}[direction]
            if current == target and direction in ("up", "down"):
                return 0
            neighbor, loose = units[target]
            remaining = [row for row in rows if row["package_id"] != package_id]
            edges = [i for i, row in enumerate(remaining)
                     if (row["package_id"] == neighbor if neighbor else row["id"] == loose)]
            if direction == "first":
                index = 0
            elif direction == "last":
                index = len(remaining)
            else:
                index = edges[0] if target < current else edges[-1] + 1
            order = [row["id"] for row in remaining]
            order[index:index] = members
            return self.write_order(rows, order)

    def skip_downloaded(self, excluded=()):
        with self.db:
            return self.db.execute("""
                UPDATE jobs SET state='skipped', reason=? WHERE state='queued'
                    AND id NOT IN (SELECT value FROM json_each(?))
                    AND NOT json_extract(options, '$.force')
                    AND EXISTS (SELECT 1 FROM downloads WHERE id=jobs.video_id)
            """, ("Film jest już w historii.", json.dumps(list(excluded)))).rowcount

    def claim(self, excluded=(), moving=()):
        with self.db:
            row = self.db.execute("""
                UPDATE jobs SET state='running', reason='' WHERE id=(
                    SELECT j.id FROM jobs j WHERE j.state='queued' AND j.ready<=?
                    AND j.id NOT IN (SELECT value FROM json_each(?))
                    AND j.video_id NOT IN (SELECT value FROM json_each(?))
                    AND NOT EXISTS (SELECT 1 FROM packages p WHERE p.id=j.package_id AND p.paused)
                    AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.video_id=j.video_id AND r.state='running')
                    ORDER BY COALESCE(j.queue_position, j.id) LIMIT 1
                ) RETURNING *
            """, (time.time(), json.dumps(list(excluded)), json.dumps(list(moving)))).fetchone()
            return self.job(row) if row else None

    def state(self, job_id, state, reason="", *, delay=0, attempts=None, recovery=None):
        with self.db:
            self.db.execute("""
                UPDATE jobs SET state=?, reason=?, ready=?, attempts=COALESCE(?, attempts),
                    recovery=COALESCE(?, recovery) WHERE id=?
            """, (state, reason, time.time() + delay, attempts, recovery, job_id))

    def complete(self, job: Job, path: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO downloads VALUES (?,?,?,?)",
                            (job.entry.id, job.entry.title, path, time.time()))
            self.db.execute("UPDATE jobs SET state='done', path=?, reason='' WHERE id=?", (path, job.id))

    def recover_interrupted(self):
        with self.db:
            self.db.execute("UPDATE jobs SET state='queued', ready=0 WHERE state IN ('running','repair')")

    def retry_failed(self, ids=None):
        selection = None if ids is None else json.dumps(list(ids))
        with self.db:
            return self.db.execute("""
                UPDATE jobs SET state='queued', reason='', attempts=0, recovery=0, auth_attempts=0, ready=0
                WHERE state='failed'
                    AND (?1 IS NULL OR id IN (SELECT value FROM json_each(?1)))
            """, (selection,)).rowcount

    def edit_job_options(self, jobs, changes):
        if not changes:
            return 0
        rows = []
        for job in jobs:
            options = replace(job.options, **changes)
            options.validate()
            rows.append((json.dumps(asdict(options), ensure_ascii=False), job.id, *EDITABLE_STATES))
        states = ",".join("?" for _ in EDITABLE_STATES)
        with self.db:
            return self.db.executemany(f"UPDATE jobs SET options=? WHERE id=? AND state IN ({states})", rows).rowcount

    def retry_auth(self):
        with self.db:
            self.db.execute("""
                UPDATE jobs SET state='queued', reason='', ready=0, auth_attempts=auth_attempts+1
                WHERE state='auth'
            """)
            self.db.execute("UPDATE subscriptions SET needs_login=0, due=0 WHERE needs_login=1")

    def clear_jobs(self):
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE state IN ('done','skipped','failed')")
            self.delete_empty_packages()

    def remove_jobs(self, ids):
        with self.db:
            count = self.db.execute("DELETE FROM jobs WHERE id IN (SELECT value FROM json_each(?))",
                                    (json.dumps(list(ids)),)).rowcount
            self.delete_empty_packages()
            return count

    def delete_empty_packages(self):
        self.db.execute("DELETE FROM packages WHERE NOT EXISTS (SELECT 1 FROM jobs WHERE package_id=packages.id)")

    def history(self):
        return self.db.execute("SELECT * FROM downloads ORDER BY time DESC").fetchall()

    def record_file_move(self, root, video_id, title, files):
        with self.db:
            return self.db.execute("INSERT INTO file_moves (root,video_id,title,files) VALUES (?,?,?,?)",
                                   (str(root), video_id, title, json.dumps(files))).lastrowid

    def pending_file_moves(self):
        return self.db.execute("SELECT * FROM file_moves ORDER BY id").fetchall()

    def update_file_move(self, move_id, files):
        with self.db:
            self.db.execute("UPDATE file_moves SET files=? WHERE id=?", (json.dumps(files), move_id))

    def finish_file_move(self, move_id, video_id, title, paths):
        with self.db:
            for source, target in paths:
                self.db.execute("""
                    INSERT INTO downloads (id,title,path,time) VALUES (?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET path=excluded.path
                    WHERE downloads.path=? COLLATE NOCASE OR downloads.path=''
                """, (video_id, title, target, time.time(), source))
                self.db.execute("UPDATE jobs SET path=? WHERE video_id=? AND path=? COLLATE NOCASE",
                                (target, video_id, source))
            self.db.execute("DELETE FROM file_moves WHERE id=?", (move_id,))

    def import_archive(self, lines):
        ids = []
        ignored = 0
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) == 2 and fields[0].lower() == "youtube" and VIDEO_ID.fullmatch(fields[1]):
                ids.append(fields[1])
            else:
                ignored += 1
        imported_at = time.time()
        with self.db:
            added = self.db.executemany("""
                INSERT INTO downloads (id,title,path,time) VALUES (?,'','',?) ON CONFLICT(id) DO NOTHING
            """, ((video_id, imported_at) for video_id in ids)).rowcount
        return added, len(ids) - added, ignored

    def clear_history(self):
        with self.db:
            self.db.execute("DELETE FROM downloads")

    def record_update(self, component, version, status, detail=""):
        with self.db:
            self.db.execute("INSERT INTO updates VALUES (?,?,?,?,?)",
                            (time.time(), component, version, status, detail))

    def update_history(self):
        return self.db.execute("SELECT * FROM updates ORDER BY rowid DESC LIMIT 500").fetchall()

    def add_subscription(self, url, tabs, hours, *, music=False):
        with self.db:
            self.db.execute("""
                INSERT INTO subscriptions (url,tabs,hours,due,music) VALUES (?,?,?,0,?)
                ON CONFLICT(url,tabs) DO UPDATE SET hours=excluded.hours, music=excluded.music
            """, (url, json.dumps(tabs), hours, music))

    def subscriptions(self):
        return self.db.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()

    def subscription_due(self):
        return self.db.execute("SELECT * FROM subscriptions WHERE needs_login=0 AND due<=? ORDER BY due,id LIMIT 1",
                               (time.time(),)).fetchone()

    def subscription_needing_login(self):
        return self.db.execute("SELECT * FROM subscriptions WHERE needs_login=1 ORDER BY id LIMIT 1").fetchone()

    def postpone_subscription(self, sub_id, seconds, *, needs_login=False):
        with self.db:
            return self.db.execute("UPDATE subscriptions SET due=?, needs_login=? WHERE id=?",
                                   (time.time() + seconds, needs_login, sub_id)).rowcount != 0

    def remove_subscription(self, sub_id):
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
