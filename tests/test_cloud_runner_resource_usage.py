import tempfile
import unittest
from pathlib import Path
from unittest import mock

from outcomeci.cloud_runner.resource_usage import ResourceUsageSampler


class ResourceUsageSamplerTests(unittest.TestCase):
    def test_reports_execution_deltas_limits_and_peaks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cgroup = root / "cgroup"
            workspace = root / "workspace"
            cgroup.mkdir()
            workspace.mkdir()
            (cgroup / "cpu.stat").write_text(
                "usage_usec 1000000\nuser_usec 800000\nsystem_usec 200000\n"
                "nr_periods 10\nnr_throttled 2\nthrottled_usec 3000\n",
                encoding="ascii",
            )
            (cgroup / "cpu.max").write_text("200000 100000\n", encoding="ascii")
            (cgroup / "memory.current").write_text("400\n", encoding="ascii")
            (cgroup / "memory.peak").write_text("500\n", encoding="ascii")
            (cgroup / "memory.max").write_text("4096\n", encoding="ascii")
            sampler = ResourceUsageSampler(
                workspace,
                interval_seconds=3600,
                cgroup_directory=cgroup,
            )

            with (
                mock.patch(
                    "outcomeci.cloud_runner.resource_usage.time.monotonic",
                    side_effect=(10.0, 10.0, 11.0, 12.0, 12.0),
                ),
                mock.patch(
                    "outcomeci.cloud_runner.resource_usage._filesystem_usage",
                    side_effect=((1000, 5000), (1800, 5000), (1400, 5000)),
                ),
            ):
                sampler.start()
                (cgroup / "cpu.stat").write_text(
                    "usage_usec 1500000\nnr_throttled 5\nthrottled_usec 9000\n",
                    encoding="ascii",
                )
                (cgroup / "memory.current").write_text("700\n", encoding="ascii")
                (cgroup / "memory.peak").write_text("900\n", encoding="ascii")
                sampler._sample()
                report = sampler.stop()

        self.assertEqual(
            report,
            {
                "schema_version": 1,
                "sample_count": 3,
                "sampled_milliseconds": 2000,
                "cpu_usage_usec": 500000,
                "cpu_throttled_usec": 6000,
                "cpu_nr_throttled": 3,
                "cpu_peak_millicores": 500,
                "cpu_limit_millicores": 2000,
                "memory_peak_bytes": 900,
                "memory_limit_bytes": 4096,
                "workspace_peak_bytes": 800,
                "workspace_final_bytes": 400,
                "workspace_limit_bytes": 5000,
            },
        )
        self.assertIs(sampler.stop(), report)

    def test_omits_unavailable_sources_and_does_not_raise(self):
        with tempfile.TemporaryDirectory() as directory:
            sampler = ResourceUsageSampler(
                Path(directory),
                interval_seconds=3600,
                cgroup_directory=Path(directory) / "missing",
            )
            with mock.patch(
                "outcomeci.cloud_runner.resource_usage._filesystem_usage", return_value=None
            ):
                sampler.start()
                self.assertIsNone(sampler.stop())


if __name__ == "__main__":
    unittest.main()
