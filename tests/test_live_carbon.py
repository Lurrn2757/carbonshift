import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from fastapi.testclient import TestClient
from carbonshift.api import app, get_store
from carbonshift.carbon import fetch_electricitymaps_live, parse_live_reading, CarbonProviderError, NoRedirect
from carbonshift.evidence import append_observation, read_observations, reading_view
from carbonshift.__main__ import main


NOW = datetime(2026,9,18,10,tzinfo=timezone.utc)


def payload(**changes):
    data = {'zone':'IN-WE','carbonIntensity':540.5,'datetime':NOW.isoformat(),
            'isEstimated':True,'estimationMethod':'TEST_FIXTURE',
            'emissionFactorType':'lifecycle','flowTraced':True,'temporalGranularity':'hourly'}
    data.update(changes)
    return json.dumps(data).encode()


class LiveCarbonTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.log=self.root/'observations.jsonl'
        self.queue=self.root/'queue.sqlite3'
        self.env=patch.dict(os.environ,{'CARBONSHIFT_OBSERVATIONS':str(self.log),'CARBONSHIFT_DB':str(self.queue),'ELECTRICITYMAPS_API_KEY':'local-test-secret'})
        self.env.start();self.addCleanup(self.env.stop)
        self.reading=parse_live_reading(payload(), 'IN-WE', NOW)

    def test_request_authentication_and_signal_parameters(self):
        opener=Mock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=payload(datetime=datetime.now(timezone.utc).isoformat()))))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch('carbonshift.carbon.build_opener',return_value=opener):
            reading=fetch_electricitymaps_live('IN-WE')
        request=opener.open.call_args.args[0]
        self.assertEqual(request.get_header('Auth-token'),'local-test-secret')
        self.assertNotIn('local-test-secret',request.full_url)
        self.assertTrue(request.full_url.startswith('https://api.electricitymaps.com/v4/carbon-intensity/latest?'))
        self.assertIn('disableCallerLookup=true',request.full_url)
        self.assertIn('emissionFactorType=lifecycle',request.full_url)
        self.assertEqual(reading.zone,'IN-WE')
        self.assertTrue(reading.is_estimated)

    def test_missing_key_and_bad_zone_make_no_network_request(self):
        with patch('carbonshift.carbon.build_opener') as opener:
            with self.assertRaises(CarbonProviderError):fetch_electricitymaps_live('IN-WE', '')
            with self.assertRaises(CarbonProviderError):fetch_electricitymaps_live('IN-WE&zone=DE', 'secret')
            opener.assert_not_called()

    def test_http_errors_are_actionable_and_do_not_echo_secrets(self):
        for code,text in [(401,'key rejected'),(403,'entitlement'),(404,'No latest'),(429,'rate limit'),(500,'HTTP 500')]:
            with self.subTest(code=code),patch('carbonshift.carbon.build_opener') as factory:
                factory.return_value.open.side_effect=HTTPError('https://example.invalid',code,'local-test-secret',{},None)
                with self.assertRaises(CarbonProviderError) as error:fetch_electricitymaps_live()
                self.assertIn(text,str(error.exception))
                self.assertNotIn('local-test-secret',str(error.exception))

    def test_network_failure_never_falls_back_to_simulation(self):
        with patch('carbonshift.carbon.build_opener') as factory:
            factory.return_value.open.side_effect=URLError('local-test-secret')
            with self.assertRaisesRegex(CarbonProviderError,'unreachable'):fetch_electricitymaps_live()
        self.assertFalse(self.log.exists())

    def test_redirect_is_rejected_without_forwarding_key(self):
        with self.assertRaises(CarbonProviderError):NoRedirect().redirect_request(None,None,302,'',{},'https://other.invalid')

    def test_wrong_zone_bad_intensity_missing_and_future_times_are_rejected(self):
        cases=[{'zone':'DE'},{'carbonIntensity':-1},{'carbonIntensity':float('nan')},
               {'carbonIntensity':True},{'carbonIntensity':'540'}, {'datetime':'2026-09-18T10:00:00'},
               {'datetime':(NOW+timedelta(hours=1)).isoformat()},{'isEstimated':'false'},
               {'flowTraced':False},{'emissionFactorType':'direct'},{'temporalGranularity':'15_minutes'}]
        for change in cases:
            with self.subTest(change=change),self.assertRaises(CarbonProviderError):parse_live_reading(payload(**change),'IN-WE',NOW)
        for body in [b'{}',b'[]',b'not json']:
            with self.assertRaises(CarbonProviderError):parse_live_reading(body,'IN-WE',NOW)

    def test_stale_reading_is_retained_but_not_a_forecast(self):
        stale=parse_live_reading(payload(datetime=(NOW-timedelta(hours=4)).isoformat()),'IN-WE',NOW)
        self.assertTrue(reading_view(stale,NOW)['stale'])
        self.assertFalse(stale.used_for_scheduling)
        self.assertFalse(stale.is_simulated)
        self.assertEqual(stale.data_kind,'latest_observation')

    def test_log_survives_reopen_without_creating_queue_database(self):
        append_observation(self.reading)
        result=read_observations()
        self.assertEqual(result['total_valid_records'],1)
        self.assertEqual(result['records'][0]['zone'],'IN-WE')
        self.assertFalse(self.queue.exists())
        self.assertNotIn('local-test-secret',self.log.read_text())

    def test_concurrent_appends_keep_complete_json_lines(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _:append_observation(self.reading),range(24)))
        result=read_observations(limit=10)
        self.assertEqual(result['total_valid_records'],24)
        self.assertEqual(len(result['records']),10)
        self.assertEqual(result['warnings'],[])

    def test_corrupt_tail_is_preserved_and_blocks_append(self):
        append_observation(self.reading)
        with self.log.open('ab') as f:f.write(b'{partial')
        before=self.log.read_bytes()
        with self.assertRaisesRegex(ValueError,'incomplete'):append_observation(self.reading)
        self.assertEqual(self.log.read_bytes(),before)
        self.assertEqual(read_observations()['total_valid_records'],1)
        self.assertTrue(read_observations()['warnings'])

    def test_queue_path_and_sqlite_disguised_as_jsonl_are_rejected(self):
        with self.assertRaises(ValueError):append_observation(self.reading,self.queue)
        other=self.root/'renamed-db.jsonl'
        with sqlite3.connect(other) as db:db.execute('CREATE TABLE untouched(id)')
        before=other.read_bytes()
        with self.assertRaisesRegex(ValueError,'SQLite'):append_observation(self.reading,other)
        self.assertEqual(other.read_bytes(),before)

    def test_cli_fetch_and_log_without_queue_changes(self):
        with patch('carbonshift.carbon.fetch_electricitymaps_live',return_value=self.reading),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['carbon-now','--zone','IN-WE']),0)
        self.assertFalse(json.loads(out.getvalue())['used_for_scheduling'])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(['carbon-log']),0)
        self.assertEqual(json.loads(out.getvalue())['total_valid_records'],1)
        self.assertFalse(self.queue.exists())

    def test_http_observation_routes_never_instantiate_queue_store(self):
        app.dependency_overrides[get_store]=lambda: (_ for _ in ()).throw(AssertionError('queue accessed'))
        self.addCleanup(app.dependency_overrides.clear)
        with TestClient(app,base_url='http://127.0.0.1') as client,patch('carbonshift.api.fetch_electricitymaps_live',return_value=self.reading):
            response=client.post('/api/carbon/observe?zone=IN-WE')
            self.assertEqual(response.status_code,200)
            history=client.get('/api/carbon/observations').json()
            self.assertTrue(history['configured'])
            self.assertEqual(history['total_valid_records'],1)
            self.assertNotIn('local-test-secret',json.dumps(history))
        self.assertFalse(self.queue.exists())

    def test_api_outage_preserves_previous_observations(self):
        append_observation(self.reading)
        before=self.log.read_bytes()
        with TestClient(app,base_url='http://127.0.0.1') as client,patch('carbonshift.api.fetch_electricitymaps_live',side_effect=CarbonProviderError('rate limit')):
            self.assertEqual(client.post('/api/carbon/observe').status_code,503)
        self.assertEqual(self.log.read_bytes(),before)

    def test_api_reports_journal_write_failure_without_claiming_saved(self):
        with TestClient(app,base_url='http://127.0.0.1') as client,patch('carbonshift.api.fetch_electricitymaps_live',return_value=self.reading),patch('carbonshift.api.append_observation',side_effect=OSError('disk full')):
            response=client.post('/api/carbon/observe')
        self.assertEqual(response.status_code,503)
        self.assertIn('NOT saved',response.json()['detail'])
        self.assertFalse(self.log.exists())

    def test_existing_database_bytes_are_unchanged_by_observation(self):
        with sqlite3.connect(self.queue) as db:
            db.execute('CREATE TABLE sentinel(value TEXT)')
            db.execute("INSERT INTO sentinel VALUES ('leave intact')")
        before=self.queue.read_bytes()
        append_observation(self.reading)
        read_observations()
        self.assertEqual(self.queue.read_bytes(),before)
