import unittest
from datetime import timedelta

from pydantic import ValidationError

from carbonshift.demo import demo_request
from carbonshift.models import OptimizeRequest
from carbonshift.optimizer import optimize


class OptimizerTests(unittest.TestCase):
    def make(self, edit):
        data = demo_request().model_dump(mode="json")
        edit(data)
        return OptimizeRequest.model_validate(data)

    def test_known_90_minute_scenario(self):
        req = demo_request()
        result = optimize(req)
        self.assertEqual(result["recommended"]["start"], (req.job.earliest_start + timedelta(hours=3)).isoformat())
        self.assertAlmostEqual(result["baseline"]["estimated_grid_energy_kwh"], 0.25)
        self.assertAlmostEqual(result["recommended"]["estimated_grid_energy_kwh"], 0.06)
        self.assertAlmostEqual(result["estimated_grid_energy_reduction_percent"], 76)

    def test_no_solar_means_no_savings_and_no_delay(self):
        req = self.make(lambda d: [x.update(estimated_solar_kw=0) for x in d["forecast"]["intervals"]])
        result = optimize(req)
        self.assertEqual(result["baseline"], result["recommended"])
        self.assertEqual(result["estimated_grid_energy_reduction_kwh"], 0)
        self.assertAlmostEqual(result["recommended"]["estimated_grid_energy_kwh"], 0.27)

    def test_base_load_consumes_available_solar(self):
        result = optimize(self.make(lambda d: d["site"].update(base_load_kw=100)))
        self.assertEqual(result["baseline"], result["recommended"])
        self.assertEqual(result["recommended"]["estimated_solar_energy_kwh"], 0)

    def test_abundant_solar_does_not_create_negative_grid_energy(self):
        result = optimize(self.make(lambda d: [x.update(estimated_solar_kw=10) for x in d["forecast"]["intervals"]]))
        self.assertAlmostEqual(result["baseline"]["estimated_grid_energy_kwh"], 0)
        self.assertEqual(result["estimated_grid_energy_reduction_percent"], 0)
        self.assertEqual(result["baseline"], result["recommended"])

    def test_total_energy_is_conserved(self):
        result = optimize(demo_request())
        for window in [result["baseline"], result["recommended"]]:
            self.assertAlmostEqual(window["estimated_solar_energy_kwh"] + window["estimated_grid_energy_kwh"], 0.27)

    def test_duration_is_not_replaced_by_best_single_hour(self):
        def edit(data):
            data["site"]["base_load_kw"] = 0
            data["job"].update(duration_minutes=120, estimated_power_w=1000)
            values = [1, 0, 0.8, 0.8, 0, 0, 0, 0, 0]
            for interval, power in zip(data["forecast"]["intervals"], values):
                interval["estimated_solar_kw"] = power
        req = self.make(edit)
        result = optimize(req)
        self.assertEqual(result["recommended"]["start"], (req.job.earliest_start + timedelta(hours=2)).isoformat())
        self.assertAlmostEqual(result["recommended"]["estimated_grid_energy_kwh"], 0.4)

    def test_impossible_deadline_rejected(self):
        req = demo_request()
        with self.assertRaises(ValidationError):
            self.make(lambda d: d["job"].update(deadline=(req.job.earliest_start + timedelta(minutes=89)).isoformat()))

    def test_exact_deadline_one_candidate(self):
        req = demo_request()
        result = optimize(self.make(lambda d: d["job"].update(deadline=(req.job.earliest_start + timedelta(minutes=90)).isoformat())))
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["baseline"], result["recommended"])

    def test_latest_start_included_when_not_on_step(self):
        req = demo_request()
        result = optimize(self.make(lambda d: d["job"].update(deadline=(req.job.earliest_start + timedelta(minutes=157)).isoformat())))
        self.assertEqual(result["candidate_count"], 6)  # 0, 15, 30, 45, 60, 67
        self.assertEqual(result["recommended"]["finish"], (req.job.earliest_start + timedelta(minutes=157)).isoformat())

    def test_missing_forecast_coverage_rejected(self):
        req = self.make(lambda d: d["forecast"]["intervals"].pop())
        with self.assertRaisesRegex(ValueError, "cover"):
            optimize(req)

    def test_gap_overlap_and_unsorted_intervals_rejected(self):
        for alteration in ["gap", "overlap", "unsorted"]:
            with self.subTest(alteration=alteration), self.assertRaises(ValidationError):
                def edit(data):
                    points = data["forecast"]["intervals"]
                    if alteration == "gap":
                        points.pop(2)
                    elif alteration == "overlap":
                        points[1]["start"] = points[0]["start"]
                    else:
                        points.reverse()
                self.make(edit)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(ValidationError):
            self.make(lambda d: d["job"].update(earliest_start="2026-09-12T09:00:00"))

    def test_nonfinite_and_negative_power_rejected(self):
        for value in [float("nan"), float("inf"), -1, 0]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.make(lambda d: d["job"].update(estimated_power_w=value))

    def test_demo_labels_survive_optimization(self):
        result = optimize(demo_request())
        self.assertEqual(result["status"], "preview_only")
        self.assertEqual(result["forecast_source"], "simulated")
        self.assertTrue(result["site_configuration_is_simulated"])


if __name__ == "__main__":
    unittest.main()
