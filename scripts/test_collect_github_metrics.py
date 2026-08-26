from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

import collect_github_metrics


class GithubMetricsTests(unittest.TestCase):
    def test_collect_keeps_only_aggregate_signals(self) -> None:
        responses = {
            "repos/cloudguo123/mac-parallel-accelerator": {
                "stargazers_count": 2,
                "forks_count": 1,
                "subscribers_count": 1,
                "open_issues_count": 4,
            },
            "repos/cloudguo123/mac-parallel-accelerator/releases?per_page=100": [
                {"assets": [{"download_count": 3}]}
            ],
            "repos/cloudguo123/mac-parallel-accelerator/traffic/views?per=day": {
                "count": 10,
                "uniques": 7,
            },
            "repos/cloudguo123/mac-parallel-accelerator/traffic/clones?per=day": {
                "count": 5,
                "uniques": 4,
            },
            "repos/cloudguo123/mac-parallel-accelerator/traffic/popular/referrers": [],
            "repos/cloudguo123/mac-parallel-accelerator/traffic/popular/paths": [],
            "search/issues?q=repo%3Acloudguo123%2Fmac-parallel-accelerator+is%3Aissue+label%3Afirst-run&per_page=1": {
                "total_count": 6
            },
            "search/issues?q=repo%3Acloudguo123%2Fmac-parallel-accelerator+is%3Aissue+label%3Abenchmark&per_page=1": {
                "total_count": 3
            },
        }
        with mock.patch.object(
            collect_github_metrics,
            "request_json",
            side_effect=lambda path, token: responses[path],
        ):
            result = collect_github_metrics.collect(
                "cloudguo123/mac-parallel-accelerator", token="test"
            )
        self.assertEqual(result["stars"], 2)
        self.assertEqual(result["traffic_14d"]["unique_cloners"], 4)
        self.assertEqual(result["release_asset_downloads"], 3)
        self.assertEqual(result["first_run_reports"], 6)
        self.assertEqual(result["benchmark_reports"], 3)
        self.assertNotIn("visitors", result)

    def test_labeled_issue_count_fails_closed_on_unexpected_shape(self) -> None:
        with mock.patch.object(
            collect_github_metrics,
            "optional_json",
            return_value=({"items": []}, None),
        ):
            count, error = collect_github_metrics.labeled_issue_count(
                "cloudguo123/mac-parallel-accelerator", "first-run", token=None
            )
        self.assertIsNone(count)
        self.assertIn("unexpected shape", error or "")

    def test_missing_file_starts_empty_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "metrics.json"
            result = collect_github_metrics.load_existing(path)
        self.assertEqual(result["snapshots"], [])

    def test_previous_authenticated_traffic_can_be_identified(self) -> None:
        previous = {
            "captured_at": "2026-08-25T00:00:00+00:00",
            "traffic_14d": {"views": 12, "unique_visitors": 7},
        }
        current = {"traffic_14d": {"views": None, "unique_visitors": None}}
        collect_github_metrics.retain_last_traffic(current, previous)
        self.assertEqual(current["traffic_14d"]["unique_visitors"], 7)
        self.assertEqual(current["traffic_stale_from"], previous["captured_at"])


if __name__ == "__main__":
    unittest.main()
