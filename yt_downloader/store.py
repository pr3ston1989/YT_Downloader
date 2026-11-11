from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sqlite3
import time

from .model import Entry, Options, Preferences


@dataclass(frozen=True)
class Job:
    id: int
    entry: Entry
    options: Options
    state: str
    attempts: int
    recovery: int
    reason: str
    path: str


class Store:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "data.sqlite3", timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT NOT NULL, entry TEXT NOT NULL, options TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                recovery INTEGER NOT NULL DEFAULT 0, ready REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS job_state ON jobs (state, ready);
            CREATE INDEX IF NOT EXISTS job_video ON jobs (video_id, state);
            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, path TEXT NOT NULL, time REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS updates (
                time REAL NOT NULL, component TEXT NOT NULL, version TEXT NOT NULL,
                status TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, tabs TEXT NOT NULL,
                hours INTEGER NOT NULL, due REAL NOT NULL, UNIQUE(url, tabs)
            );
        """)

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

    def enqueue(self, entries, options: Options, *, only_new=False):
        encoded = json.dumps(asdict(options), ensure_ascii=False)
        count = 0
        with self.db:
            for entry in entries:
                if only_new and self.db.execute("""
                    SELECT id FROM downloads WHERE id=?
                    UNION ALL SELECT video_id FROM jobs WHERE video_id=? AND state NOT IN ('skipped','failed')
                """, (entry.id, entry.id)).fetchone():
                    continue
                self.db.execute("INSERT INTO jobs (video_id,entry,options) VALUES (?,?,?)",
                                (entry.id, json.dumps(asdict(entry), ensure_ascii=False), encoded))
                count += 1
        return count

    @staticmethod
    def job(row):
        return Job(row["id"], Entry(**json.loads(row["entry"])), Options(**json.loads(row["options"])),
                   row["state"], row["attempts"], row["recovery"], row["reason"], row["path"])

    def jobs(self):
        return [self.job(row) for row in self.db.execute("SELECT * FROM jobs ORDER BY id")]

    def skip_downloaded(self):
        with self.db:
            return self.db.execute("""
                UPDATE jobs SET state='skipped', reason=? WHERE state='queued'
                    AND NOT json_extract(options, '$.force')
                    AND EXISTS (SELECT 1 FROM downloads WHERE id=jobs.video_id)
            """, ("Film jest już w historii.",)).rowcount

    def claim(self):
        with self.db:
            row = self.db.execute("""
                SELECT j.* FROM jobs j
                WHERE j.state='queued' AND j.ready<=?
                    AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.video_id=j.video_id AND r.state='running')
                ORDER BY j.id LIMIT 1
            """, (time.time(),)).fetchone()
            if row:
                self.db.execute("UPDATE jobs SET state='running', reason='' WHERE id=?", (row["id"],))
                return self.job(row)
        return None

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

    def retry_failed(self):
        with self.db:
            self.db.execute("UPDATE jobs SET state='queued', reason='', attempts=0, recovery=0, ready=0 "
                            "WHERE state='failed'")

    def clear_jobs(self):
        with self.db:
            self.db.execute("DELETE FROM jobs WHERE state IN ('done','skipped','failed')")

    def history(self):
        return self.db.execute("SELECT * FROM downloads ORDER BY time DESC").fetchall()

    def clear_history(self):
        with self.db:
            self.db.execute("DELETE FROM downloads")

    def record_update(self, component, version, status, detail=""):
        with self.db:
            self.db.execute("INSERT INTO updates VALUES (?,?,?,?,?)",
                            (time.time(), component, version, status, detail))

    def update_history(self):
        return self.db.execute("SELECT * FROM updates ORDER BY rowid DESC LIMIT 500").fetchall()

    def add_subscription(self, url, tabs, hours):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO subscriptions (url,tabs,hours,due) VALUES (?,?,?,0)",
                            (url, json.dumps(tabs), hours))

    def subscriptions(self):
        return self.db.execute("SELECT * FROM subscriptions ORDER BY id").fetchall()

    def subscription_due(self):
        return self.db.execute("SELECT * FROM subscriptions WHERE due<=? ORDER BY due LIMIT 1",
                               (time.time(),)).fetchone()

    def postpone_subscription(self, sub_id, seconds):
        with self.db:
            self.db.execute("UPDATE subscriptions SET due=? WHERE id=?", (time.time() + seconds, sub_id))

    def remove_subscription(self, sub_id):
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
