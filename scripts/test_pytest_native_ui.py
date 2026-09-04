from __future__ import annotations

import contextlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys
import time
import unittest

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import live_runner
import mcp_server

ROOT = pathlib.Path(__file__).resolve().parents[1]


class PytestNativeProgressTests(unittest.TestCase):
    def test_atomic_progress_context_labels_configuration_and_case_hints(self) -> None:
        context = mcp_server._atomic_progress_context(
            {
                "test_suites": [
                    {
                        "strategy": "native_worker_pool",
                        "configured_workers": 4,
                        "case_count_hint": 60,
                    },
                    {
                        "strategy": "native_worker_pool",
                        "configured_workers": 2,
                        "case_count_hint": 40,
                    },
                ]
            }
        )

        self.assertEqual(
            context,
            {
                "native_workers_configured": 4,
                "test_cases_planned": 100,
                "savings_pending_native_report": True,
            },
        )

    def test_native_progress_defers_savings_until_baseline_or_junit(self) -> None:
        reporter = mcp_server.ProgressReporter(
            1,
            None,
            time.monotonic(),
            {
                "native_workers_configured": 4,
                "test_cases_planned": 100,
                "savings_pending_native_report": True,
            },
        )
        reporter.task_started("pytest-suite")

        snapshot = reporter.snapshot()
        self.assertEqual(snapshot["native_workers_configured"], 4)
        self.assertEqual(snapshot["test_cases_planned"], 100)
        self.assertTrue(snapshot["savings_pending_native_report"])
        self.assertIsNone(snapshot["estimated_saved_so_far_seconds"])
        self.assertTrue(snapshot["savings_eligible_so_far"])

    def test_regular_progress_shape_and_estimate_remain_compatible(self) -> None:
        reporter = mcp_server.ProgressReporter(1, None, time.monotonic())
        snapshot = reporter.snapshot()

        self.assertNotIn("native_workers_configured", snapshot)
        self.assertNotIn("test_cases_planned", snapshot)
        self.assertNotIn("savings_pending_native_report", snapshot)
        self.assertIsInstance(snapshot["estimated_saved_so_far_seconds"], float)

    def test_mcp_and_console_progress_show_configured_not_observed_workers(self) -> None:
        snapshot = {
            "elapsed_seconds": 12.3,
            "running_tasks": 1,
            "ready_tasks": 0,
            "completed_tasks": 0,
            "task_count": 1,
            "failed_tasks": 0,
            "estimated_saved_so_far_seconds": None,
            "savings_eligible_so_far": True,
            "native_workers_configured": 4,
            "test_cases_planned": 100,
            "savings_pending_native_report": True,
        }

        mcp_output = io.StringIO()
        with contextlib.redirect_stdout(mcp_output):
            mcp_server._progress_callback("test-token")(snapshot)
        notification = json.loads(mcp_output.getvalue())
        message = notification["params"]["message"]
        self.assertIn("原生 workers 4（配置）", message)
        self.assertIn("计划用例 100（提示）", message)
        self.assertIn("节约待串行基线/JUnit", message)
        self.assertNotIn("峰值 4", message)

        console_output = io.StringIO()
        with contextlib.redirect_stdout(console_output):
            live_runner.ConsoleProgress()(snapshot)
        self.assertIn("原生 workers 4（配置）", console_output.getvalue())
        self.assertIn("节约待串行基线/JUnit", console_output.getvalue())


class PytestNativeIndicatorStaticTests(unittest.TestCase):
    def test_indicator_distinguishes_native_configuration_from_outer_peak(self) -> None:
        indicator = (ROOT / "assets" / "parallel-indicator.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("native_workers_configured", indicator)
        self.assertIn("outer_peak_concurrency", indicator)
        self.assertIn("workers 是配置值，非运行时实测", indicator)
        self.assertIn("待基线/JUnit", indicator)
        self.assertIn("计划提示", indicator)
        self.assertIn("estimated_sum_of_testcase_durations", indicator)
        self.assertIn("累计已入账", indicator)
        self.assertIn("未计入累计已入账", indicator)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for DOM smoke test")
    def test_indicator_executes_input_progress_and_result_lifecycle(self) -> None:
        indicator = (ROOT / "assets" / "parallel-indicator.html").read_text(
            encoding="utf-8"
        )
        inline_scripts = re.findall(
            r"<script>\s*(.*?)\s*</script>", indicator, re.DOTALL
        )
        self.assertEqual(len(inline_scripts), 1)
        harness = (
            "import vm from 'node:vm';\nconst source = "
            + json.dumps(inline_scripts[0])
            + r""";
const elements = new Map();
const listeners = new Map();
let now = 1000;
let intervalCallback = null;
function element(id) {
  if (!elements.has(id)) elements.set(id, { textContent: "", className: "", style: {} });
  return elements.get(id);
}
const windowValue = {
  openai: null,
  addEventListener(name, callback) {
    if (!listeners.has(name)) listeners.set(name, []);
    listeners.get(name).push(callback);
  },
};
const sandbox = {
  window: windowValue,
  document: { getElementById: element },
  location: { protocol: "https:" },
  performance: { now: () => now },
  setInterval(callback) { intervalCallback = callback; return 1; },
  clearInterval() { intervalCallback = null; },
  setTimeout() { throw new Error("unexpected preview timeout"); },
};
vm.runInNewContext(source, sandbox);
function emit(name, event) {
  for (const callback of listeners.get(name) || []) callback(event);
}
function equal(actual, expected, label) {
  if (actual !== expected) throw new Error(`${label}: ${actual} !== ${expected}`);
}
emit("atomlane:tool-input", { detail: { compiled_plan: { test_suites: [{
  strategy: "native_worker_pool", configured_workers: 4, case_count_hint: 100,
}] } } });
equal(element("state").textContent, "原生 pytest worker 池运行中…", "running state");
equal(element("peak").textContent, "4（配置）", "configured workers");
equal(element("current-saved").textContent, "待基线/JUnit", "pending savings");
equal(element("cumulative-saved").textContent, "完成后更新", "pending ledger");
equal(element("cumulative-estimated").textContent, "完成后更新", "pending estimates");
if (intervalCallback == null) throw new Error("live timer was not started");
now = 2500;
intervalCallback();
equal(element("elapsed").textContent, "1.5 秒", "live elapsed timer");
if (!element("note").textContent.includes("实时运行中")) throw new Error("missing live note");
emit("message", { data: { method: "notifications/progress", params: {
  message: "已运行 2.0s · 运行中 1 · 已完成 0/1 · 原生 workers 4（配置） · 计划用例 100（提示） · 节约待串行基线/JUnit",
} } });
equal(element("elapsed").textContent, "2.0 秒", "progress elapsed");
equal(element("result").textContent, "1 运行 · 0/1 完成", "progress counts");
equal(element("current-saved").textContent, "待基线/JUnit", "progress savings");
emit("atomlane:tool-result", { detail: {
  indicator: {
    parallel: true, parallelism_kind: "native_worker_pool",
    native_workers_configured: 4, native_workers_observed: null,
    outer_peak_concurrency: 1, speedup_multiplier: 3,
    speedup_kind: "measured_serial_baseline", savings_eligible: true,
    time_saved_seconds: 2.2, cumulative_saved_seconds: 5.5,
    ledger_credit_eligible: true, ledger_credit_recorded: true,
    cumulative_estimated_saved_seconds: 0,
    cumulative_ledger_available: true,
  },
  summary: {
    task_count: 1, elapsed_seconds: 1.8,
    status_counts: { succeeded: 1 }, peak_concurrency: 1,
  },
  test_report: { tests: 100, passed: 100, failures: 0, errors: 0, skipped: 0 },
} });
equal(element("state").textContent, "原生测试池执行完成", "final state");
equal(element("tasks").textContent, "100（JUnit）", "JUnit count");
equal(element("result").textContent, "100 通过", "final result");
equal(element("current-saved").textContent, "2.20 秒", "final savings");
equal(element("cumulative-saved").textContent, "5.50 秒", "final ledger");
equal(element("cumulative-estimated").textContent, "0.00 秒", "final estimates");
if (!element("note").textContent.includes("本次实测已入账")) throw new Error("missing measured provenance");
emit("atomlane:tool-result", { detail: {
  indicator: {
    parallel: true, parallelism_kind: "native_worker_pool",
    native_workers_configured: 4, native_workers_observed: null,
    outer_peak_concurrency: 1, speedup_multiplier: 2.5,
    speedup_kind: "estimated_sum_of_testcase_durations", savings_eligible: true,
    time_saved_seconds: 1.5, estimated_time_saved_seconds: 1.5,
    ledger_credit_eligible: false, ledger_credit_recorded: false,
    credited_time_saved_seconds: 0,
    cumulative_saved_seconds: 5.5, cumulative_estimated_saved_seconds: 7,
    cumulative_ledger_available: true,
  },
  summary: {
    task_count: 1, elapsed_seconds: 2,
    status_counts: { succeeded: 1 }, peak_concurrency: 1,
  },
  test_report: { tests: 100, passed: 100, failures: 0, errors: 0, skipped: 0 },
} });
equal(element("qualifier").textContent, "JUnit 估算", "estimated qualifier");
equal(element("current-saved").textContent, "1.50 秒", "estimated savings");
equal(element("cumulative-saved").textContent, "5.50 秒", "credited ledger unchanged");
equal(element("cumulative-estimated").textContent, "7.00 秒", "estimated ledger");
if (!element("note").textContent.includes("未计入累计已入账")) throw new Error("missing estimate provenance");
if (!element("note").textContent.includes("累计估算 7.00 秒")) throw new Error("missing estimate ledger");
emit("atomlane:tool-result", { detail: {
  indicator: {
    parallel: true, parallelism_kind: "native_worker_pool",
    native_workers_configured: 4, native_workers_observed: null,
    outer_peak_concurrency: 1, speedup_multiplier: 3,
    speedup_kind: "measured_serial_baseline", savings_eligible: true,
    time_saved_seconds: 2.2, measured_time_saved_seconds: 2.2,
    ledger_credit_eligible: true, ledger_credit_recorded: false,
    credited_time_saved_seconds: 0, cumulative_saved_seconds: null,
    cumulative_ledger_available: false,
  },
  summary: {
    task_count: 1, elapsed_seconds: 1.8,
    status_counts: { succeeded: 1 }, peak_concurrency: 1,
  },
  test_report: { tests: 100, passed: 100, failures: 0, errors: 0, skipped: 0 },
} });
equal(element("cumulative-saved").textContent, "不可用", "failed ledger");
equal(element("cumulative-estimated").textContent, "不可用", "failed estimates");
if (!element("note").textContent.includes("累计账本写入失败")) throw new Error("missing ledger failure");
emit("atomlane:tool-result", { detail: {
  indicator: {
    parallel: true, parallelism_kind: "native_worker_pool",
    native_workers_configured: 4, outer_peak_concurrency: 1,
    speedup_multiplier: 3, speedup_kind: "measured_serial_baseline",
    savings_eligible: true, time_saved_seconds: 2,
    cumulative_saved_seconds: 4, cumulative_ledger_available: true,
  },
  summary: { task_count: 1, elapsed_seconds: 1, status_counts: { succeeded: 1 } },
  test_report: { tests: 1, passed: 1, failures: 0, errors: 0, skipped: 0 },
} });
if (!element("note").textContent.includes("本次实测已入账")) throw new Error("old measured mislabeled");
emit("atomlane:tool-result", { detail: {
  indicator: {
    parallel: true, parallelism_kind: "native_worker_pool",
    native_workers_configured: 4, outer_peak_concurrency: 1,
    speedup_multiplier: 2, speedup_kind: "estimated_sum_of_testcase_durations",
    savings_eligible: true, time_saved_seconds: 1,
    cumulative_saved_seconds: 5, cumulative_ledger_available: true,
  },
  summary: { task_count: 1, elapsed_seconds: 1, status_counts: { succeeded: 1 } },
  test_report: { tests: 1, passed: 1, failures: 0, errors: 0, skipped: 0 },
} });
if (!element("note").textContent.includes("旧版 JUnit 估算，证据未分类（已按旧口径入账）")) throw new Error("old estimate mislabeled");
if (element("note").textContent.includes("本次实测已入账")) throw new Error("old estimate claimed measured");
"""
        )
        completed = subprocess.run(
            [shutil.which("node") or "node", "--input-type=module", "-"],
            input=harness,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )


if __name__ == "__main__":
    unittest.main()
