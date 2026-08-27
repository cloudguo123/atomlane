#!/usr/bin/env python3
"""Run the public verification suite and render a self-contained test dashboard."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import time
import unittest
from collections import Counter
from datetime import datetime, timezone
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

DOMAIN_META = {
    "test_atom_engine": {
        "label": "Atomic compiler & scheduler",
        "description": "Atom lowering, conflict safety, resource admission, hashes, and snapshots",
        "accent": "#65e6b4",
    },
    "test_atom_frontends": {
        "label": "Static workload frontends",
        "description": "Shell, package scripts, Make, Compose, and inferred dataflow",
        "accent": "#80b7ff",
    },
    "test_mcp_runtime": {
        "label": "MCP runtime & live execution",
        "description": "Failure propagation, output bounds, timeout semantics, and live progress",
        "accent": "#d7a6ff",
    },
    "test_growth_assets": {
        "label": "Growth assets & evidence sharing",
        "description": "Exact benchmark metrics, share cards, and honest comparison labels",
        "accent": "#f4cf78",
    },
    "test_collect_github_metrics": {
        "label": "Privacy-safe growth metrics",
        "description": "Aggregate repository signals and resilient snapshot storage",
        "accent": "#ff9fc6",
    },
    "test_long_benchmark": {
        "label": "Long-horizon benchmark evidence",
        "description": "Observed task runtimes, duration gates, and cumulative savings history",
        "accent": "#9ae7ff",
    },
}


class TimedResult(unittest.TextTestResult):
    """Collect compact, public-safe per-test outcomes and durations."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.started: dict[str, float] = {}
        self.records: list[dict[str, Any]] = []

    def startTest(self, test: unittest.case.TestCase) -> None:
        self.started[test.id()] = time.perf_counter()
        super().startTest(test)

    def _record(self, test: unittest.case.TestCase, status: str) -> None:
        test_id = test.id()
        elapsed = time.perf_counter() - self.started.pop(test_id, time.perf_counter())
        parts = test_id.split(".")
        module = parts[-3] if len(parts) >= 3 else "unknown"
        class_name = parts[-2] if len(parts) >= 2 else "Unknown"
        name = parts[-1]
        self.records.append(
            {
                "id": test_id,
                "module": module,
                "domain": DOMAIN_META.get(module, {}).get("label", module),
                "class": class_name,
                "name": name,
                "title": name.removeprefix("test_").replace("_", " "),
                "status": status,
                "duration_ms": round(elapsed * 1000, 2),
            }
        )

    def addSuccess(self, test: unittest.case.TestCase) -> None:
        super().addSuccess(test)
        self._record(test, "passed")

    def addFailure(self, test: unittest.case.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        self._record(test, "failed")

    def addError(self, test: unittest.case.TestCase, err: Any) -> None:
        super().addError(test, err)
        self._record(test, "error")

    def addSkip(self, test: unittest.case.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._record(test, "skipped")

    def addExpectedFailure(self, test: unittest.case.TestCase, err: Any) -> None:
        super().addExpectedFailure(test, err)
        self._record(test, "expected_failure")

    def addUnexpectedSuccess(self, test: unittest.case.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._record(test, "unexpected_success")


def run_command(name: str, argv: list[str]) -> dict[str, Any]:
    started = time.perf_counter()
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return {
        "name": name,
        "status": "passed" if completed.returncode == 0 else "failed",
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "returncode": completed.returncode,
    }


def run_regression_tests() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sys.path.insert(0, str(SCRIPTS))
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(str(SCRIPTS), pattern="test*.py")
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=TimedResult)
    result = runner.run(suite)
    duration_ms = round((time.perf_counter() - started) * 1000, 2)
    status = "passed" if result.wasSuccessful() else "failed"
    check = {
        "name": "Regression suite",
        "status": status,
        "duration_ms": duration_ms,
        "returncode": 0 if result.wasSuccessful() else 1,
    }
    return check, sorted(result.records, key=lambda item: (item["module"], item["class"], item["name"]))


def validate_metadata() -> dict[str, Any]:
    started = time.perf_counter()
    paths = [
        ROOT / ".codex-plugin" / "plugin.json",
        ROOT / ".agents" / "plugins" / "marketplace.json",
        ROOT / "catalog" / "scenarios.json",
        ROOT / "package.json",
        ROOT / "package-lock.json",
        ROOT / "benchmarks" / "project-result.schema.json",
        ROOT / "benchmarks" / "external-results.json",
    ]
    try:
        for path in paths:
            json.loads(path.read_text(encoding="utf-8"))
        status = "passed"
        returncode = 0
    except (OSError, json.JSONDecodeError):
        status = "failed"
        returncode = 1
    return {
        "name": "Manifest & catalog integrity",
        "status": status,
        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        "returncode": returncode,
    }


def bundle_check() -> dict[str, Any]:
    if not (ROOT / "node_modules").is_dir() or shutil.which("npm") is None:
        return {
            "name": "Reproducible UI bundle",
            "status": "skipped",
            "duration_ms": 0.0,
            "returncode": 0,
        }
    build = run_command("Reproducible UI bundle", ["npm", "run", "build:indicator"])
    if build["status"] == "passed":
        bundle = ROOT / "assets" / "parallel-indicator-host.bundle.js"
        first_digest = sha256(bundle)
        second = run_command("Reproducible UI bundle", ["npm", "run", "build:indicator"])
        build["duration_ms"] = round(build["duration_ms"] + second["duration_ms"], 2)
        if second["status"] != "passed" or sha256(bundle) != first_digest:
            build["status"] = "failed"
            build["returncode"] = second["returncode"] or 1
    return build


def command_version(argv: list[str]) -> str:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    line = (completed.stdout or completed.stderr).strip().splitlines()
    return line[0] if line else "unavailable"


def git_value(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unavailable"


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_long_benchmark() -> dict[str, Any]:
    path = ROOT / "docs" / "benchmark-results.json"
    if not path.exists():
        return {"available": False}
    try:
        benchmark = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    latest = benchmark.get("latest")
    cumulative = benchmark.get("cumulative")
    if not isinstance(latest, dict) or not isinstance(cumulative, dict):
        return {"available": False}
    return {"available": True, **benchmark}


def load_growth_metrics() -> dict[str, Any]:
    path = ROOT / "docs" / "metrics.json"
    if not path.exists():
        return {"available": False}
    try:
        metrics = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    latest = metrics.get("latest")
    targets = metrics.get("targets_30d")
    if not isinstance(latest, dict) or not isinstance(targets, dict):
        return {"available": False}
    return {"available": True, **metrics}


def build_report() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    python_sources = sorted(str(path.relative_to(ROOT)) for path in SCRIPTS.glob("*.py"))
    checks.append(
        run_command(
            "Python bytecode compilation",
            [sys.executable, "-m", "py_compile", *python_sources],
        )
    )
    regression_check, tests = run_regression_tests()
    checks.append(regression_check)
    checks.append(run_command("End-to-end MCP self-test", [sys.executable, "scripts/self_test.py"]))

    if shutil.which("ruff"):
        ruff_argv = ["ruff", "check", "--no-cache", "scripts"]
    elif shutil.which("uvx"):
        ruff_argv = ["uvx", "ruff@0.16.4", "check", "--no-cache", "scripts"]
    else:
        ruff_argv = [sys.executable, "-m", "ruff", "check", "--no-cache", "scripts"]
    checks.append(run_command("Ruff static analysis", ruff_argv))
    checks.append(validate_metadata())
    checks.append(bundle_check())

    status_counts = Counter(test["status"] for test in tests)
    passed = status_counts["passed"]
    overall = "passed" if all(check["status"] in {"passed", "skipped"} for check in checks) else "failed"
    domain_rows = []
    for module, meta in DOMAIN_META.items():
        domain_tests = [test for test in tests if test["module"] == module]
        domain_rows.append(
            {
                **meta,
                "module": module,
                "total": len(domain_tests),
                "passed": sum(test["status"] == "passed" for test in domain_tests),
                "duration_ms": round(sum(test["duration_ms"] for test in domain_tests), 2),
            }
        )

    bundle = ROOT / "assets" / "parallel-indicator-host.bundle.js"
    return {
        "schema_version": "1.0",
        "project": "AtomLane",
        "version": json.loads((ROOT / ".codex-plugin" / "plugin.json").read_text())["version"],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall": overall,
        "summary": {
            "total": len(tests),
            "passed": passed,
            "failed": len(tests) - passed,
            "pass_rate": round((passed / len(tests) * 100) if tests else 0, 2),
            "checks_passed": sum(check["status"] == "passed" for check in checks),
            "checks_total": len(checks),
            "elapsed_ms": round(sum(check["duration_ms"] for check in checks), 2),
        },
        "source": {
            "commit": git_value("rev-parse", "HEAD"),
            "commit_short": git_value("rev-parse", "--short=10", "HEAD"),
            "branch": os.environ.get("GITHUB_REF_NAME") or git_value("branch", "--show-current"),
            "repository": "cloudguo123/mac-parallel-accelerator",
        },
        "environment": {
            "os": f"{platform.system()} {platform.release()}",
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "node": command_version(["node", "--version"]),
            "runner": os.environ.get("RUNNER_NAME", "local verification runner"),
        },
        "provenance": {
            "bundle_sha256": sha256(bundle),
            "bundle_bytes": bundle.stat().st_size,
        },
        "checks": checks,
        "domains": domain_rows,
        "tests": tests,
        "benchmark": load_long_benchmark(),
        "growth": load_growth_metrics(),
        "scope_note": (
            "This dashboard reports behavioral regression and release-gate results. "
            "It is not a statement of line or branch coverage."
        ),
    }


def render_html(report: dict[str, Any]) -> str:
    data = json.dumps(report, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    overall = report["overall"].upper()
    generated = html.escape(report["generated_at"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="description" content="Verified tests, a five-minute benchmark, live progress, and public adoption signals for AtomLane.">
  <meta name="theme-color" content="#07100e">
  <link rel="canonical" href="https://cloudguo123.github.io/mac-parallel-accelerator/">
  <meta property="og:type" content="website">
  <meta property="og:title" content="AtomLane · Verified test report">
  <meta property="og:description" content="Parallelize only what is proven safe: 44 verified tests and a reproducible five-minute benchmark.">
  <meta property="og:url" content="https://cloudguo123.github.io/mac-parallel-accelerator/">
  <meta property="og:image" content="https://cloudguo123.github.io/mac-parallel-accelerator/share/social-preview.png">
  <meta property="og:image:width" content="1280">
  <meta property="og:image:height" content="640">
  <meta property="og:image:alt" content="AtomLane benchmark: 20 minutes 41 seconds serial equivalent, 5 minutes 10 seconds parallel wall time, 15 minutes 31 seconds saved">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="AtomLane · Verified test report">
  <meta name="twitter:description" content="Parallelize only what is proven safe, with live progress and honest savings evidence.">
  <meta name="twitter:image" content="https://cloudguo123.github.io/mac-parallel-accelerator/share/social-preview.png">
  <script type="application/ld+json">{{"@context":"https://schema.org","@type":"SoftwareApplication","name":"AtomLane","alternateName":"Mac Parallel Accelerator","applicationCategory":"DeveloperApplication","operatingSystem":"macOS","softwareVersion":"{html.escape(report['version'])}","codeRepository":"https://github.com/cloudguo123/mac-parallel-accelerator","url":"https://cloudguo123.github.io/mac-parallel-accelerator/","license":"https://opensource.org/license/mit","offers":{{"@type":"Offer","price":"0","priceCurrency":"USD"}}}}</script>
  <title>AtomLane · Test Report</title>
  <style>
    :root {{ color-scheme: dark; --bg:#07100e; --panel:#0c1815; --panel2:#10201c; --line:#20352f; --text:#ecf8f3; --muted:#8ba69c; --green:#65e6b4; --blue:#80b7ff; --purple:#d7a6ff; --red:#ff8f8f; }}
    * {{ box-sizing:border-box }}
    body {{ margin:0; background:radial-gradient(circle at 80% -10%,#163d32 0,transparent 34%),radial-gradient(circle at 5% 20%,#102b32 0,transparent 28%),var(--bg); color:var(--text); font:15px/1.55 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; min-height:100vh }}
    a {{ color:var(--green); text-decoration:none }} a:hover {{ text-decoration:underline }}
    .wrap {{ width:min(1180px,calc(100% - 32px)); margin:auto; padding:46px 0 72px }}
    .nav {{ display:flex; justify-content:space-between; align-items:center; color:var(--muted); margin-bottom:50px }}
    .brand {{ color:var(--text); font-weight:700; letter-spacing:.02em }} .navlinks {{ display:flex; gap:20px }}
    .eyebrow {{ text-transform:uppercase; letter-spacing:.18em; font-size:12px; color:var(--green); font-weight:800 }}
    h1 {{ margin:14px 0 12px; max-width:820px; font-size:clamp(42px,7vw,82px); line-height:.98; letter-spacing:-.055em }}
    .lede {{ color:var(--muted); font-size:18px; max-width:720px; margin:0 }}
    .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:25px }} .button {{ display:inline-flex; align-items:center; justify-content:center; border-radius:11px; padding:11px 16px; color:#06100d; background:var(--green); font-weight:800; border:1px solid var(--green) }} .button.secondary {{ color:var(--text); background:#0b1714; border-color:var(--line) }} .button:hover {{ text-decoration:none; filter:brightness(1.06) }}
    .hero {{ display:grid; grid-template-columns:1fr 260px; gap:32px; align-items:center; margin-bottom:46px }}
    .seal {{ width:220px; aspect-ratio:1; margin-left:auto; border-radius:50%; display:grid; place-items:center; background:conic-gradient(var(--green) calc(var(--rate)*1%),#1a2b26 0); box-shadow:0 0 80px #45d89d1b; position:relative }}
    .seal:before {{ content:""; position:absolute; inset:11px; border-radius:50%; background:var(--bg); border:1px solid var(--line) }}
    .seal-inner {{ z-index:1; text-align:center }} .rate {{ font-size:46px; font-weight:850; letter-spacing:-.05em }} .seal-label {{ color:var(--muted); font-size:12px; letter-spacing:.13em; text-transform:uppercase }}
    .grid4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:30px 0 42px }}
    .grid6 {{ display:grid; grid-template-columns:repeat(6,1fr); gap:10px }}
    .metric,.panel {{ background:linear-gradient(145deg,#10211dce,#0b1714e8); border:1px solid var(--line); border-radius:16px }}
    .metric {{ padding:20px }} .metric strong {{ display:block; font-size:28px; letter-spacing:-.03em }} .metric span {{ color:var(--muted); font-size:13px }}
    .section-title {{ display:flex; justify-content:space-between; align-items:end; margin:46px 0 16px }} .section-title h2 {{ margin:0; font-size:26px; letter-spacing:-.03em }} .section-title p {{ margin:0; color:var(--muted) }}
    .panel {{ padding:22px }}
    .checks {{ display:grid; grid-template-columns:repeat(2,1fr); gap:12px }}
    .check {{ padding:17px; border:1px solid var(--line); border-radius:12px; background:#0b1714 }} .checktop {{ display:flex; justify-content:space-between; gap:10px }}
    .pill {{ border-radius:999px; padding:3px 9px; font-size:11px; font-weight:800; letter-spacing:.08em; text-transform:uppercase; background:#65e6b418; color:var(--green); border:1px solid #65e6b43b }}
    .pill.failed {{ color:var(--red); border-color:#ff8f8f45; background:#ff8f8f12 }} .pill.skipped {{ color:#f4cf78; border-color:#f4cf7840; background:#f4cf7812 }}
    .bar {{ height:5px; background:#1a2a25; border-radius:10px; overflow:hidden; margin-top:14px }} .bar i {{ height:100%; display:block; background:linear-gradient(90deg,var(--green),var(--blue)); border-radius:10px }}
    .benchmark {{ position:relative; overflow:hidden; border-color:#65e6b454; box-shadow:0 28px 80px #0005 }} .benchmark:before {{ content:""; position:absolute; width:420px; height:420px; border-radius:50%; background:#52dfa318; filter:blur(80px); right:-180px; top:-250px; pointer-events:none }}
    .benchmark-head {{ display:flex; justify-content:space-between; gap:20px; align-items:start; margin-bottom:22px }} .benchmark-head h3 {{ font-size:28px; margin:4px 0 }} .benchmark-head p {{ color:var(--muted); margin:0; max-width:670px }}
    .long-pill {{ display:inline-flex; gap:8px; align-items:center; color:var(--green); font-weight:750; background:#65e6b412; border:1px solid #65e6b43e; border-radius:999px; padding:8px 12px; white-space:nowrap }} .long-pill:before {{ content:""; width:7px; height:7px; border-radius:50%; background:var(--green); box-shadow:0 0 12px var(--green) }}
    .bench-metric {{ background:#08130fbd; border:1px solid var(--line); border-radius:12px; padding:14px }} .bench-metric strong {{ display:block; font-size:21px }} .bench-metric span {{ color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.07em }}
    .comparison {{ display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-top:20px }} .compare-row {{ display:grid; grid-template-columns:130px 1fr 72px; gap:12px; align-items:center; margin:12px 0 }} .compare-track {{ height:12px; background:#152722; border-radius:999px; overflow:hidden }} .compare-fill {{ display:block; height:100%; border-radius:999px; background:linear-gradient(90deg,var(--blue),var(--purple)) }} .compare-fill.parallel {{ background:linear-gradient(90deg,var(--green),#9ef0d1) }}
    .lanes {{ display:grid; gap:8px }} .lane {{ display:grid; grid-template-columns:180px 1fr 72px; gap:12px; align-items:center; font-size:13px }} .lane-track {{ height:26px; background:#142620; border-radius:7px; padding:3px }} .lane-fill {{ display:flex; align-items:center; height:100%; border-radius:5px; padding:0 8px; color:#06100d; background:linear-gradient(90deg,var(--green),#9ae7ff); font-size:10px; font-weight:850; letter-spacing:.07em }}
    .history {{ display:flex; align-items:end; gap:7px; min-height:90px; padding-top:14px }} .history-bar {{ min-width:18px; flex:1; max-width:42px; border-radius:5px 5px 2px 2px; background:linear-gradient(180deg,var(--purple),#5f7ee7); position:relative }} .history-bar:hover:after {{ content:attr(data-label); position:absolute; bottom:calc(100% + 6px); left:50%; transform:translateX(-50%); color:var(--text); background:#07100e; border:1px solid var(--line); border-radius:6px; padding:4px 7px; white-space:nowrap; font-size:11px }}
    .domains {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px }} .domain {{ position:relative; overflow:hidden }} .domain:before {{ content:""; position:absolute; inset:0 auto 0 0; width:3px; background:var(--accent) }}
    .domain h3 {{ margin:0 0 7px; font-size:17px }} .domain p {{ margin:0; min-height:48px; color:var(--muted) }} .domain-foot {{ display:flex; justify-content:space-between; margin-top:18px; font-variant-numeric:tabular-nums }}
    .toolbar {{ display:flex; gap:10px; margin-bottom:13px }} input {{ flex:1; min-width:0; color:var(--text); background:#08120f; border:1px solid var(--line); border-radius:10px; padding:11px 13px; outline:none }} input:focus {{ border-color:#65e6b478 }}
    table {{ width:100%; border-collapse:collapse }} th {{ text-align:left; color:var(--muted); font-size:11px; letter-spacing:.1em; text-transform:uppercase; padding:10px 12px; border-bottom:1px solid var(--line) }} td {{ padding:13px 12px; border-bottom:1px solid #172a25; vertical-align:top }} tr:last-child td {{ border-bottom:0 }} .test-title {{ font-weight:650 }} .test-id {{ font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--muted) }} .time {{ font-variant-numeric:tabular-nums; white-space:nowrap }}
    .provenance {{ display:grid; grid-template-columns:repeat(3,1fr); gap:1px; background:var(--line); padding:1px; border-radius:14px; overflow:hidden }} .prov {{ background:var(--panel); padding:18px }} .prov span {{ display:block; color:var(--muted); font-size:12px }} .prov code {{ display:block; margin-top:5px; overflow-wrap:anywhere; font-size:12px; color:#d9ece5 }}
    .growth {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px }} .growth-card {{ padding:18px; border:1px solid var(--line); background:#0b1714; border-radius:13px }} .growth-card strong {{ display:block; font-size:28px }} .growth-card span {{ color:var(--muted); font-size:12px }}
    .demo {{ width:100%; display:block; border-radius:13px; border:1px solid var(--line); background:#07100e }}
    .note {{ margin-top:15px; color:var(--muted); font-size:13px }} footer {{ margin-top:46px; padding-top:22px; border-top:1px solid var(--line); display:flex; justify-content:space-between; color:var(--muted); font-size:13px }}
    @media(max-width:1000px) {{ .grid6 {{ grid-template-columns:repeat(3,1fr) }} }}
    @media(max-width:800px) {{ .hero {{ grid-template-columns:1fr }} .seal {{ margin:18px auto 0 }} .grid4,.growth {{ grid-template-columns:repeat(2,1fr) }} .checks,.domains,.comparison {{ grid-template-columns:1fr }} .provenance {{ grid-template-columns:1fr }} .navlinks {{ display:none }} .benchmark-head {{ display:block }} .long-pill {{ margin-top:14px }} }}
    @media(max-width:520px) {{ .grid4 {{ grid-template-columns:1fr 1fr }} .wrap {{ width:min(100% - 22px,1180px); padding-top:24px }} .panel {{ padding:14px }} th:nth-child(2),td:nth-child(2) {{ display:none }} }}
  </style>
</head>
<body>
<main class="wrap">
  <nav class="nav"><div class="brand">ATOMLANE / VERIFY</div><div class="navlinks"><a href="https://github.com/cloudguo123/mac-parallel-accelerator">Source</a><a href="https://github.com/cloudguo123/mac-parallel-accelerator#install-in-two-commands">Install</a><a href="https://github.com/cloudguo123/mac-parallel-accelerator/issues/new?template=first-run.yml">First run</a><a href="https://github.com/cloudguo123/mac-parallel-accelerator/discussions">Discuss</a><a href="test-results.json">Raw JSON</a></div></nav>
  <section class="hero">
    <div><div class="eyebrow">AtomLane release verification · v<span id="version"></span></div><h1>Parallelize only what is proven safe.</h1><p class="lede">AtomLane compiles local work into verified atomic plans, then runs safe concurrency on macOS with visible progress and honest time-savings evidence.</p><div class="actions"><a class="button" href="https://github.com/cloudguo123/mac-parallel-accelerator#install-in-two-commands">Install free</a><a class="button secondary" href="https://github.com/cloudguo123/mac-parallel-accelerator/issues/new?template=first-run.yml">Report first run</a><a class="button secondary" href="https://github.com/cloudguo123/mac-parallel-accelerator/issues/new?template=benchmark.yml">Share a benchmark</a></div></div>
    <div class="seal" id="seal"><div class="seal-inner"><div class="rate" id="rate">—</div><div class="seal-label">tests passing</div></div></div>
  </section>
  <div class="grid4" id="metrics"></div>
  <div class="section-title"><div><div class="eyebrow">Visible by default</div><h2>Watch the work while it runs</h2></div><p>20-second deterministic demo</p></div>
  <section class="panel"><img class="demo" src="share/demo.gif" width="960" height="540" alt="Live execution counters and estimated time saved updating during a parallel run"><p class="note">The real PTY runner streams elapsed time, running/ready/completed/failed counters, and current savings throughout long execution.</p></section>
  <div class="section-title"><div><div class="eyebrow">Long-horizon evidence</div><h2>Five-minute parallel benchmark</h2></div><p>Fast regression report retained below</p></div>
  <section class="panel benchmark" id="benchmark"></section>
  <div class="section-title"><div><div class="eyebrow">Quality gates</div><h2>Release checks</h2></div><p id="overall">{overall}</p></div>
  <section class="checks" id="checks"></section>
  <div class="section-title"><div><div class="eyebrow">Behavioral coverage</div><h2>Verified subsystems</h2></div><p>Every regression grouped by responsibility</p></div>
  <section class="domains" id="domains"></section>
  <div class="section-title"><div><div class="eyebrow">Test inventory</div><h2>All regression cases</h2></div><p id="test-count"></p></div>
  <section class="panel"><div class="toolbar"><input id="search" type="search" placeholder="Filter by test, subsystem, or behavior…" aria-label="Filter tests"></div><div style="overflow:auto"><table><thead><tr><th>Behavior</th><th>Subsystem</th><th>Status</th><th>Duration</th></tr></thead><tbody id="tests"></tbody></table></div></section>
  <div class="section-title"><div><div class="eyebrow">Reproducibility</div><h2>Build provenance</h2></div><p>Machine-readable evidence included</p></div>
  <section class="provenance" id="provenance"></section>
  <div class="section-title"><div><div class="eyebrow">Open growth</div><h2>Community pulse</h2></div><p>Aggregate GitHub signals only</p></div>
  <section class="panel"><div class="growth" id="growth"></div><p class="note" id="growth-note"></p></section>
  <p class="note" id="scope"></p>
  <footer><span>Generated {generated}</span><span>Report schema 1.0</span></footer>
</main>
<script id="report-data" type="application/json">{data}</script>
<script>
const d=JSON.parse(document.getElementById('report-data').textContent);
const q=s=>document.querySelector(s);
const E=(tag,className='',value=null)=>{{const node=document.createElement(tag);if(className)node.className=className;if(value!==null&&value!==undefined)node.textContent=String(value);return node;}};
const add=(parent,...children)=>{{parent.append(...children.filter(Boolean));return parent;}};
const clear=(selector,...children)=>{{const node=q(selector);node.replaceChildren(...children);return node;}};
const finite=(value,fallback=0)=>{{const number=Number(value);return Number.isFinite(number)?number:fallback;}};
const pct=value=>Math.max(0,Math.min(100,finite(value))).toFixed(3)+'%';
const ms=n=>finite(n)>=1000?(finite(n)/1000).toFixed(2)+'s':Math.round(finite(n))+'ms';
const span=s=>finite(s)>=3600?(finite(s)/3600).toFixed(2)+'h':finite(s)>=60?(finite(s)/60).toFixed(2)+'m':finite(s).toFixed(1)+'s';
const pillClass=status=>status==='failed'||status==='skipped'?'pill '+status:'pill';
const valueCard=(className,label,value)=>add(E('div',className),E('strong','',value),E('span','',label));
const compareRow=(label,width,value,parallel=false)=>{{const fill=E('i',parallel?'compare-fill parallel':'compare-fill');fill.style.width=pct(width);return add(E('div','compare-row'),E('span','',label),add(E('div','compare-track'),fill),E('strong','',value));}};

q('#version').textContent=String(d.version);
q('#rate').textContent=finite(d.summary.pass_rate).toFixed(1)+'%';
q('#seal').style.setProperty('--rate',String(Math.max(0,Math.min(100,finite(d.summary.pass_rate)))));
q('#overall').textContent=d.overall==='passed'?'ALL REQUIRED GATES PASSED':'VERIFICATION FAILED';
q('#overall').style.color=d.overall==='passed'?'var(--green)':'var(--red)';

const metrics=[['Tests',d.summary.total],['Passed',d.summary.passed],['Release gates',d.summary.checks_passed+'/'+d.summary.checks_total],['Total wall time',ms(d.summary.elapsed_ms)]];
clear('#metrics',...metrics.map(item=>valueCard('metric',item[0],item[1])));

const b=d.benchmark;
if(!b.available){{
  const copy=add(E('div'),E('h3','','First long benchmark pending'),E('p','','The five-minute evidence run is independent from fast CI and will appear here after its first successful execution.'));
  clear('#benchmark',add(E('div','benchmark-head'),copy,E('span','long-pill','Scheduled benchmark')));
}}else{{
  const x=b.latest,c=b.cumulative,serial=finite(x.serial_equivalent.seconds),wall=finite(x.parallel.wall_time_seconds),historyRows=Array.isArray(b.history)?b.history:[],maxSaved=Math.max(...historyRows.map(row=>finite(row.saved_seconds)),1),longEnough=finite(x.minimum_task_seconds)>=300;
  const headerCopy=add(E('div'),E('div','eyebrow',String(x.status)+' · '+String(x.task_count)+' independent workloads'),E('h3','',longEnough?'Every task ran beyond five minutes':'Every task met the configured duration gate'),E('p','','Observed task runtimes are summed for the serial equivalent; no synthetic multiplier and no 20-minute serial rerun. Savings equal that observed work minus actual parallel wall time.'));
  const header=add(E('div','benchmark-head'),headerCopy,E('span','long-pill','Minimum gate '+span(x.minimum_task_seconds)));
  const benchmarkMetrics=[['Per-task minimum',span(x.minimum_task_seconds)],['Parallel wall time',span(wall)],['Serial equivalent',span(serial)],['Saved this run',span(x.savings.seconds)],['Cumulative saved',span(c.saved_seconds)],['Observed speedup',finite(x.savings.speedup_multiplier).toFixed(2)+'×']];
  const metricGrid=add(E('div','grid6'),...benchmarkMetrics.map(item=>valueCard('bench-metric',item[0],item[1])));
  const comparisonLeft=add(E('div'),E('h4','','Serial-equivalent vs parallel'),compareRow('Serial equivalent',100,span(serial)),compareRow('Parallel',serial>0?wall/serial*100:0,span(wall),true),E('p','note',finite(x.savings.percent).toFixed(2)+'% less wall time · '+(finite(x.savings.parallel_efficiency)*100).toFixed(1)+'% parallel efficiency · peak '+String(x.parallel.peak_concurrency)+' workers'));
  const history=E('div','history');
  historyRows.forEach(row=>{{const bar=E('i','history-bar');bar.style.height=Math.max(12,finite(row.saved_seconds)/maxSaved*76).toFixed(3)+'px';bar.dataset.label=span(row.saved_seconds)+' saved';history.append(bar);}});
  const comparisonRight=add(E('div'),E('h4','','Cumulative savings history'),history,E('p','note',String(c.run_count)+' verified run'+(c.run_count===1?'':'s')+' · '+span(c.saved_seconds)+' saved in total'));
  const comparison=add(E('div','comparison'),comparisonLeft,comparisonRight);
  const lanes=E('div','lanes'),targetSeconds=Math.max(finite(x.target_task_seconds),1);
  (Array.isArray(x.tasks)?x.tasks:[]).forEach(task=>{{const fill=E('i','lane-fill',String(task.status).toUpperCase());fill.style.width=pct(finite(task.duration_seconds)/targetSeconds*100);lanes.append(add(E('div','lane'),E('span','',task.label),add(E('div','lane-track'),fill),E('strong','',span(task.duration_seconds))));}});
  const resource=x.resource||{{}};
  const methodNote='Method: '+String(x.method)+'. Latest run '+String(x.run_id)+' on '+String(resource.machine)+' ('+String(resource.logical_cpus)+' logical CPUs). Source commit '+String(x.commit).slice(0,10)+'.';
  clear('#benchmark',header,metricGrid,comparison,E('h4','','Concurrent task timeline'),lanes,E('p','note',methodNote));
}}

const maxCheck=Math.max(...d.checks.map(check=>finite(check.duration_ms)),1);
clear('#checks',...d.checks.map(check=>{{const top=add(E('div','checktop'),E('strong','',check.name),E('span',pillClass(check.status),check.status));const fill=E('i');fill.style.width=pct(Math.max(2,finite(check.duration_ms)/maxCheck*100));return add(E('article','check'),top,add(E('div','bar'),fill),E('small','',ms(check.duration_ms)));}}));

clear('#domains',...d.domains.map(domain=>{{const article=E('article','panel domain');const accent=/^#[0-9A-Fa-f]{{6}}$/.test(String(domain.accent))?String(domain.accent):'#65e6b4';article.style.setProperty('--accent',accent);const foot=add(E('div','domain-foot'),E('strong','',String(domain.passed)+' / '+String(domain.total)+' passed'),E('span','',ms(domain.duration_ms)));return add(article,E('h3','',domain.label),E('p','',domain.description),foot);}}));

function draw(list){{
  q('#test-count').textContent=String(list.length)+' of '+String(d.tests.length)+' shown';
  const rows=list.map(test=>{{const identity=add(E('td'),E('div','test-title',test.title),E('div','test-id',String(test.class)+'.'+String(test.name)));return add(E('tr'),identity,E('td','',test.domain),add(E('td'),E('span',pillClass(test.status),test.status)),E('td','time',ms(test.duration_ms)));}});
  if(!rows.length){{const empty=E('td','','No matching tests.');empty.colSpan=4;rows.push(add(E('tr'),empty));}}
  clear('#tests',...rows);
}}
draw(d.tests);
q('#search').addEventListener('input',event=>{{const term=String(event.target.value).toLowerCase();draw(d.tests.filter(test=>Object.values(test).join(' ').toLowerCase().includes(term)));}});

const provenance=[['Verified commit',d.source.commit],['Branch',d.source.branch],['Runner',d.environment.runner],['Platform',d.environment.os+' · '+d.environment.architecture],['Toolchain','Python '+d.environment.python+' · Node '+d.environment.node],['UI bundle SHA-256',d.provenance.bundle_sha256]];
clear('#provenance',...provenance.map(item=>add(E('div','prov'),E('span','',item[0]),E('code','',item[1]))));
q('#scope').textContent=String(d.scope_note);

const g=d.growth;
if(!g.available){{
  clear('#growth',valueCard('growth-card','First aggregate snapshot','Pending'));
  q('#growth-note').textContent='The weekly metrics workflow will publish stars, forks, release downloads, public first-run reports, and GitHub’s rolling 14-day traffic counters.';
}}else{{
  const latest=g.latest,traffic=latest.traffic_14d||{{}},growthMetrics=[['Stars',latest.stars],['Forks',latest.forks],['14d unique visitors',traffic.unique_visitors??'—'],['14d unique cloners',traffic.unique_cloners??'—'],['Release downloads',latest.release_asset_downloads],['First-run reports',latest.first_run_reports??'—'],['Benchmark reports',latest.benchmark_reports??'—'],['30d first-run target',g.targets_30d.first_run_reports??20]];
  clear('#growth',...growthMetrics.map(item=>valueCard('growth-card',item[0],item[1])));
  const stale=latest.traffic_stale_from?' Traffic last authenticated '+String(latest.traffic_stale_from)+'.':'';
  q('#growth-note').textContent='Captured '+String(latest.captured_at)+'.'+stale+' Traffic is a rolling 14-day GitHub window and may lag. Clones, downloads, and self-selected public reports indicate intent—not verified installations.';
}}
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=pathlib.Path, default=ROOT / "docs" / "index.html")
    parser.add_argument(
        "--json-output",
        type=pathlib.Path,
        default=ROOT / "docs" / "test-results.json",
    )
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(report), encoding="utf-8")
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(ROOT / "benchmarks" / "project-result.schema.json", ROOT / "docs")
    shutil.copy2(ROOT / "benchmarks" / "external-results.json", ROOT / "docs")
    print(
        json.dumps(
            {
                "overall": report["overall"],
                "tests": report["summary"]["total"],
                "passed": report["summary"]["passed"],
                "output": str(args.output),
            }
        )
    )
    return 0 if report["overall"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
