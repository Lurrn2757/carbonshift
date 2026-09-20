import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

from fastapi.testclient import TestClient
from carbonshift.api import app, get_store
from carbonshift.store import Store
from carbonshift.worker import main as worker_main
from carbonshift.docker_runner import DockerRunner, RunnerError
from carbonshift.models import SubmitRequest
from carbonshift.worker import Worker
from test_queue_worker import FakeDocker


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name) / 'queue.sqlite3')
        app.dependency_overrides[get_store] = lambda: self.store
        self.addCleanup(app.dependency_overrides.clear)
        self.client = TestClient(app, base_url='http://127.0.0.1')
        self.addCleanup(self.client.close)

    def test_dashboard_served_with_local_assets(self):
        page = self.client.get('/')
        self.assertIn('text/html', page.headers['content-type'])
        self.assertIn('Schedule planner', page.text)
        self.assertEqual(self.client.get('/static/app.js').status_code, 200)
        self.assertEqual(self.client.get('/static/style.css').status_code, 200)

    def test_editable_plan_preserves_energy_and_provenance(self):
        result = self.client.post('/api/planner', json={'estimated_power_w': 250, 'duration_minutes': 120, 'objective': 'hybrid'}).json()
        self.assertTrue(result['result']['carbon_is_simulated'])
        self.assertEqual(result['request']['forecast']['source'], 'simulated')
        self.assertAlmostEqual(result['result']['recommended']['estimated_total_energy_kwh'], .5)
        self.assertEqual(self.store.list(), [])

    def test_no_solar_plan_does_not_fabricate_savings(self):
        result = self.client.post('/api/planner', json={'scenario':'no-solar'}).json()['result']
        self.assertEqual(result['baseline'], result['recommended'])
        self.assertEqual(result['estimated_grid_energy_reduction_kwh'], 0)

    def test_execution_preview_queues_the_exact_recommendation(self):
        preview = self.client.post('/api/execution/preview', json={'mode':'solar-shift', 'delay_seconds':60}).json()
        result = preview['result']
        self.assertAlmostEqual(result['estimated_grid_energy_reduction_percent'], 100)
        self.assertEqual(self.store.list(), [])
        response = self.client.post('/api/jobs', json=preview['submission'])
        self.assertEqual(response.status_code, 200)
        job = response.json()
        self.assertEqual(job['recommendation'], result)
        self.assertEqual(job['snapshot'], preview['submission'])
        now = job['scheduled_start'] - .1
        runner = FakeDocker(lambda: now)
        worker = Worker(self.store, runner, lambda: now)
        worker.tick()
        self.assertEqual(runner.starts, 0)
        now = job['scheduled_start']
        worker.tick()
        self.assertEqual(runner.starts, 1)
        now += 5
        runner.finish()
        worker.tick()
        finished = self.store.get(job['id'])
        self.assertEqual(finished['state'], 'SUCCEEDED')
        self.assertEqual(finished['recommendation'], result)
        self.assertIsNone(finished['evidence']['measured_carbon_savings'])
        self.assertNotEqual(finished['evidence']['estimated_total_energy_kwh'], result['recommended']['estimated_total_energy_kwh'])

    def test_execution_preview_budget_and_estimated_power(self):
        preview = self.client.post('/api/execution/preview', json={'mode':'solar-shift', 'run_seconds':3600, 'estimated_power_w':200}).json()
        request = SubmitRequest.model_validate(preview['submission'])
        self.assertEqual(request.optimization.job.duration_minutes,61)
        self.assertEqual(request.execution.timeout_seconds,3610)
        self.assertEqual(request.optimization.job.estimated_power_w,200)
        self.assertGreater(preview['result']['estimated_grid_energy_reduction_kwh'],0)

    def test_immediate_execution_preview_has_no_shift_or_savings(self):
        preview = self.client.post('/api/execution/preview', json={}).json()['result']
        self.assertEqual(preview['recommended'], preview['baseline'])

    def test_invalid_plan_is_rejected(self):
        self.assertEqual(self.client.post('/api/planner', json={'duration_minutes':541}).status_code, 422)

    def test_foreign_browser_origins_and_hosts_are_rejected(self):
        self.assertEqual(self.client.post('/api/planner', json={}, headers={'Origin':'https://unrelated.example'}).status_code, 403)
        self.assertEqual(self.client.get('/health', headers={'Host':'unrelated.example'}).status_code, 400)
        self.assertEqual(self.client.post('/api/planner', json={}, headers={'Origin':'http://127.0.0.1'}).status_code, 200)

    def test_unknown_stale_unavailable_and_stopped_workers_are_not_ready(self):
        self.assertFalse(self.client.get('/api/system').json()['execution_ready'])
        self.store.heartbeat('ready', now=100)
        self.assertTrue(self.store.status(now=110)['execution_ready'])
        self.assertFalse(self.store.status(now=150)['execution_ready'])
        self.store.heartbeat('unavailable', {'error':'daemon offline'}, now=150)
        self.assertFalse(self.store.status(now=151)['execution_ready'])
        self.assertTrue(self.store.status(now=151)['worker']['online'])
        self.store.heartbeat('stopped', now=151)
        self.assertFalse(self.store.status(now=152)['worker']['online'])

    def test_queue_summary_and_names_survive_reopening(self):
        payload = self.client.get('/api/jobs/demo/request').json()
        job = self.client.post('/api/jobs', json=payload).json()
        self.assertEqual(Store(self.store.path).list()[0]['name'], 'short-docker-demo')
        self.assertEqual(self.client.get('/api/system').json()['counts'], {'SCHEDULED':1})
        self.client.post('/api/jobs/'+job['id']+'/cancel')
        self.assertEqual(self.client.get('/api/system').json()['counts'], {'CANCELLED':1})

    def test_worker_recovers_readiness_after_daemon_outage(self):
        seen = []
        def tick():
            seen.append(self.store.status()['worker']['state'])
        stop = Mock()
        stop.is_set.side_effect = [False,False,True]
        runner = Mock()
        runner.check.side_effect = [RunnerError('offline'), {'docker_server':'test', 'image_id':'fake'}]
        with patch('carbonshift.worker.DockerRunner', return_value=runner), \
             patch('carbonshift.worker.threading.Event', return_value=stop), \
             patch('carbonshift.worker.signal.signal'), \
             patch('carbonshift.worker.time.monotonic', side_effect=[10,10,20,20]), \
             patch('carbonshift.worker.Worker.tick', side_effect=tick):
            self.assertEqual(worker_main(['--db', str(self.store.path)]), 0)
        self.assertEqual(seen, ['unavailable','ready'])
        self.assertEqual(self.store.status()['worker']['state'], 'stopped')

    def test_single_log_file_explicitly_disables_compression(self):
        payload = self.client.get('/api/jobs/demo/request').json()
        job = self.client.post('/api/jobs', json=payload).json()
        with patch.object(DockerRunner, 'command', return_value='test') as command:
            DockerRunner().create(job, self.store.queue_id)
        args = command.call_args.args[0]
        options = [args[i+1] for i,v in enumerate(args) if v == '--log-opt']
        self.assertIn('max-file=1', options)
        self.assertIn('compress=false', options)
