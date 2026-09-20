import copy
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from carbonshift.demo import queue_demo_request
from carbonshift.docker_runner import Container, RunnerError
from carbonshift.models import SubmitRequest
from carbonshift.store import Store, Conflict
from carbonshift.worker import Worker, worker_lock


class FakeDocker:
    """A stateful test double. These tests do NOT claim real Docker execution."""
    def __init__(self, clock):
        self.clock = clock
        self.container = None
        self.creates = self.starts = self.stops = 0
        self.offline = False
        self.uncertain_create = self.uncertain_start = False

    def inspect(self, name):
        if self.offline:
            raise RunnerError("daemon offline")
        return self.container

    def create(self, job, queue_id):
        self.creates += 1
        self.container = Container("a" * 64, "created", {"carbonshift.queue": queue_id, "carbonshift.job": job["id"]})
        if self.uncertain_create:
            self.uncertain_create = False
            raise RunnerError("create timed out after daemon created container")
        return self.container.id

    def start(self, container_id):
        self.starts += 1
        self.container.status = "running"
        self.container.started_at = self.clock()
        if self.uncertain_start:
            self.uncertain_start = False
            raise RunnerError("start timed out after daemon started container")

    def stop(self, container_id):
        self.stops += 1
        self.finish(137)

    def finish(self, exit_code=0):
        self.container.status = "exited"
        self.container.finished_at = self.clock()
        self.container.exit_code = exit_code

    def logs(self, container_id):
        return '{"event":"completed","test_double":true}'


class QueueWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "queue.sqlite3")
        self.request = queue_demo_request(60, 5, "test-job")
        self.job = self.store.submit(self.request)
        self.now = self.job["scheduled_start"]
        self.runner = FakeDocker(lambda: self.now)
        self.worker = Worker(self.store, self.runner, lambda: self.now)

    def test_state_and_input_survive_database_reopen(self):
        reopened = Store(self.store.path)
        self.assertEqual(reopened.queue_id, self.store.queue_id)
        self.assertEqual(reopened.get(self.job["id"])["snapshot"], self.job["snapshot"])

    def test_idempotent_retry_returns_same_job_even_after_scheduled_time(self):
        retry = self.store.submit(self.request, now=self.now + 100)
        self.assertEqual(retry["id"], self.job["id"])
        self.assertEqual(len(self.store.list()), 1)

    def test_same_key_different_request_is_rejected(self):
        data = self.request.model_dump()
        data["optimization"]["job"]["name"] = "different-job"
        with self.assertRaises(Conflict):
            self.store.submit(SubmitRequest.model_validate(data))

    def test_overlapping_reservation_rejected(self):
        data = self.request.model_dump()
        data["idempotency_key"] = "another"
        with self.assertRaisesRegex(Conflict, "overlaps"):
            self.store.submit(SubmitRequest.model_validate(data))

    def test_concurrent_submissions_do_not_double_book(self):
        self.store.cancel(self.job["id"])
        def submit(index):
            data = self.request.model_dump()
            data["idempotency_key"] = f"parallel-{index}"
            try:
                return self.store.submit(SubmitRequest.model_validate(data))["id"]
            except Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(submit, [1, 2]))
        self.assertEqual(sum(x is not None for x in result), 1)

    def test_does_not_start_early(self):
        self.now -= 1
        self.assertIsNone(self.worker.tick())
        self.assertEqual(self.runner.creates, 0)

    def test_full_worker_lifecycle_records_evidence(self):
        self.worker.tick()
        self.assertEqual(self.store.get(self.job["id"])["state"], "RUNNING")
        self.now += 5
        self.runner.finish()
        self.worker.tick()
        result = self.store.get(self.job["id"])
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["evidence"]["runtime_seconds"], 5)
        self.assertIsNone(result["evidence"]["measured_carbon_savings"])
        self.assertAlmostEqual(result["evidence"]["estimated_total_energy_kwh"], 30 / 1000 * 5 / 3600)
        self.assertIsNone(self.worker.tick())
        self.assertEqual(self.runner.starts, 1)

    def test_restarts_reconcile_running_container(self):
        self.worker.tick()
        restarted = Worker(Store(self.store.path), self.runner, lambda: self.now)
        restarted.tick()
        self.assertEqual(self.runner.creates, 1)
        self.assertEqual(self.runner.starts, 1)

    def test_ambiguous_create_does_not_duplicate_container(self):
        self.runner.uncertain_create = True
        self.worker.tick()
        self.assertEqual(self.store.get(self.job["id"])["state"], "STARTING")
        self.worker.tick()
        self.assertEqual(self.runner.creates, 1)
        self.assertEqual(self.runner.starts, 1)

    def test_ambiguous_start_does_not_restart_workload(self):
        self.runner.uncertain_start = True
        self.worker.tick()
        self.worker.tick()
        self.assertEqual(self.runner.starts, 1)
        self.assertEqual(self.store.get(self.job["id"])["state"], "RUNNING")

    def test_daemon_outage_holds_slot_and_never_means_missing(self):
        self.runner.offline = True
        self.worker.tick()
        self.assertEqual(self.runner.creates, 0)
        self.assertEqual(self.store.get(self.job["id"])["state"], "STARTING")
        with self.assertRaises(Conflict):
            self.store.submit(queue_demo_request(3600, 5))

    def test_missing_tracked_container_is_lost_and_not_retried(self):
        self.worker.tick()
        self.runner.container = None
        self.worker.tick()
        self.assertEqual(self.store.get(self.job["id"])["state"], "LOST")
        self.assertEqual(self.runner.creates, 1)

    def test_foreign_container_holds_queue_without_actions(self):
        self.runner.container = Container("b" * 64, "running", {})
        self.worker.tick()
        self.assertEqual(self.store.get(self.job["id"])["state"], "RECOVERY_REQUIRED")
        self.assertEqual(self.runner.starts + self.runner.stops + self.runner.creates, 0)

    def test_timeout_stops_then_observes_exit(self):
        self.worker.tick()
        self.now += 16
        self.worker.tick()
        self.assertEqual(self.runner.stops, 1)
        self.worker.tick()
        self.assertEqual(self.store.get(self.job["id"])["state"], "TIMED_OUT")

    def test_unobserved_timeout_is_not_success(self):
        self.worker.tick()
        self.now += 20
        self.runner.finish()
        self.worker.tick()
        self.assertEqual(self.store.get(self.job["id"])["state"], "TIMED_OUT")

    def test_nonzero_exit_is_failed(self):
        self.worker.tick()
        self.now += 3
        self.runner.finish(2)
        self.worker.tick()
        self.assertEqual(self.store.get(self.job["id"])["state"], "FAILED")

    def test_expired_window_never_starts(self):
        self.now = self.job["scheduled_end"] + 1
        self.worker.tick()
        self.assertEqual(self.store.get(self.job["id"])["state"], "MISSED_WINDOW")
        self.assertEqual(self.runner.creates, 0)

    def test_cancelled_job_never_runs(self):
        self.store.cancel(self.job["id"])
        self.assertIsNone(self.worker.tick())
        self.assertEqual(self.runner.creates, 0)

    def test_cannot_cancel_claimed_work(self):
        self.worker.tick()
        with self.assertRaises(Conflict):
            self.store.cancel(self.job["id"])

    def test_second_worker_lock_is_rejected(self):
        with worker_lock(self.store.path):
            with self.assertRaisesRegex(RuntimeError, "Another worker"):
                with worker_lock(self.store.path):
                    self.fail("Second worker acquired lock")
