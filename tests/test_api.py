import unittest
import tempfile
from pathlib import Path
from importlib.util import find_spec
from unittest.mock import patch
from urllib.error import URLError

HTTP_DEPS_AVAILABLE = find_spec("fastapi") is not None and find_spec("httpx") is not None
if HTTP_DEPS_AVAILABLE:
    from fastapi.testclient import TestClient
    from carbonshift.api import app, get_store
    from carbonshift.store import Store


@unittest.skipUnless(HTTP_DEPS_AVAILABLE, "Install requirements-dev.txt to run HTTP tests")
class APITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = Store(Path(temporary.name) / "http-tests.sqlite3")
        app.dependency_overrides[get_store] = lambda: store
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, base_url="http://127.0.0.1")
        self.addCleanup(self.client.close)

    def test_health_and_schema(self):
        self.assertEqual(self.client.get("/health").json()["version"], "0.4.0")
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)

    def test_demo_can_be_posted_to_optimizer(self):
        request = self.client.get("/api/demo/request").json()
        response = self.client.post("/api/optimize", json=request)
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(response.json()["estimated_grid_energy_reduction_kwh"], 0.19)

    def test_bad_request_is_422(self):
        self.assertEqual(self.client.post("/api/optimize", json={}).status_code, 422)

    def test_provider_outage_does_not_become_fake_live_data(self):
        with patch("carbonshift.api.fetch_open_meteo", side_effect=URLError("offline")):
            self.assertEqual(self.client.get("/api/forecast").status_code, 503)

    def test_invalid_provider_data_is_502(self):
        with patch("carbonshift.api.fetch_open_meteo", side_effect=ValueError("bad units")):
            self.assertEqual(self.client.get("/api/forecast").status_code, 502)

    def test_queue_submission_idempotency_and_readback(self):
        payload = self.client.get("/api/jobs/demo/request?delay_seconds=60").json()
        first = self.client.post("/api/jobs", json=payload)
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/jobs", json=payload)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(self.client.get("/api/jobs").json()[0]["state"], "SCHEDULED")
        detail = self.client.get("/api/jobs/" + first.json()["id"])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["snapshot"], payload)

    def test_overlap_and_cancellation(self):
        payload = self.client.get("/api/jobs/demo/request?delay_seconds=60").json()
        first = self.client.post("/api/jobs", json=payload).json()
        payload["idempotency_key"] = "overlap-key"
        self.assertEqual(self.client.post("/api/jobs", json=payload).status_code, 409)
        cancelled = self.client.post("/api/jobs/" + first["id"] + "/cancel")
        self.assertEqual(cancelled.json()["state"], "CANCELLED")
        self.assertEqual(self.client.post("/api/jobs", json=payload).status_code, 200)

    def test_hybrid_demo_has_simulated_carbon(self):
        response = self.client.get("/api/demo?objective=hybrid")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["carbon_is_simulated"])

    def test_missing_job_is_404(self):
        self.assertEqual(self.client.get("/api/jobs/not-a-job").status_code, 404)


if __name__ == "__main__":
    unittest.main()
