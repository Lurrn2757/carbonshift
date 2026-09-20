"""Restart-aware single-slot worker. Run under a local filesystem lock."""

import argparse
import fcntl
import json
import signal
import threading
import time
import zipfile
from contextlib import contextmanager

from .docker_runner import DockerRunner, RunnerError
from .store import Store


@contextmanager
def worker_lock(path):
    # The database must live on a local filesystem, shared by API and worker.
    with open(str(path) + ".worker.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another worker already owns this queue.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Worker:
    def __init__(self, store, runner, clock=time.time):
        self.store, self.runner, self.clock = store, runner, clock

    def tick(self):
        now = self.clock()
        job = self.store.claim(now)
        if job is None:
            return None
        if job["state"] == "RECOVERY_REQUIRED":
            return job["id"]
        try:
            self.reconcile(job, now)
        except RunnerError as exc:
            # Keep the slot reserved when the daemon's state is uncertain.
            self.store.update(job["id"], now=now, error=str(exc))
        return job["id"]

    def reconcile(self, job, now):
        spec = job["snapshot"]["execution"]
        container = self.runner.inspect(job["container_name"])
        if container is None:
            if job["container_id"] or job["state"] == "RUNNING":
                self.store.update(job["id"], now=now, state="LOST", finished_at=now,
                    error="Tracked container is absent. Its execution outcome is unknown; no automatic rerun.", message="Container lost; preserving audit record.")
                return
            if now + spec["run_seconds"] > min(job["scheduled_end"], job["deadline"]):
                self.store.update(job["id"], now=now, state="MISSED_WINDOW", finished_at=now,
                    error="Worker could not begin within the reserved window.", message="Reserved execution window missed; job did not run.")
                return
            container_id = self.runner.create(job, self.store.queue_id)
            if not container_id:
                raise RunnerError("Docker create returned no container identity.")
            self.store.update(job["id"], now=now, container_id=container_id, error=None, message="Container identity persisted before start.")
            job["container_id"] = container_id
            container = self.runner.inspect(job["container_name"])
            if container is None:
                raise RunnerError("New container not yet observable; will reconcile on next poll.")

        expected = {"carbonshift.queue": self.store.queue_id, "carbonshift.job": job["id"]}
        if any(container.labels.get(key) != value for key, value in expected.items()) or (job["container_id"] and job["container_id"] != container.id):
            self.store.update(job["id"], now=now, state="RECOVERY_REQUIRED", error="Container identity/ownership mismatch. Queue held for operator investigation.", message="Ownership mismatch; no container was started or stopped.")
            return
        if not job["container_id"]:
            self.store.update(job["id"], now=now, container_id=container.id, message="Recovered existing container after an interrupted create.")
            job["container_id"] = container.id

        if container.status in ("exited", "dead"):
            self.finish(job, container, now)
            return
        if container.status == "created":
            if now + spec["run_seconds"] > min(job["scheduled_end"], job["deadline"]):
                self.store.update(job["id"], now=now, state="MISSED_WINDOW", finished_at=now,
                    error="Container was created but the execution window elapsed before start.", message="Created container left stopped; no workload executed.")
                return
            self.runner.start(container.id)
            # Record actual Docker timestamps on the next inspection, including
            # the case where a short task has already finished by then.
            self.store.update(job["id"], now=now, state="RUNNING", error=None, message="Docker start accepted.")
            return
        if container.status != "running" or container.started_at is None:
            self.store.update(job["id"], now=now, state="RECOVERY_REQUIRED", error=f"Unexpected Docker state {container.status}; queue held.", message="Manual inspection required; workload was not restarted.")
            return

        changes = {"state": "RUNNING", "started_at": container.started_at, "error": None}
        if spec["workload"] == "image-batch":
            try:
                changes["logs"] = self.runner.logs(container.id)
            except RunnerError:
                pass  # Continue timeout supervision even when log collection fails.
        cutoff = min(container.started_at + spec["timeout_seconds"], job["scheduled_end"], job["deadline"])
        if now >= cutoff or job["stop_reason"]:
            reason = job["stop_reason"] or "Execution timeout or reserved-window limit reached."
            changes["stop_reason"] = reason
            self.store.update(job["id"], now=now, message="Stopping workload at its execution limit." if not job["stop_reason"] else None, **changes)
            self.runner.stop(container.id)
        else:
            self.store.update(job["id"], now=now, message="Reconciled running container." if job["state"] != "RUNNING" else None, **changes)

    def finish(self, job, container, now):
        end = container.finished_at or now
        runtime = max(0.0, end - container.started_at) if container.started_at else None
        if job["stop_reason"]:
            state, error = "TIMED_OUT", job["stop_reason"]
        elif end > job["deadline"]:
            state, error = "MISSED_DEADLINE", "Container finished after the user deadline."
        elif end > job["scheduled_end"] or (runtime is not None and runtime > job["snapshot"]["execution"]["timeout_seconds"]):
            state, error = "TIMED_OUT", "Execution exceeded its reserved window or timeout before the worker observed completion."
        elif container.status == "dead" or container.exit_code != 0 or container.oom_killed:
            state, error = "FAILED", f"Container exited with code {container.exit_code}; OOM killed={container.oom_killed}."
        elif container.started_at is None:
            state, error = "FAILED", "Container exited without an observable start time."
        else:
            state, error = "SUCCEEDED", None
        try:
            logs = self.runner.logs(container.id)
        except RunnerError as exc:
            logs = f"Logs unavailable: {exc}"
        power = job["snapshot"]["optimization"]["job"]["estimated_power_w"]
        evidence = {
            "execution_backend": "docker", "container_id": container.id, "runtime_seconds": runtime,
            "estimated_total_energy_kwh": power / 1000 * runtime / 3600 if runtime is not None else None,
            "energy_method": "configured_power_x_container_runtime; not a power measurement",
            "measured_carbon_savings": None,
            "note": "The recommendation is a frozen prediction. It is not relabeled as realized savings after execution.",
        }
        if state == "SUCCEEDED" and job["snapshot"]["execution"]["workload"] == "image-batch":
            try:
                from .assets import inspect_result
                evidence["artifact"] = inspect_result(job)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile) as exc:
                state, error = "FAILED", f"Container exited successfully but result validation failed: {exc}"
        self.store.update(job["id"], now=now, state=state, started_at=container.started_at,
            finished_at=end, exit_code=container.exit_code, logs=logs, error=error, evidence=evidence,
            message=f"Observed terminal Docker state; recorded {state}.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="CarbonShift single Docker worker")
    parser.add_argument("--db", help="Same local SQLite path used by API/CLI")
    parser.add_argument("--once", action="store_true", help="Reconcile once and exit; does not wait for a job to complete")
    parser.add_argument("--poll-seconds", type=float, default=1)
    args = parser.parse_args(argv)
    if not 0.2 <= args.poll_seconds <= 5:
        parser.error("poll-seconds must be between 0.2 and 5")
    store = Store(args.db)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    try:
        with worker_lock(store.path):
            runner = DockerRunner()
            worker = Worker(store, runner)
            print(f"Worker supervising queue at {store.path}", flush=True)
            last_check, readiness, previous = 0, {}, None
            try:
                while not stop.is_set():
                    if time.monotonic() - last_check >= 5 or not readiness:
                        try:
                            readiness = {"state": "ready", **runner.check()}
                        except RunnerError as exc:
                            readiness = {"state": "unavailable", "error": str(exc)}
                        last_check = time.monotonic()
                    store.heartbeat(readiness["state"], readiness)
                    # An unavailable image must not prevent reconciliation of a
                    # container already running under its immutable image ID.
                    job_id = worker.tick()
                    job = store.get(job_id) if job_id else None
                    report = (job_id, job["state"], job["error"]) if job else (None, readiness["state"], readiness.get("error"))
                    if report != previous:
                        print(json.dumps({"job_id": report[0], "state": report[1], "error": report[2]}), flush=True)
                        previous = report
                    if args.once:
                        break
                    stop.wait(args.poll_seconds)
            finally:
                store.heartbeat("stopped")
    except (RuntimeError, OSError) as exc:
        print(f"Worker stopped: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
