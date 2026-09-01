#!/usr/bin/env python3
"""Regression tests for the bounded Python parallelization advisor."""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mcp_server
from python_parallel_advisor import (
    MAX_PARSE_TOKENS,
    MAX_SOURCE_BYTES,
    AdvisorError,
    analyze_python_parallelism,
)

SAFE_PROGRAM = """\
def square(value):
    return value * value

def main():
    values = list(range(100))
    results = [square(value) for value in values]
    return results

if __name__ == "__main__":
    main()
"""


class PythonParallelAdvisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atomlane-python-advisor-")
        self.project = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def analyze(self, **overrides):
        arguments = {
            "project_path": self.project,
            "paths": ["job.py"],
            "max_workers": 4,
        }
        arguments.update(overrides)
        return analyze_python_parallelism(**arguments)

    def apply_diff(self, diff: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["git", "apply", "--unsafe-paths", "--whitespace=nowarn", "-"],
            cwd=self.project,
            input=diff.encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=15,
        )

    def test_pure_ordered_map_emits_hash_bound_process_preview(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        result = self.analyze()
        self.assertFalse(result["execution_performed"])
        self.assertFalse(result["files_modified"])
        self.assertEqual(result["summary"]["candidate_count"], 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["classification"], "reviewable_rewrite")
        self.assertEqual(candidate["recommended_executor"], "process_pool")
        self.assertEqual(candidate["proof_level"], "bounded_static_candidate")
        runtime_proof = next(
            item for item in candidate["proof_obligations"] if item["id"] == "runtime_protocols_and_pickling"
        )
        self.assertEqual(runtime_proof["status"], "unknown")
        preview = candidate["rewrite_preview"]
        self.assertEqual(preview["source_sha256"], result["snapshots"][0]["sha256"])
        self.assertFalse(preview["applies_automatically"])
        self.assertIn("ProcessPoolExecutor", preview["unified_diff"])
        self.assertIn(".map(square, values)", preview["unified_diff"])

    def test_windows_target_caps_process_rewrite_at_platform_limit(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        result = self.analyze(max_workers=64, target_platform="windows")
        self.assertEqual(result["options"]["effective_max_workers"], 61)
        self.assertIn("max_workers=61", result["candidates"][0]["rewrite_preview"]["unified_diff"])
        self.assertIn(
            "WINDOWS_PROCESS_POOL_LIMIT",
            {item["code"] for item in result["diagnostics"]},
        )

        mcp_result = mcp_server.python_parallel_advisor(
            {
                "project_path": str(self.project.resolve()),
                "paths": ["job.py"],
                "max_workers": 64,
                "target_platform": "windows",
            }
        )
        self.assertLessEqual(mcp_result["resource_plan"]["chosen_concurrency"], 61)
        self.assertEqual(mcp_result["resource_plan"]["target_worker_ceiling"], 61)

    def test_append_loop_preserves_order_with_executor_map(self) -> None:
        self.write(
            "job.py",
            "def square(value):\n"
            "    return value * value\n\n"
            "def main():\n"
            "    values = range(10)\n"
            "    results = []\n"
            "    for value in values:\n"
            "        results.append(square(value))\n"
            "    return results\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["pattern"], "ordered_append_map_loop")
        self.assertIn("results.extend", candidate["rewrite_preview"]["unified_diff"])

    def test_rewrite_uses_collision_resistant_executor_and_pool_names(self) -> None:
        self.write(
            "job.py",
            "_AtomLaneProcessPoolExecutor = None\n\n"
            "def square(value):\n    return value * value\n\n"
            "def main():\n"
            "    _atomlane_pool = 'preserve me'\n"
            "    results = [square(value) for value in range(10)]\n"
            "    return _atomlane_pool, results\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        diff = self.analyze()["candidates"][0]["rewrite_preview"]["unified_diff"]
        self.assertIn("ProcessPoolExecutor as _AtomLaneProcessPoolExecutor2", diff)
        self.assertIn(
            'with _AtomLaneProcessPoolExecutor2(max_workers=4, '
            'mp_context=_atomlane_multiprocessing.get_context("spawn")) as _atomlane_pool2',
            diff,
        )

    def test_spawn_fixture_preserves_order_and_values(self) -> None:
        serial = self.write(
            "serial_fixture.py",
            "import json\n\ndef square(value):\n    return value * value\n\n"
            "if __name__ == '__main__':\n"
            "    print(json.dumps([square(value) for value in range(12)]))\n",
        )
        parallel = self.write(
            "parallel_fixture.py",
            "import json\nimport multiprocessing\n"
            "from concurrent.futures import ProcessPoolExecutor\n\n"
            "def square(value):\n    return value * value\n\n"
            "if __name__ == '__main__':\n"
            "    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn')) as pool:\n"
            "        print(json.dumps(list(pool.map(square, range(12)))))\n",
        )
        outputs = []
        for path in (serial, parallel):
            completed = subprocess.run(
                [sys.executable, str(path)],
                cwd=self.project,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            outputs.append(completed.stdout)
        self.assertEqual(outputs[0], outputs[1])

    def test_emitted_diff_applies_and_matches_serial_execution(self) -> None:
        target = self.write("job.py", SAFE_PROGRAM)
        driver = self.write(
            "driver.py",
            "import job\n\n"
            "if __name__ == '__main__':\n"
            "    print(job.main())\n",
        )
        serial = subprocess.run(
            [sys.executable, str(driver)],
            cwd=self.project,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(serial.returncode, 0, serial.stderr)

        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "reviewable_rewrite")
        diff = candidate["rewrite_preview"]["unified_diff"]
        applied = self.apply_diff(diff)
        self.assertEqual(
            applied.returncode,
            0,
            applied.stderr.decode("utf-8", errors="replace"),
        )
        compile(target.read_text(encoding="utf-8"), str(target), "exec")

        parallel = subprocess.run(
            [sys.executable, str(driver)],
            cwd=self.project,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(parallel.returncode, 0, parallel.stderr)
        self.assertEqual(parallel.stdout, serial.stdout)

    def test_emitted_diff_applies_to_crlf_source_as_exact_utf8_bytes(self) -> None:
        target = self.project / "job.py"
        target.write_bytes(SAFE_PROGRAM.replace("\n", "\r\n").encode("utf-8"))
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "reviewable_rewrite")
        diff = candidate["rewrite_preview"]["unified_diff"]
        self.assertIn("\r\n", diff)

        applied = self.apply_diff(diff)
        self.assertEqual(
            applied.returncode,
            0,
            applied.stderr.decode("utf-8", errors="replace"),
        )
        patched = target.read_bytes()
        self.assertIn(b"\r\n", patched)
        self.assertNotIn(b"\r\r\n", patched)
        compile(patched.decode("utf-8"), str(target), "exec")

    def test_missing_main_guard_is_conditional_and_has_no_patch(self) -> None:
        self.write(
            "job.py",
            "def square(value):\n    return value * value\n\n"
            "def run(values):\n    return [square(value) for value in values]\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertNotIn("rewrite_preview", candidate)
        self.assertIn("PROCESS_POOL_REQUIRES_MAIN_GUARD", {item["code"] for item in candidate["blockers"]})

    def test_dunder_name_rebinding_cannot_fake_spawn_guard(self) -> None:
        self.write(
            "job.py",
            "__name__ = '__main__'\n\n"
            "def square(value):\n    return value * value\n\n"
            "def main():\n    return [square(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        codes = {item["code"] for item in candidate["blockers"]}
        self.assertIn("PROCESS_POOL_REQUIRES_MAIN_GUARD", codes)
        self.assertIn("MODULE_IMPORT_EFFECTS", codes)

    def test_global_write_fails_closed(self) -> None:
        self.write(
            "job.py",
            "total = 0\n\n"
            "def worker(value):\n"
            "    global total\n"
            "    total += value\n"
            "    return total\n\n"
            "def main():\n"
            "    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("global_write", candidate["effects"])
        self.assertNotIn("rewrite_preview", candidate)

    def test_module_global_read_fails_closed_for_spawn_semantics(self) -> None:
        self.write(
            "job.py",
            "offset = {'value': 1}\n\n"
            "def worker(value):\n    return value + offset['value']\n\n"
            "def main():\n    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("module_global_read", candidate["effects"])

    def test_unknown_call_fails_closed(self) -> None:
        self.write(
            "job.py",
            "def worker(value):\n    return mystery(value)\n\n"
            "def main():\n    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("unknown_call", candidate["effects"])

    def test_unknown_receiver_method_fails_closed(self) -> None:
        self.write(
            "job.py",
            "def worker(value):\n    return value.lower()\n\n"
            "def main():\n    return [worker(value) for value in ['A', 'B']]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("unknown_call", candidate["effects"])

    def test_shadowed_builtin_is_not_treated_as_pure(self) -> None:
        self.write(
            "job.py",
            "len = sum\n\n"
            "def worker(value):\n    return len([value])\n\n"
            "def main():\n    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("module_global_read", candidate["effects"])
        self.assertIn("unknown_call", candidate["effects"])

    def test_lazy_or_unresolved_iterable_blocks_cpu_rewrite(self) -> None:
        self.write(
            "job.py",
            "def square(value):\n    return value * value\n\n"
            "def main(values):\n    return [square(value) for value in values]\n\n"
            "if __name__ == '__main__':\n    main(iter([1, 2]))\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("ITERABLE_SEMANTICS_UNPROVEN", {item["code"] for item in candidate["blockers"]})

    def test_iterable_proof_rejects_intervening_control_flow_rebind(self) -> None:
        self.write(
            "job.py",
            "def values_from_generator():\n"
            "    print('yield 0')\n"
            "    yield 0\n"
            "    print('yield 1')\n"
            "    yield 1\n\n"
            "def worker(value):\n    return 1 // value\n\n"
            "def main():\n"
            "    values = range(2)\n"
            "    if True:\n"
            "        values = values_from_generator()\n"
            "    return [worker(value) for value in values]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
        self.assertNotIn("rewrite_preview", candidate)

    def test_iterable_proof_rejects_shadowed_materializer_builtin(self) -> None:
        self.write(
            "job.py",
            "def range(count):\n"
            "    print('yield 0')\n"
            "    yield 0\n"
            "    print('yield 1')\n"
            "    yield 1\n\n"
            "def worker(value):\n    return 1 // value\n\n"
            "def main():\n    return [worker(value) for value in range(2)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
        self.assertNotIn("rewrite_preview", candidate)

    def test_environment_read_fails_closed(self) -> None:
        self.write(
            "job.py",
            "import os\n\ndef worker(value):\n    return os.environ['TOKEN'] + str(value)\n\n"
            "def main(values):\n    return [worker(value) for value in values]\n\n"
            "if __name__ == '__main__':\n    main([])\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("environment_read", candidate["effects"])

    def test_file_read_is_advisory_only(self) -> None:
        self.write(
            "job.py",
            "from pathlib import Path\n\n"
            "def read_one(path):\n    return Path(path).read_text()\n\n"
            "def main(paths):\n    return [read_one(path) for path in paths]\n\n"
            "if __name__ == '__main__':\n    main([])\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "advisory_only")
        self.assertEqual(candidate["workload"], "blocking_read_io")
        self.assertEqual(candidate["recommended_executor"], "thread_pool")
        self.assertNotIn("rewrite_preview", candidate)

    def test_network_and_subprocess_are_advice_not_patches(self) -> None:
        programs = {
            "network": (
                "import requests\n\ndef worker(value):\n    return requests.get(value)\n\n"
                "def main(values):\n    return [worker(value) for value in values]\n\n"
                "if __name__ == '__main__':\n    main([])\n"
            ),
            "subprocess": (
                "import subprocess\n\ndef worker(value):\n    return subprocess.run(value)\n\n"
                "def main(values):\n    return [worker(value) for value in values]\n\n"
                "if __name__ == '__main__':\n    main([])\n"
            ),
        }
        for expected, source in programs.items():
            with self.subTest(expected=expected):
                self.write("job.py", source)
                candidate = self.analyze()["candidates"][0]
                self.assertEqual(candidate["classification"], "advisory_only")
                self.assertNotIn("rewrite_preview", candidate)

    def test_native_or_existing_parallelism_avoids_nested_pool(self) -> None:
        self.write(
            "job.py",
            "import concurrent.futures\nimport numpy as np\n\n"
            "def worker(value):\n    return np.sin(value)\n\n"
            "def main(values):\n    return [worker(value) for value in values]\n\n"
            "if __name__ == '__main__':\n    main([])\n",
        )
        result = self.analyze()
        self.assertTrue(result["existing_parallelism"])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["classification"], "already_parallel")
        self.assertIn("EXISTING_PARALLELISM", {item["code"] for item in candidate["blockers"]})

    def test_outer_parallel_context_blocks_nested_rewrite(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        candidate = self.analyze(execution_context="atomlane_worker")["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("NESTED_PARALLEL_BUDGET_UNPROVEN", {item["code"] for item in candidate["blockers"]})

    def test_top_level_import_side_effect_blocks_process_rewrite_without_executing(self) -> None:
        marker = self.project / "must-not-exist"
        self.write(
            "job.py",
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n\n"
            "def square(value):\n    return value * value\n\n"
            "def main():\n    return [square(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        before = (self.project / "job.py").read_bytes()
        candidate = self.analyze()["candidates"][0]
        after = (self.project / "job.py").read_bytes()
        self.assertFalse(marker.exists())
        self.assertEqual(before, after)
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("MODULE_IMPORT_EFFECTS", {item["code"] for item in candidate["blockers"]})

    def test_import_requires_spawn_safety_review_without_importing_target(self) -> None:
        self.write(
            "job.py",
            "import package_that_does_not_exist\n\n"
            "def square(value):\n    return value * value\n\n"
            "def main():\n    return [square(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("MODULE_IMPORT_EFFECTS", {item["code"] for item in candidate["blockers"]})

    def test_definition_time_effects_block_spawn_preview(self) -> None:
        programs = {
            "decorator": "@decorate\ndef helper():\n    pass\n",
            "default": "def helper(value=build_default()):\n    return value\n",
            "annotation": "def helper(value: build_annotation()):\n    return value\n",
        }
        suffix = (
            "\ndef square(value):\n    return value * value\n\n"
            "def main():\n    return [square(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n"
        )
        for label, prefix in programs.items():
            with self.subTest(label=label):
                self.write("job.py", prefix + suffix)
                candidate = self.analyze()["candidates"][0]
                self.assertEqual(candidate["classification"], "blocked")
                self.assertIn("MODULE_IMPORT_EFFECTS", {item["code"] for item in candidate["blockers"]})

    def test_preview_refuses_shared_physical_source_lines(self) -> None:
        programs = {
            "single_line_suite": (
                "def square(value):\n    return value * value\n\n"
                "def main(flag):\n"
                "    if flag: results = [square(value) for value in range(10)]\n"
                "    return results\n\n"
                "if __name__ == '__main__':\n    main(True)\n"
            ),
            "semicolon": (
                "def square(value):\n    return value * value\n\n"
                "def main():\n"
                "    results = [square(value) for value in range(10)]; marker = 'preserve'\n"
                "    return marker, results\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
        }
        for label, source in programs.items():
            with self.subTest(label=label):
                self.write("job.py", source)
                candidate = self.analyze()["candidates"][0]
                self.assertEqual(candidate["classification"], "blocked")
                self.assertNotIn("rewrite_preview", candidate)
                self.assertIn(
                    "REWRITE_REQUIRES_STANDALONE_STATEMENT",
                    {item["code"] for item in candidate["blockers"]},
                )

    def test_measured_hotspot_is_distinguished_from_modeled_projection(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        line = 6
        candidate = self.analyze(
            hotspots=[{"path": "job.py", "line": line, "wall_seconds": 120.0, "item_count": 100}]
        )["candidates"][0]
        benefit = candidate["benefit"]
        self.assertEqual(benefit["kind"], "measured_serial_modeled_parallel")
        self.assertGreater(benefit["projected_time_saved_seconds"], 0)
        self.assertIn("not a benchmark", benefit["warning"])

    def test_no_runtime_evidence_never_invents_speedup(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        benefit = self.analyze()["candidates"][0]["benefit"]
        self.assertEqual(benefit["kind"], "not_estimated")
        self.assertNotIn("projected_speedup", benefit)

    def test_invalid_hotspot_numbers_are_rejected(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        for value in (0, -1, math.inf, math.nan):
            with self.subTest(value=value), self.assertRaises(AdvisorError):
                self.analyze(hotspots=[{"path": "job.py", "line": 6, "wall_seconds": value}])

    def test_boolean_numeric_limits_are_rejected(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        for name in ("max_files", "max_candidates", "max_workers", "minimum_hotspot_seconds"):
            with self.subTest(name=name), self.assertRaises(AdvisorError):
                self.analyze(**{name: True})

    def test_single_item_is_not_a_rewrite_candidate(self) -> None:
        self.write(
            "job.py",
            "def square(value):\n    return value * value\n\n"
            "def main():\n    return [square(value) for value in [1]]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertIn("INSUFFICIENT_ITEMS", {item["code"] for item in candidate["blockers"]})

    def test_syntax_utf8_and_size_failures_are_structured(self) -> None:
        path = self.project / "job.py"
        cases = [
            (b"def broken(:\n", "PYTHON_SOURCE_PARSE_FAILED"),
            (b"\xff\xfe", "PYTHON_SOURCE_NOT_UTF8"),
            (b"#" * (MAX_SOURCE_BYTES + 1), "PYTHON_SOURCE_TOO_LARGE"),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                path.write_bytes(payload)
                result = self.analyze()
                self.assertEqual(result["summary"]["files_analyzed"], 0)
                self.assertIn(code, {item["code"] for item in result["diagnostics"]})

    def test_oversized_source_is_rejected_before_an_unbounded_read(self) -> None:
        path = self.project / "job.py"
        with path.open("wb") as stream:
            stream.truncate(MAX_SOURCE_BYTES + 1)
        target = path.resolve()
        original_read_bytes = Path.read_bytes

        def bounded_read(candidate: Path) -> bytes:
            if candidate == target:
                raise AssertionError("oversized source must be rejected before read_bytes")
            return original_read_bytes(candidate)

        with mock.patch.object(Path, "read_bytes", bounded_read):
            result = self.analyze()
        self.assertEqual(result["summary"]["files_analyzed"], 0)
        self.assertIn(
            "PYTHON_SOURCE_TOO_LARGE",
            {item["code"] for item in result["diagnostics"]},
        )

    def test_dense_valid_source_is_rejected_before_ast_construction(self) -> None:
        repetitions = MAX_PARSE_TOKENS // 4 + 2_000
        self.write("job.py", "x=0\n" * repetitions)
        with mock.patch(
            "python_parallel_advisor.ast.parse",
            side_effect=AssertionError("preflight must reject before AST construction"),
        ):
            result = self.analyze()
        self.assertEqual(result["summary"]["files_analyzed"], 0)
        self.assertIn(
            "PYTHON_PARSE_BUDGET_EXCEEDED",
            {item["code"] for item in result["diagnostics"]},
        )

    def test_path_escape_and_symlink_escape_are_rejected(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        with tempfile.TemporaryDirectory(prefix="atomlane-outside-") as outside_text:
            outside = Path(outside_text) / "outside.py"
            outside.write_text(SAFE_PROGRAM, encoding="utf-8")
            with self.assertRaises(AdvisorError):
                analyze_python_parallelism(self.project, paths=[str(outside)])
            link = self.project / "escape.py"
            os.symlink(outside, link)
            with self.assertRaises(AdvisorError):
                analyze_python_parallelism(self.project, paths=["escape.py"])

    def test_missing_project_root_is_reported_as_advisor_error(self) -> None:
        missing = self.project / "missing"
        with self.assertRaises(AdvisorError):
            analyze_python_parallelism(missing)

    def test_output_is_deterministic_and_source_hash_bound(self) -> None:
        path = self.write("job.py", SAFE_PROGRAM)
        first = self.analyze()
        second = self.analyze()
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )
        first_id = first["candidates"][0]["id"]
        first_hash = first["analysis_hash"]
        path.write_text(SAFE_PROGRAM.replace("value * value", "value * value + 1"), encoding="utf-8")
        changed = self.analyze()
        self.assertNotEqual(first_id, changed["candidates"][0]["id"])
        self.assertNotEqual(first_hash, changed["analysis_hash"])

    def test_discovery_skips_virtualenv_and_is_sorted(self) -> None:
        self.write("b.py", SAFE_PROGRAM)
        self.write("a.py", SAFE_PROGRAM)
        self.write(".venv/ignored.py", SAFE_PROGRAM)
        result = analyze_python_parallelism(self.project, max_workers=4)
        self.assertEqual([item["path"] for item in result["snapshots"]], ["a.py", "b.py"])

    def test_worker_and_main_rebindings_block_rewrite(self) -> None:
        programs = {
            "local_worker": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    print(value)\n    return value\n\n"
                "def main():\n"
                "    worker = evil\n"
                "    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "worker_parameter": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    print(value)\n    return value\n\n"
                "def main(worker):\n"
                "    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main(evil)\n"
            ),
            "module_worker": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    print(value)\n    return value\n\n"
                "worker = evil\n\n"
                "def main():\n    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "guard_worker": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    print(value)\n    return value\n\n"
                "def main():\n    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n"
                "    worker = evil\n"
                "    main()\n"
            ),
            "dynamic_worker": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    print(value)\n    return value\n\n"
                "def main():\n"
                "    globals()['worker'] = evil\n"
                "    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "module_main": (
                "def worker(value):\n    return value * value\n\n"
                "def main():\n    return [worker(value) for value in range(10)]\n\n"
                "def other():\n    return None\n\n"
                "main = other\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
        }
        for label, source in programs.items():
            with self.subTest(label=label):
                self.write("job.py", source)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_transitive_helper_rebindings_block_rewrite(self) -> None:
        programs = {
            "local_helper": (
                "def helper(value):\n    return value * value\n\n"
                "def worker(value):\n"
                "    helper = print\n"
                "    return helper(value)\n\n"
            ),
            "module_helper": (
                "def helper(value):\n    return value * value\n\n"
                "def evil(value):\n    print(value)\n    return value\n\n"
                "helper = evil\n\n"
                "def worker(value):\n    return helper(value)\n\n"
            ),
            "comprehension_walrus": (
                "def helper(value):\n    return value * value\n\n"
                "def worker(value):\n"
                "    [(helper := print) for _ in [0]]\n"
                "    return helper(value)\n\n"
            ),
            "container_global_helper": (
                (
                    "def helper(value):\n    return value * value\n\n"
                    "def evil(value):\n    print(value)\n    return value\n\n"
                    "def worker(value):\n    return helper(value)\n\n"
                    "def main():\n"
                    "    global helper\n"
                    "    helper = evil\n"
                    "    return [worker(value) for value in range(10)]\n\n"
                    "if __name__ == '__main__':\n    main()\n"
                ),
                None,
            ),
            "guard_builtin": (
                (
                    "def evil(value):\n    print(value)\n    return value\n\n"
                    "def worker(value):\n    return abs(value)\n\n"
                    "def main():\n    return [worker(value) for value in range(10)]\n\n"
                    "if __name__ == '__main__':\n"
                    "    abs = evil\n"
                    "    main()\n"
                ),
                None,
            ),
            "pure_module_name_shadow": (
                (
                    "def math():\n    return None\n\n"
                    "def evil(value):\n    print(value)\n    return value\n\n"
                    "def worker(value):\n    return math.sqrt(value)\n\n"
                    "def main():\n"
                    "    math.sqrt = evil\n"
                    "    return [worker(value) for value in range(10)]\n\n"
                    "if __name__ == '__main__':\n    main()\n"
                ),
                None,
            ),
        }
        suffix = (
            "def main():\n    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n"
        )
        for label, program in programs.items():
            with self.subTest(label=label):
                if isinstance(program, tuple):
                    source = program[0]
                else:
                    source = program + suffix
                self.write("job.py", source)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_main_guard_proof_rejects_deferred_else_async_and_generator_calls(self) -> None:
        programs = {
            "lambda": (
                "def main():\n    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    callback = lambda: main()\n"
            ),
            "else": (
                "def main():\n    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\nelse:\n    main()\n"
            ),
            "async": (
                "async def main():\n    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "generator": (
                "def main():\n"
                "    results = [worker(value) for value in range(10)]\n"
                "    yield results\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
        }
        worker = "def worker(value):\n    return value * value\n\n"
        for label, body in programs.items():
            with self.subTest(label=label):
                self.write("job.py", worker + body)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_sync_worker_calling_async_helper_is_not_process_rewriteable(self) -> None:
        self.write(
            "job.py",
            "async def helper(value):\n    return value * value\n\n"
            "def worker(value):\n    return helper(value)\n\n"
            "def main():\n    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
        self.assertNotIn("rewrite_preview", candidate)

    def test_rewrite_aliases_do_not_collide_with_function_parameters(self) -> None:
        self.write(
            "job.py",
            "def worker(value):\n    return value * value\n\n"
            "def main(_AtomLaneProcessPoolExecutor, _atomlane_pool):\n"
            "    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main(None, None)\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "reviewable_rewrite")
        diff = candidate["rewrite_preview"]["unified_diff"]
        self.assertIn("ProcessPoolExecutor as _AtomLaneProcessPoolExecutor2", diff)
        self.assertIn(" as _atomlane_pool2:", diff)

    def test_rewrite_preserves_shebang_and_encoding_cookie_before_import(self) -> None:
        self.write(
            "job.py",
            "#!/usr/bin/env python3\n"
            "# -*- coding: utf-8 -*-\n"
            "def worker(value):\n    return value * value\n\n"
            "def main():\n    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertEqual(candidate["classification"], "reviewable_rewrite")
        diff = candidate["rewrite_preview"]["unified_diff"]
        import_text = "from concurrent.futures import ProcessPoolExecutor"
        self.assertLess(diff.index("#!/usr/bin/env python3"), diff.index(import_text))
        self.assertLess(diff.index("# -*- coding: utf-8 -*-"), diff.index(import_text))

    def test_one_worker_and_low_benefit_never_emit_process_rewrite(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        one_worker = self.analyze(max_workers=1)["candidates"][0]
        self.assertNotEqual(one_worker["classification"], "reviewable_rewrite")
        self.assertEqual(one_worker["benefit"]["kind"], "not_applicable_until_safety")
        self.assertNotIn("rewrite_preview", one_worker)

        low_benefit = self.analyze(
            hotspots=[{"path": "job.py", "line": 6, "wall_seconds": 0.01, "item_count": 100}],
            minimum_hotspot_seconds=10.0,
        )["candidates"][0]
        self.assertNotEqual(low_benefit["classification"], "reviewable_rewrite")
        self.assertNotIn("rewrite_preview", low_benefit)

    def test_append_loop_variable_live_out_blocks_rewrite(self) -> None:
        self.write(
            "job.py",
            "def worker(value):\n    return value * value\n\n"
            "def main():\n"
            "    results = []\n"
            "    for value in range(10):\n"
            "        results.append(worker(value))\n"
            "    return results, value\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
        self.assertNotIn("rewrite_preview", candidate)

    def test_append_loop_reaching_definition_rejects_destructuring_rebind(self) -> None:
        self.write(
            "job.py",
            "def worker(value):\n    return value * value\n\n"
            "def main():\n"
            "    class Custom:\n"
            "        def append(self, value):\n            print('append', value)\n"
            "        def extend(self, values):\n            print('extend', list(values))\n"
            "    results = []\n"
            "    (results,) = (Custom(),)\n"
            "    for value in range(10):\n"
            "        results.append(worker(value))\n"
            "    return results\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidates = self.analyze()["candidates"]
        self.assertFalse(
            any(
                candidate["classification"] == "reviewable_rewrite"
                or "rewrite_preview" in candidate
                for candidate in candidates
            )
        )

    def test_append_loop_external_or_reflective_target_binding_blocks_rewrite(self) -> None:
        programs = {
            "global_target": (
                "def main():\n"
                "    global value\n"
                "    results = []\n"
                "    for value in range(10):\n"
                "        results.append(worker(value))\n"
                "    return results\n"
            ),
            "locals_reflection": (
                "def main():\n"
                "    results = []\n"
                "    for value in range(10):\n"
                "        results.append(worker(value))\n"
                "    return results, locals()\n"
            ),
            "delete_target": (
                "def main():\n"
                "    results = []\n"
                "    for value in range(10):\n"
                "        results.append(worker(value))\n"
                "    del value\n"
                "    return results\n"
            ),
            "closure_defined_before_loop": (
                "def main():\n"
                "    def last_value():\n"
                "        return value\n"
                "    results = []\n"
                "    for value in range(10):\n"
                "        results.append(worker(value))\n"
                "    return results, last_value()\n"
            ),
        }
        prefix = "def worker(value):\n    return value * value\n\n"
        suffix = "\nif __name__ == '__main__':\n    main()\n"
        for label, body in programs.items():
            with self.subTest(label=label):
                self.write("job.py", prefix + body + suffix)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_free_name_worker_import_subscript_write_and_module_annotation_fail_closed(self) -> None:
        programs = {
            "free_name": (
                "def worker(value):\n    return value + missing\n\n",
                "unresolved_free_name",
            ),
            "worker_import": (
                "def worker(value):\n    import this\n    return value * value\n\n",
                "dynamic_import",
            ),
            "subscript_write": (
                (
                    "def worker(value):\n"
                    "    for value[0] in [2]:\n        pass\n"
                    "    return 1\n\n"
                ),
                "subscript_write",
            ),
            "module_annotation": (
                (
                    "sentinel: print('IMPORT EFFECT') = 1\n\n"
                    "def worker(value):\n    return value * value\n\n"
                ),
                None,
            ),
        }
        suffixes = {
            "subscript_write": (
                "def main():\n"
                "    values = [[0], [0], [0]]\n"
                "    return [worker(value) for value in values]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            )
        }
        default_suffix = (
            "def main():\n    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n"
        )
        for label, (prefix, expected_effect) in programs.items():
            with self.subTest(label=label):
                self.write("job.py", prefix + suffixes.get(label, default_suffix))
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)
                if expected_effect is not None:
                    self.assertIn(expected_effect, candidate["effects"])

    def test_spawn_import_rejects_guard_only_names_and_module_target_mutation(self) -> None:
        programs = {
            "guard_only_name": (
                "def worker(value):\n    return value * value\n\n"
                "def main():\n    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    READY = 1\n"
                "copied = READY\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "function_default_mutation": (
                "def worker(value, factor=1):\n    return value * factor\n\n"
                "def main():\n    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n"
                "worker.__defaults__ = (2,)\n"
            ),
            "helper_defined_after_guard": (
                "def worker(value):\n    return helper(value)\n\n"
                "def main():\n    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n\n"
                "def helper(value):\n    return value * value\n"
            ),
        }
        for label, source in programs.items():
            with self.subTest(label=label):
                self.write("job.py", source)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_callable_identity_and_guard_mutations_block_spawn_rewrite(self) -> None:
        programs = {
            "mutation_in_main": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    return value + 100\n\n"
                "def main():\n"
                "    worker.__code__ = evil.__code__\n"
                "    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "mutation_in_separate_guard": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    return value + 100\n\n"
                "def main():\n"
                "    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n"
                "    worker.__code__ = evil.__code__\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "module_candidate_mutation": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    return value + 100\n\n"
                "if __name__ == '__main__':\n"
                "    worker.__code__ = evil.__code__\n"
                "    results = [worker(value) for value in range(10)]\n"
            ),
            "transitive_configure_global_mutation": (
                "configured = 0\n\n"
                "def worker(value):\n    return value * value\n\n"
                "def configure():\n"
                "    global configured\n"
                "    configured = 1\n\n"
                "def main():\n"
                "    configure()\n"
                "    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "mutation_hidden_in_iterable_expression": (
                "def worker(value):\n    return value * value\n\n"
                "def evil(value):\n    return value + 100\n\n"
                "def configure():\n"
                "    worker.__code__ = evil.__code__\n"
                "    return 10\n\n"
                "def main():\n"
                "    return [worker(value) for value in range(configure())]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
        }
        for label, source in programs.items():
            with self.subTest(label=label):
                self.write("job.py", source)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_same_module_function_values_block_spawn_rewrite(self) -> None:
        programs = {
            "stable_function_identity": (
                "def helper(value):\n    return value\n\n"
                "def worker(value):\n    return repr(helper)\n\n"
                "def main():\n"
                "    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
            "function_value_defined_after_guard": (
                "def worker(value):\n    return repr(helper)\n\n"
                "def main():\n"
                "    return [worker(value) for value in range(10)]\n\n"
                "if __name__ == '__main__':\n    main()\n\n"
                "def helper(value):\n    return value\n"
            ),
        }
        for label, source in programs.items():
            with self.subTest(label=label):
                self.write("job.py", source)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_explicit_termination_and_assertions_block_spawn_rewrite(self) -> None:
        workers = {
            "raise": "def worker(value):\n    raise ValueError(value)\n\n",
            "system_exit": "def worker(value):\n    raise SystemExit(value)\n\n",
            "assert": "def worker(value):\n    assert value >= 0\n    return value * value\n\n",
        }
        suffix = (
            "def main():\n"
            "    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n"
        )
        for label, worker_source in workers.items():
            with self.subTest(label=label):
                self.write("job.py", worker_source + suffix)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_process_specific_dynamic_values_block_spawn_rewrite(self) -> None:
        keys = ", ".join(repr(f"key{index}") for index in range(20))
        programs = {
            "module_identity_name": (
                "def worker(value):\n"
                "    return __name__\n\n"
            ),
            "nested_class_identity": (
                "def worker(value):\n"
                "    class LocalType:\n        pass\n"
                "    return repr(LocalType)\n\n"
            ),
            "generator_expression_identity": (
                "def worker(value):\n"
                "    return repr((value for _ in range(2)))\n\n"
            ),
            "unordered_hash_seed_iteration": (
                "def worker(value):\n"
                f"    for item in {{{keys}}}:\n"
                "        return item\n\n"
            ),
        }
        suffix = (
            "def main():\n"
            "    return [worker(value) for value in range(10)]\n\n"
            "if __name__ == '__main__':\n    main()\n"
        )
        for label, worker_source in programs.items():
            with self.subTest(label=label):
                self.write("job.py", worker_source + suffix)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_class_scope_and_repeated_pool_creation_are_not_rewriteable(self) -> None:
        programs = {
            "class_scope_changes_name_resolution": (
                "def worker(value):\n    return value * value\n\n"
                "if __name__ == '__main__':\n"
                "    class Example:\n"
                "        worker = abs\n"
                "        results = [worker(value) for value in range(-10, -1)]\n"
            ),
            "candidate_inside_outer_loop": (
                "def worker(value):\n    return value * value\n\n"
                "def main():\n"
                "    for batch in range(100):\n"
                "        results = [worker(value) for value in range(10)]\n"
                "    return results\n\n"
                "if __name__ == '__main__':\n    main()\n"
            ),
        }
        for label, source in programs.items():
            with self.subTest(label=label):
                self.write("job.py", source)
                candidate = self.analyze()["candidates"][0]
                self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
                self.assertNotIn("rewrite_preview", candidate)

    def test_append_loop_augmented_live_out_blocks_rewrite(self) -> None:
        self.write(
            "job.py",
            "def worker(value):\n    return value * value\n\n"
            "def main():\n"
            "    results = []\n"
            "    for value in range(10):\n"
            "        results.append(worker(value))\n"
            "    value += 1\n"
            "    return results\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        candidate = self.analyze()["candidates"][0]
        self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
        self.assertNotIn("rewrite_preview", candidate)

    def test_rewrite_does_not_discard_internal_comments(self) -> None:
        self.write(
            "job.py",
            "def worker(value):\n    return value * value\n\n"
            "def main():\n"
            "    results = [\n"
            "        worker(value)  # security-reviewed: preserve this\n"
            "        for value in range(10)\n"
            "    ]\n"
            "    return results\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        comment_candidate = self.analyze()["candidates"][0]
        self.assertNotEqual(comment_candidate["classification"], "reviewable_rewrite")
        self.assertNotIn("rewrite_preview", comment_candidate)

    def test_rewrite_does_not_emit_control_characters_in_diff_headers(self) -> None:
        hostile_path = "job\n--- injected.py"
        with self.assertRaisesRegex(AdvisorError, "control or line-separator"):
            analyze_python_parallelism(
                self.project,
                paths=[hostile_path],
                max_workers=4,
            )

    def test_missing_final_newline_never_produces_a_malformed_diff(self) -> None:
        source = (
            "def worker(value):\n    return value * value\n\n"
            "if __name__ == '__main__':\n"
            "    results = [worker(value) for value in range(10)]"
        )
        self.write("job.py", source)
        candidate = self.analyze()["candidates"][0]
        preview = candidate.get("rewrite_preview")
        if preview is None:
            self.assertNotEqual(candidate["classification"], "reviewable_rewrite")
            return
        diff = preview["unified_diff"]
        self.assertIn("\\ No newline at end of file", diff)
        self.assertNotRegex(diff, r"^-.*\]\+", "old and new diff records must not be concatenated")

    def test_source_read_does_not_follow_a_post_validation_symlink_swap(self) -> None:
        inside = self.write("job.py", SAFE_PROGRAM).resolve()
        with tempfile.TemporaryDirectory(prefix="atomlane-python-advisor-outside-") as outside_text:
            outside = Path(outside_text) / "outside.py"
            outside.write_text(
                SAFE_PROGRAM.replace("square", "OUTSIDE_SECRET_WORKER"),
                encoding="utf-8",
            )
            original_open = os.open
            swapped = False

            def racing_open(path: os.PathLike[str] | str, flags: int, *args, **kwargs) -> int:
                nonlocal swapped
                candidate = Path(path)
                if not swapped and candidate == inside:
                    swapped = True
                    candidate.unlink()
                    candidate.symlink_to(outside)
                return original_open(path, flags, *args, **kwargs)

            with mock.patch("python_parallel_advisor.os.open", racing_open):
                try:
                    result = self.analyze()
                except AdvisorError:
                    return
            self.assertNotIn("OUTSIDE_SECRET_WORKER", json.dumps(result, sort_keys=True))

    def test_candidate_limit_reports_only_actual_truncation(self) -> None:
        self.write(
            "job.py",
            "def worker(value):\n    return value * value\n\n"
            "def main():\n"
            "    first = [worker(value) for value in range(10)]\n"
            "    second = [worker(value) for value in range(10)]\n"
            "    return first, second\n\n"
            "if __name__ == '__main__':\n    main()\n",
        )
        exact = self.analyze(max_candidates=2)
        self.assertEqual(exact["summary"]["candidate_count"], 2)
        self.assertFalse(exact["summary"]["candidates_truncated"])
        self.assertNotIn(
            "PYTHON_CANDIDATE_LIMIT",
            {item["code"] for item in exact["diagnostics"]},
        )

        truncated = self.analyze(max_candidates=1)
        self.assertEqual(truncated["summary"]["candidate_count"], 1)
        self.assertTrue(truncated["summary"]["candidates_truncated"])
        self.assertIn(
            "PYTHON_CANDIDATE_LIMIT",
            {item["code"] for item in truncated["diagnostics"]},
        )

    def test_deep_ast_is_reported_without_crashing(self) -> None:
        expression = "+".join("1" for _ in range(1500))
        self.write("job.py", f"deep = {expression}\n")
        result = self.analyze()
        self.assertEqual(result["summary"]["files_analyzed"], 0)
        self.assertIn(
            result["diagnostics"][0]["code"],
            {"PYTHON_AST_DEPTH_EXCEEDED", "PYTHON_SOURCE_PARSE_FAILED"},
        )

    def test_mcp_schema_is_closed_and_explicitly_non_executing(self) -> None:
        tool = next(item for item in mcp_server.TOOLS if item["name"] == "python_parallel_advisor")
        self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertFalse(tool["inputSchema"]["properties"]["hotspots"]["items"]["additionalProperties"])
        self.assertIn("Never imports or executes", tool["description"])
        self.assertIn("never modifies files", tool["description"])

    def test_mcp_runtime_rejects_schema_bypasses(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        base = {"project_path": str(self.project), "paths": ["job.py"]}
        invalid_arguments = {
            "unknown_top_level": {**base, "unexpected": True},
            "string_preview_flag": {**base, "include_rewrite_previews": "false"},
            "boolean_workers": {**base, "max_workers": True},
            "string_memory": {**base, "estimated_memory_mb_per_worker": "1"},
            "nonfinite_memory": {**base, "estimated_memory_mb_per_worker": math.nan},
            "too_many_paths": {**base, "paths": ["job.py"] * 129},
            "empty_paths": {**base, "paths": []},
            "too_many_hotspots": {
                **base,
                "hotspots": [
                    {"path": "job.py", "line": 6, "wall_seconds": 10.0}
                ]
                * 129,
            },
            "unknown_hotspot_key": {
                **base,
                "hotspots": [
                    {
                        "path": "job.py",
                        "line": 6,
                        "wall_seconds": 10.0,
                        "unexpected": True,
                    }
                ],
            },
            "huge_minimum": {**base, "minimum_hotspot_seconds": 10**1000},
            "huge_hotspot_wall": {
                **base,
                "hotspots": [{"path": "job.py", "line": 6, "wall_seconds": 10**1000}],
            },
            "huge_item_count": {
                **base,
                "hotspots": [
                    {
                        "path": "job.py",
                        "line": 6,
                        "wall_seconds": 10.0,
                        "item_count": 10**1000,
                    }
                ],
            },
        }
        for label, arguments in invalid_arguments.items():
            with self.subTest(label=label), self.assertRaises(mcp_server.InputError):
                mcp_server.python_parallel_advisor(arguments)

    def test_core_rejects_non_boolean_preview_and_oversized_input_arrays(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        with self.assertRaises(AdvisorError):
            self.analyze(include_rewrite_previews="false")
        with self.assertRaises(AdvisorError):
            self.analyze(paths=["job.py"] * 129)
        with self.assertRaises(AdvisorError):
            self.analyze(
                hotspots=[{"path": "job.py", "line": 6, "wall_seconds": 10.0}] * 129
            )

    def test_mcp_dispatch_returns_matching_structured_and_text_content(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        response = asyncio.run(
            mcp_server.call_tool(
                "python_parallel_advisor",
                {
                    "project_path": str(self.project),
                    "paths": ["job.py"],
                    "max_workers": 2,
                },
            )
        )
        structured = response["structuredContent"]
        self.assertEqual(json.loads(response["content"][0]["text"]), structured)
        self.assertFalse(structured["advice_contract"]["target_code_executed"])
        self.assertFalse(structured["advice_contract"]["files_modified"])
        self.assertNotIn("_meta", response)

    def test_mcp_one_worker_resource_plan_withholds_process_benefit(self) -> None:
        self.write("job.py", SAFE_PROGRAM)
        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            return_value={"chosen_concurrency": 1},
        ):
            result = mcp_server.python_parallel_advisor(
                {
                    "project_path": str(self.project),
                    "paths": ["job.py"],
                    "max_workers": 4,
                    "hotspots": [
                        {
                            "path": "job.py",
                            "line": 6,
                            "wall_seconds": 30.0,
                            "item_count": 100,
                        }
                    ],
                }
            )
        candidate = result["candidates"][0]
        self.assertEqual(candidate["classification"], "blocked")
        self.assertEqual(candidate["benefit"]["kind"], "not_applicable_until_safety")
        self.assertNotIn("rewrite_preview", candidate)

    def test_mcp_rejects_non_absolute_project_path(self) -> None:
        with self.assertRaises(mcp_server.InputError):
            mcp_server.python_parallel_advisor({"project_path": "relative"})


if __name__ == "__main__":
    unittest.main()
