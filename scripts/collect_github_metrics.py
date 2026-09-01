#!/usr/bin/env python3
"""Collect aggregate GitHub growth signals without third-party analytics."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


def request_json(path: str, token: str | None) -> Any:
    if token:
        request = urllib.request.Request(
            f"https://api.github.com/{path.lstrip('/')}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "atomlane-metrics",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    completed = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=False, timeout=30
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"gh api {path} failed")
    return json.loads(completed.stdout)


def optional_json(path: str, token: str | None) -> tuple[Any | None, str | None]:
    try:
        return request_json(path, token), None
    except (OSError, RuntimeError, ValueError, urllib.error.HTTPError) as exc:
        return None, f"{type(exc).__name__}: endpoint unavailable"


def labeled_issue_count(
    repository: str, label: str, token: str | None
) -> tuple[int | None, str | None]:
    """Return an aggregate public issue count without retaining issue content."""
    query = urllib.parse.urlencode(
        {"q": f"repo:{repository} is:issue label:{label}", "per_page": 1}
    )
    response, error = optional_json(f"search/issues?{query}", token)
    if not isinstance(response, dict) or not isinstance(response.get("total_count"), int):
        return None, error or "ValueError: endpoint returned an unexpected shape"
    return int(response["total_count"]), None


def load_existing(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": "1.0", "repository": "", "snapshots": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": "1.0", "repository": "", "snapshots": []}
    if not isinstance(data, dict) or not isinstance(data.get("snapshots"), list):
        return {"schema_version": "1.0", "repository": "", "snapshots": []}
    return data


def retain_last_traffic(snapshot: dict[str, Any], previous: Any) -> dict[str, Any]:
    """Carry forward an authenticated traffic sample when Actions lacks access."""
    if not isinstance(previous, dict) or snapshot["traffic_14d"].get("views") is not None:
        return snapshot
    previous_traffic = previous.get("traffic_14d")
    if not isinstance(previous_traffic, dict) or previous_traffic.get("views") is None:
        return snapshot
    snapshot["traffic_14d"] = previous_traffic
    snapshot["top_referrers"] = previous.get("top_referrers", [])
    snapshot["top_paths"] = previous.get("top_paths", [])
    snapshot["traffic_stale_from"] = previous.get("captured_at")
    return snapshot


def collect(repository: str, token: str | None) -> dict[str, Any]:
    repo = request_json(f"repos/{repository}", token)
    releases, releases_error = optional_json(f"repos/{repository}/releases?per_page=100", token)
    views, views_error = optional_json(f"repos/{repository}/traffic/views?per=day", token)
    clones, clones_error = optional_json(f"repos/{repository}/traffic/clones?per=day", token)
    referrers, referrers_error = optional_json(f"repos/{repository}/traffic/popular/referrers", token)
    paths, paths_error = optional_json(f"repos/{repository}/traffic/popular/paths", token)
    first_runs, first_runs_error = labeled_issue_count(repository, "first-run", token)
    benchmarks, benchmarks_error = labeled_issue_count(repository, "benchmark", token)
    release_downloads = 0
    release_count = 0
    if isinstance(releases, list):
        release_count = len(releases)
        release_downloads = sum(
            int(asset.get("download_count", 0))
            for release in releases
            for asset in release.get("assets", [])
            if isinstance(asset, dict)
        )
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stars": int(repo.get("stargazers_count", 0)),
        "forks": int(repo.get("forks_count", 0)),
        "watchers": int(repo.get("subscribers_count", 0)),
        "open_issues_and_prs": int(repo.get("open_issues_count", 0)),
        "releases": release_count,
        "release_asset_downloads": release_downloads,
        "first_run_reports": first_runs,
        "benchmark_reports": benchmarks,
        "traffic_14d": {
            "views": views.get("count") if isinstance(views, dict) else None,
            "unique_visitors": views.get("uniques") if isinstance(views, dict) else None,
            "clones": clones.get("count") if isinstance(clones, dict) else None,
            "unique_cloners": clones.get("uniques") if isinstance(clones, dict) else None,
        },
        "top_referrers": referrers if isinstance(referrers, list) else [],
        "top_paths": paths if isinstance(paths, list) else [],
        "unavailable": [
            label
            for label, error in (
                ("releases", releases_error),
                ("traffic_views", views_error),
                ("traffic_clones", clones_error),
                ("traffic_referrers", referrers_error),
                ("traffic_paths", paths_error),
                ("first_run_reports", first_runs_error),
                ("benchmark_reports", benchmarks_error),
            )
            if error
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", "cloudguo123/atomlane"),
    )
    parser.add_argument("--output", type=pathlib.Path, default=pathlib.Path("docs/metrics.json"))
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    snapshot = collect(args.repository, token)
    data = load_existing(args.output)
    snapshot = retain_last_traffic(snapshot, data.get("latest"))
    snapshots = [row for row in data["snapshots"] if row.get("captured_at") != snapshot["captured_at"]]
    snapshots.append(snapshot)
    data.update(
        {
            "schema_version": "1.0",
            "repository": args.repository,
            "metric_notes": {
                "traffic_window": "GitHub returns repository traffic for the latest 14 days and may delay updates.",
                "traffic_permission": "Scheduled traffic refresh requires an optional fine-grained ATOMLANE_TRAFFIC_TOKEN with repository Administration read access; otherwise the last authenticated sample is retained and marked stale.",
                "clone_intent": "Clones and release downloads are interest signals, not verified plugin installations.",
                "community_reports": "First-run and benchmark counts include public, self-selected issues with the corresponding label; they are not verified installations.",
                "privacy": "Only aggregate GitHub counters are retained; no visitor identity or tracking cookie is collected.",
            },
            "targets_30d": {
                "unique_visitors": 500,
                "clone_or_download_intents": 100,
                "stars": 50,
                "external_benchmarks": 10,
                "first_run_reports": 20,
                "compatibility_reports": 5,
                "contributors": 3,
                "awesome_list_entries": 2,
            },
            "snapshots": snapshots[-180:],
            "latest": snapshot,
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"repository": args.repository, "captured_at": snapshot["captured_at"], "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
