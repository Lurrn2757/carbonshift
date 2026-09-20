import json
import subprocess
import unittest
from unittest.mock import patch

from carbonshift.docker_runner import DockerRunner, RunnerError


class DockerAdapterTests(unittest.TestCase):
    def test_daemon_error_cannot_become_absent_container(self):
        response = subprocess.CompletedProcess([], 1, "", "Cannot connect to Docker daemon")
        with patch("carbonshift.docker_runner.subprocess.run", return_value=response):
            with self.assertRaises(RunnerError):
                DockerRunner().inspect("carbonshift-test")

    def test_successful_empty_listing_means_absent(self):
        response = subprocess.CompletedProcess([], 0, "", "")
        with patch("carbonshift.docker_runner.subprocess.run", return_value=response):
            self.assertIsNone(DockerRunner().inspect("carbonshift-test"))

    def test_inspect_parses_actual_times_and_labels(self):
        info = [{"Id":"a"*64, "Config":{"Labels":{"carbonshift.job":"test"}},
                 "State":{"Status":"exited","ExitCode":0,"StartedAt":"2026-09-12T10:00:00.123456789Z", "FinishedAt":"2026-09-12T10:00:05.123456789Z"}}]
        responses = [subprocess.CompletedProcess([],0,"a"*12,""), subprocess.CompletedProcess([],0,json.dumps(info),"")]
        with patch("carbonshift.docker_runner.subprocess.run", side_effect=responses):
            result = DockerRunner().inspect("carbonshift-test")
        self.assertEqual(result.status, "exited")
        self.assertAlmostEqual(result.finished_at - result.started_at, 5)

    def test_logs_retain_successful_command_stderr(self):
        response = subprocess.CompletedProcess([],0,"normal output\n","Python traceback\n")
        with patch("carbonshift.docker_runner.subprocess.run", return_value=response):
            logs = DockerRunner().logs("a"*64)
        self.assertIn("normal output", logs)
        self.assertIn("Python traceback", logs)
