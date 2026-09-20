"""SQLite queue for ONE host/worker slot. No automatic retries of completed jobs."""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .models import SubmitRequest
from .optimizer import optimize

ACTIVE = ("STARTING", "RUNNING", "RECOVERY_REQUIRED")
TERMINAL = ("SUCCEEDED", "FAILED", "TIMED_OUT", "MISSED_WINDOW", "MISSED_DEADLINE", "CANCELLED", "LOST")


class Conflict(ValueError):
    pass


def default_path():
    return Path(os.environ.get("CARBONSHIFT_DB", str(Path.home() / ".local/share/carbonshift/jobs.sqlite3"))).expanduser().resolve()


class Store:
    def __init__(self, path=None):
        self.path = Path(path).expanduser().resolve() if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL, scheduled_start REAL NOT NULL,
                    scheduled_end REAL NOT NULL, deadline REAL NOT NULL, started_at REAL, finished_at REAL,
                    container_name TEXT UNIQUE NOT NULL, container_id TEXT, exit_code INTEGER,
                    stop_reason TEXT, error TEXT, logs TEXT NOT NULL DEFAULT '', snapshot TEXT NOT NULL,
                    recommendation TEXT NOT NULL, evidence TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                    at REAL NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS due_jobs ON jobs(state, scheduled_start);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")
            if db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] != "1":
                raise RuntimeError("Unsupported queue schema version.")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('queue_id', ?)", (uuid.uuid4().hex,))
            self.queue_id = db.execute("SELECT value FROM metadata WHERE key='queue_id'").fetchone()[0]

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def decode(self, row):
        if row is None:
            return None
        result = dict(row)
        for key in ("snapshot", "recommendation", "evidence"):
            if result[key] is not None:
                result[key] = json.loads(result[key])
        return result

    def get(self, job_id):
        with self.connect() as db:
            result = self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if result is None:
                raise KeyError(job_id)
            result["events"] = [dict(row) for row in db.execute("SELECT at,state,message FROM events WHERE job_id=? ORDER BY id", (job_id,))]
            return result

    def list(self, limit=100):
        with self.connect() as db:
            rows = db.execute("SELECT id,state,created_at,scheduled_start,scheduled_end,started_at,finished_at,exit_code,error,snapshot FROM jobs ORDER BY created_at DESC LIMIT ?", (min(100, max(1, limit)),))
            result = []
            for row in rows:
                item = dict(row)
                item["name"] = json.loads(item.pop("snapshot"))["optimization"]["job"]["name"]
                result.append(item)
            return result

    def heartbeat(self, state, details=None, now=None):
        payload = {"state": state, "at": time.time() if now is None else now,
                   "pid": os.getpid(), **(details or {})}
        with self.connect() as db:
            db.execute("INSERT INTO metadata(key,value) VALUES ('worker',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(payload),))

    def status(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='worker'").fetchone()
            worker = json.loads(row[0]) if row else {"state": "unknown", "at": None}
            counts = {row["state"]: row["n"] for row in db.execute("SELECT state,COUNT(*) AS n FROM jobs GROUP BY state")}
        age = max(0, now - worker["at"]) if worker["at"] is not None else None
        worker["age_seconds"] = age
        worker["online"] = age is not None and age < 45 and worker["state"] not in ("stopped", "unknown")
        return {"worker": worker, "counts": counts, "queue_id": self.queue_id,
                "execution_ready": worker["online"] and worker["state"] == "ready"}

    def submit(self, request: SubmitRequest, now=None):
        now = time.time() if now is None else now
        snapshot = request.model_dump(mode="json")
        # Preserve old checksum idempotency hashes after adding optional image fields.
        canonical_data={k:v for k,v in snapshot.items() if k != "idempotency_key"}
        canonical_data["execution"]=dict(snapshot["execution"])
        if canonical_data["execution"].get("image_batch") is None:
            canonical_data["execution"].pop("image_batch", None)
        canonical = json.dumps(canonical_data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
            if previous:
                if previous["request_hash"] != request_hash:
                    raise Conflict("Idempotency key already belongs to a different request.")
                return self.decode(previous)
            if request.execution.workload == "image-batch":
                from .assets import validate_bundle
                validate_bundle(snapshot["execution"]["image_batch"])
            result = optimize(request.optimization)
            start = datetime.fromisoformat(result["recommended"]["start"]).timestamp()
            end = datetime.fromisoformat(result["recommended"]["finish"]).timestamp()
            if start < now:
                raise Conflict("Selected start is already in the past. Generate a fresh request with a future earliest_start.")
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('STARTING','RUNNING','RECOVERY_REQUIRED') LIMIT 1").fetchone():
                raise Conflict("Worker has active or unresolved work. Submit after it finishes.")
            if db.execute("SELECT 1 FROM jobs WHERE state='SCHEDULED' AND scheduled_start < ? AND scheduled_end > ? LIMIT 1", (end, start)).fetchone():
                raise Conflict("Selected window overlaps a reserved job. Choose another window; no job was queued.")
            job_id = uuid.uuid4().hex
            name = f"carbonshift-{self.queue_id[:8]}-{job_id}"
            db.execute("""INSERT INTO jobs
                (id,idempotency_key,request_hash,state,created_at,scheduled_start,scheduled_end,deadline,container_name,snapshot,recommendation)
                VALUES (?,?,?,'SCHEDULED',?,?,?,?,?,?,?)""",
                (job_id, request.idempotency_key, request_hash, now, start, end, request.optimization.job.deadline.timestamp(), name, json.dumps(snapshot), json.dumps(result)))
            db.execute("INSERT INTO events(job_id,at,state,message) VALUES (?,?,'SCHEDULED',?)", (job_id, now, "Schedule and complete input snapshot persisted."))
        return self.get(job_id)

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT * FROM jobs WHERE state IN ('STARTING','RUNNING','RECOVERY_REQUIRED') ORDER BY scheduled_start LIMIT 1").fetchone()
            if active:
                return self.decode(active)
            row = db.execute("SELECT * FROM jobs WHERE state='SCHEDULED' AND scheduled_start<=? ORDER BY scheduled_start LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET state='STARTING' WHERE id=?", (row["id"],))
            db.execute("INSERT INTO events(job_id,at,state,message) VALUES (?,?,'STARTING',?)", (row["id"], now, "Worker claimed job; reconciling Docker before starting."))
            result = self.decode(row)
            result["state"] = "STARTING"
            return result

    def update(self, job_id, now=None, message=None, **values):
        allowed = {"state", "started_at", "finished_at", "container_id", "exit_code", "stop_reason", "error", "logs", "evidence"}
        if not values or set(values) - allowed:
            raise ValueError("Invalid queue update fields.")
        if "state" in values and values["state"] not in ACTIVE + TERMINAL:
            raise ValueError("Invalid worker state.")
        now = time.time() if now is None else now
        if "evidence" in values:
            values["evidence"] = json.dumps(values["evidence"], allow_nan=False)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] in TERMINAL:
                raise Conflict("Terminal jobs are immutable; submit a new job for another run.")
            assignments = ",".join(f"{key}=?" for key in values)
            db.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values.values(), job_id))
            if message:
                db.execute("INSERT INTO events(job_id,at,state,message) VALUES (?,?,?,?)", (job_id, now, values.get("state", row["state"]), message))

    def cancel(self, job_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["state"] != "SCHEDULED":
                raise Conflict("Only a pending SCHEDULED job can be cancelled. Running jobs remain supervised until completion or timeout.")
            db.execute("UPDATE jobs SET state='CANCELLED',finished_at=? WHERE id=?", (time.time(), job_id))
            db.execute("INSERT INTO events(job_id,at,state,message) VALUES (?,?,'CANCELLED','Cancelled before claim.')", (job_id, time.time()))
        return self.get(job_id)
