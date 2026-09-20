import unittest
from datetime import timedelta

from pydantic import ValidationError

from carbonshift.carbon import SimulatedCarbonProvider
from carbonshift.demo import demo_request
from carbonshift.models import CarbonForecast, CarbonInterval, OptimizeRequest
from carbonshift.optimizer import optimize


class CarbonTests(unittest.TestCase):
    def request(self, objective="hybrid"):
        data = demo_request().model_dump()
        job = data["job"]
        data["objective"] = objective
        data["carbon_forecast"] = SimulatedCarbonProvider().forecast(job["earliest_start"], job["deadline"], "IN-WE").model_dump()
        return data

    def test_simulated_provenance_is_retained(self):
        result = optimize(OptimizeRequest.model_validate(self.request()))
        self.assertTrue(result["carbon_is_simulated"])
        self.assertEqual(result["carbon_source"], "simulated-carbon-v1")
        self.assertGreaterEqual(result["estimated_grid_emissions_reduction_gco2e"], 0)

    def test_carbon_mode_uses_grid_only(self):
        result = optimize(OptimizeRequest.model_validate(self.request("carbon")))
        self.assertEqual(result["recommended"]["estimated_solar_energy_kwh"], 0)
        self.assertAlmostEqual(result["recommended"]["estimated_grid_energy_kwh"], 0.27)

    def test_hybrid_can_use_more_grid_energy_but_emit_less(self):
        data = self.request()
        start = data["job"]["earliest_start"]
        data["job"].update(duration_minutes=60, estimated_power_w=1000)
        data["site"]["base_load_kw"] = 0
        for index, point in enumerate(data["forecast"]["intervals"]):
            point["estimated_solar_kw"] = 0.8 if index == 0 else 0
        data["carbon_forecast"]["intervals"] = [CarbonInterval(start=start+timedelta(hours=i), end=start+timedelta(hours=i+1), intensity_gco2e_per_kwh=[600,100,1000,1000,1000,1000,1000,1000,1000][i]).model_dump() for i in range(9)]
        result = optimize(OptimizeRequest.model_validate(data))
        self.assertEqual(result["recommended"]["start"], (start+timedelta(hours=1)).isoformat())
        self.assertAlmostEqual(result["estimated_grid_energy_reduction_kwh"], -0.8)
        self.assertAlmostEqual(result["estimated_grid_emissions_reduction_gco2e"], 20)

    def test_misaligned_carbon_and_solar_intervals_integrate_correctly(self):
        data = self.request("carbon")
        start = data["job"]["earliest_start"]
        data["job"].update(duration_minutes=60, estimated_power_w=1000)
        data["carbon_forecast"]["intervals"] = [CarbonInterval(start=start+timedelta(minutes=30*i), end=start+timedelta(minutes=30*(i+1)), intensity_gco2e_per_kwh=100 if i==0 else 200).model_dump() for i in range(18)]
        result = optimize(OptimizeRequest.model_validate(data))
        self.assertAlmostEqual(result["baseline"]["estimated_grid_emissions_gco2e"], 150)

    def test_wrong_region_and_missing_carbon_are_rejected(self):
        data = self.request()
        data["carbon_forecast"]["grid_zone"] = "GB"
        with self.assertRaises(ValidationError):
            OptimizeRequest.model_validate(data)
        data["carbon_forecast"] = None
        with self.assertRaises(ValidationError):
            OptimizeRequest.model_validate(data)

    def test_carbon_gaps_and_incomplete_coverage_rejected(self):
        data = self.request()
        data["carbon_forecast"]["intervals"].pop(2)
        with self.assertRaises(ValidationError):
            OptimizeRequest.model_validate(data)
        data = self.request()
        data["carbon_forecast"]["intervals"].pop()
        with self.assertRaisesRegex(ValueError, "cover"):
            optimize(OptimizeRequest.model_validate(data))
