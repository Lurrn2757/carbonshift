"""Local observation journal. Never opens or writes the scheduling database."""

import fcntl
import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .models import LiveCarbonReading

STALE_AFTER_SECONDS = 3 * 3600  # Local display policy, not a provider SLA.


def observation_path():
    return Path(os.environ.get("CARBONSHIFT_OBSERVATIONS", str(Path.home() / ".local/share/carbonshift/carbon_observations.jsonl"))).expanduser().resolve()


def _validated_path(path=None):
    target = Path(path).expanduser().resolve() if path else observation_path()
    queue = Path(os.environ.get("CARBONSHIFT_DB", str(Path.home() / ".local/share/carbonshift/jobs.sqlite3"))).expanduser().resolve()
    if target == queue or (target.exists() and queue.exists() and os.path.samefile(target, queue)):
        raise ValueError("Observation log must be separate from the queue database.")
    if target.suffix != ".jsonl":
        raise ValueError("Observation log must use a .jsonl file.")
    if target.exists():
        with target.open("rb") as stream:
            if stream.read(16) == b"SQLite format 3\x00":
                raise ValueError("Refusing to use a SQLite database as an observation log.")
    return target


def append_observation(reading: LiveCarbonReading, path=None):
    target = _validated_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # flock coordinates threads/processes. Refuse a damaged trailing record
    # rather than joining valid JSON onto a partial write from an earlier crash.
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                raise ValueError("Observation log has an incomplete final line. Preserve it and select a new .jsonl log before fetching again.")
        stream.write((reading.model_dump_json() + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    return target


def read_observations(limit=50, path=None):
    if not 1 <= limit <= 1000:
        raise ValueError("Log limit must be between 1 and 1000.")
    target = _validated_path(path)
    rows = deque(maxlen=limit)
    warnings, count = [], 0
    if target.exists():
        with target.open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH)
            for lineno, line in enumerate(stream, 1):
                try:
                    item = LiveCarbonReading.model_validate_json(line)
                except ValueError:
                    if len(warnings) < 10:
                        warnings.append(f"Invalid observation at line {lineno}; preserved on disk and excluded from results.")
                    continue
                count += 1
                rows.append(item)
    return {"path": str(target), "total_valid_records": count, "records": [reading_view(r) for r in reversed(rows)], "warnings": warnings}


def reading_view(reading, now=None):
    now = now or datetime.now(timezone.utc)
    age = max(0, (now - reading.data_at).total_seconds())
    return {**reading.model_dump(mode="json"), "age_seconds": age,
            "stale": age > STALE_AFTER_SECONDS, "stale_after_seconds": STALE_AFTER_SECONDS}
