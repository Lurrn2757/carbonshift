import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.error import URLError

from carbonshift.__main__ import main
from carbonshift.models import OptimizeRequest
from carbonshift.optimizer import optimize


class CLITests(unittest.TestCase):
    def test_exported_demo_request_can_be_optimized(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["demo", "--request"]), 0)
        request = OptimizeRequest.model_validate_json(output.getvalue())
        self.assertAlmostEqual(optimize(request)["estimated_grid_energy_reduction_kwh"], 0.19)

    def test_demo_outputs_valid_json_and_simulation_label(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["demo"]), 0)
        self.assertEqual(json.loads(output.getvalue())["forecast_source"], "simulated")

    def test_live_network_failure_returns_nonzero_without_fake_data(self):
        output, error = io.StringIO(), io.StringIO()
        with patch("carbonshift.__main__.fetch_open_meteo", side_effect=URLError("offline")), redirect_stdout(output), redirect_stderr(error):
            code = main(["live", "--pv-capacity-kwp", "0.5", "--site-base-load-kw", "0.08", "--estimated-power-w", "180"])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Weather unavailable", error.getvalue())
