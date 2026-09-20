import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from io import BytesIO
import json

from carbonshift.forecast import fetch_open_meteo, parse_open_meteo


class ForecastTests(unittest.TestCase):
    def payload(self):
        return {
            "utc_offset_seconds": 0,
            "hourly_units": {"shortwave_radiation": "W/m²"},
            "hourly": {"time": ["2026-09-12T10:00", "2026-09-12T11:00"], "shortwave_radiation": [500, 1000]},
        }

    def test_radiation_timestamp_is_interval_end(self):
        result = parse_open_meteo(self.payload(), 1, 0.8)
        self.assertEqual(result.intervals[0].start, datetime(2026, 9, 12, 9, tzinfo=timezone.utc))
        self.assertEqual(result.intervals[0].end, datetime(2026, 9, 12, 10, tzinfo=timezone.utc))
        self.assertAlmostEqual(result.intervals[0].estimated_solar_kw, 0.4)

    def test_missing_radiation_rejected(self):
        for value in [None, -1, float("nan"), "500", True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                payload = self.payload()
                payload["hourly"]["shortwave_radiation"][0] = value
                parse_open_meteo(payload, 1, 0.8)

    def test_array_length_mismatch_rejected(self):
        payload = self.payload()
        payload["hourly"]["time"].pop()
        with self.assertRaises(ValueError):
            parse_open_meteo(payload, 1, 0.8)

    def test_malformed_response_structure_rejected(self):
        payloads = [None, [], {}, {"utc_offset_seconds": 0, "hourly_units": None}]
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_open_meteo(payload, 1, 0.8)

    def test_unknown_units_and_timezone_rejected(self):
        payload = self.payload()
        payload["hourly_units"]["shortwave_radiation"] = "kW/m²"
        with self.assertRaises(ValueError):
            parse_open_meteo(payload, 1, 0.8)
        payload = self.payload()
        payload["utc_offset_seconds"] = 19800
        with self.assertRaises(ValueError):
            parse_open_meteo(payload, 1, 0.8)

    def test_zero_capacity_means_no_solar(self):
        result = parse_open_meteo(self.payload(), 0, 0.8)
        self.assertTrue(all(x.estimated_solar_kw == 0 for x in result.intervals))

    def test_adapter_requests_utc_hourly_radiation(self):
        with patch("carbonshift.forecast.urlopen", return_value=BytesIO(json.dumps(self.payload()).encode())) as get:
            result = fetch_open_meteo()
        url = get.call_args.args[0].full_url
        self.assertIn("hourly=shortwave_radiation", url)
        self.assertIn("timezone=UTC", url)
        self.assertEqual(result.source, "open-meteo")


if __name__ == "__main__":
    unittest.main()
