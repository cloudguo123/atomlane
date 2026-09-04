"""Regression coverage for AtomLane's native pytest worker-pool adapter."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import venv
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import atom_frontends
import mcp_server
from atom_engine import AtomError
from atom_frontends import compile_entrypoints


class PytestTestSuiteFrontendTests(unittest.TestCase):
    def _entrypoint(self, **overrides: object) -> dict[str, object]:
        entrypoint: dict[str, object] = {
            "adapter": "test_suite",
            "id": "unit-tests",
            "framework": "pytest",
            "runner_argv": [sys.executable, "-m", "pytest"],
            "arguments": [],
            "worker_count": 4,
            "distribution": "loadfile",
            "case_count_hint": 100,
            "estimated_memory_mb_per_worker": 64,
            "estimated_duration_seconds": 300,
            "timeout_seconds": 420,
            "effects_declared_complete": True,
            "independence_declared": True,
            "env": {"PYTEST_ADDOPTS": ""},
        }
        entrypoint.update(overrides)
        return entrypoint

    def test_python_module_runner_compiles_one_exact_native_pool(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-plan-") as temporary:
            project = Path(temporary).resolve()
            (project / "tests").mkdir()
            (project / "pyproject.toml").write_text(
                "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                atom_frontends.subprocess,
                "run",
                side_effect=AssertionError("test-suite compilation must not execute collection"),
            ):
                compiled = compile_entrypoints(
                    project,
                    [self._entrypoint(arguments=["tests"])],
                    target_os="posix",
                    native_worker_ceiling=8,
                )

            self.assertEqual(len(compiled["atoms"]), 1)
            atom = compiled["atoms"][0]
            argv = atom["operation"]["argv"]
            self.assertEqual(
                argv[:3],
                [os.path.abspath(sys.executable), "-m", "pytest"],
            )
            self.assertEqual(
                argv[3:9],
                ["-p", "xdist", "-n", "4", "--dist", "loadfile"],
            )
            self.assertIn("no:cacheprovider", argv)
            self.assertIn("-c", argv)
            self.assertIn(f"--confcutdir={project}", argv)
            self.assertNotIn("addopts=", argv)
            self.assertIn("--maxprocesses", argv)
            self.assertIn("--max-worker-restart", argv)
            self.assertTrue(any(item.startswith("--basetemp=") for item in argv))
            self.assertTrue(any(item.startswith("--junitxml=") for item in argv))
            self.assertEqual(argv[-1], "tests")
            self.assertEqual(atom["operation"]["timeout_seconds"], 420.0)
            self.assertEqual(
                atom["operation"]["internal_parallelism"],
                {"kind": "bounded", "tokens": 4},
            )
            self.assertEqual(
                {claim["resource"]: claim["units"] for claim in atom["claims"]},
                {"worker_slot": 1, "cpu_core": 4, "memory_mb": 256.0},
            )
            self.assertEqual(atom["assurance"]["effects"], "complete_declared")
            self.assertEqual(atom["assurance"]["blockers"], [])

            suite = compiled["test_suites"][0]
            self.assertEqual(suite["strategy"], "native_worker_pool")
            self.assertEqual(suite["configured_workers"], 4)
            self.assertEqual(suite["worker_evidence"], "configured_not_observed")
            self.assertEqual(suite["case_count_hint"], 100)
            self.assertFalse(suite["collection_execution_performed"])
            self.assertEqual(suite["native_dependency"], "pytest-xdist")
            self.assertEqual(compiled["native_delegates"][0]["configured_workers"], 4)
            self.assertIn(
                "PYTEST_XDIST_RUNTIME_REQUIRED",
                {diagnostic["code"] for diagnostic in compiled["diagnostics"]},
            )
            self.assertIn(
                "PYTEST_ADDOPTS_VALIDATED",
                {diagnostic["code"] for diagnostic in compiled["diagnostics"]},
            )
            self.assertEqual(
                {Path(snapshot["path"]).name for snapshot in compiled["snapshots"]},
                {"pyproject.toml"},
            )

    def test_auto_worker_count_respects_compiled_host_ceiling(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-auto-") as temporary:
            project = Path(temporary).resolve()
            compiled = compile_entrypoints(
                project,
                [self._entrypoint(worker_count="auto")],
                target_os="posix",
                native_worker_ceiling=3,
            )

        atom = compiled["atoms"][0]
        self.assertEqual(compiled["test_suites"][0]["configured_workers"], 3)
        self.assertEqual(
            compiled["test_suites"][0]["worker_count_source"],
            "adaptive_host_budget",
        )
        self.assertEqual(atom["operation"]["internal_parallelism"]["tokens"], 3)
        argv_pairs = [
            atom["operation"]["argv"][index:index + 2]
            for index in range(len(atom["operation"]["argv"]) - 1)
        ]
        self.assertIn(["-n", "3"], argv_pairs)

    def test_auto_worker_count_does_not_exceed_case_count_hint(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-auto-cases-") as temporary:
            project = Path(temporary).resolve()
            compiled = compile_entrypoints(
                project,
                [self._entrypoint(worker_count="auto", case_count_hint=2)],
                target_os="posix",
                native_worker_ceiling=8,
            )

        suite = compiled["test_suites"][0]
        self.assertEqual(suite["configured_workers"], 2)
        self.assertEqual(suite["worker_count_source"], "adaptive_host_budget_and_case_hint")

    def test_default_distribution_shards_independent_cases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-default-dist-") as temporary:
            project = Path(temporary).resolve()
            entrypoint = self._entrypoint()
            entrypoint.pop("distribution")
            compiled = compile_entrypoints(
                project,
                [entrypoint],
                target_os="posix",
                native_worker_ceiling=4,
            )

        suite = compiled["test_suites"][0]
        self.assertEqual(suite["distribution"], "worksteal")
        self.assertIn(
            ["--dist", "worksteal"],
            [
                compiled["atoms"][0]["operation"]["argv"][index:index + 2]
                for index in range(
                    len(compiled["atoms"][0]["operation"]["argv"]) - 1
                )
            ],
        )

    def test_python_optimization_runner_is_rejected_for_evidence_safety(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-optimized-") as temporary:
            project = Path(temporary).resolve()
            for switch in ("-O", "-OO"):
                with self.subTest(switch=switch), self.assertRaisesRegex(
                    AtomError, "exact pytest runner prefix"
                ):
                    compile_entrypoints(
                        project,
                        [
                            self._entrypoint(
                                runner_argv=[sys.executable, switch, "-m", "pytest"]
                            )
                        ],
                        target_os="posix",
                        native_worker_ceiling=4,
                    )

    def test_project_cannot_shadow_pytest_or_owned_xdist_plugin(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-shadow-") as temporary:
            project = Path(temporary).resolve()
            for module in ("pytest", "xdist"):
                candidate = project / f"{module}.py"
                candidate.write_text("raise RuntimeError('shadowed')\n", encoding="utf-8")
                with self.subTest(module=module), self.assertRaisesRegex(
                    AtomError, f"trusted {module} module"
                ):
                    compile_entrypoints(
                        project,
                        [self._entrypoint()],
                        target_os="posix",
                        native_worker_ceiling=4,
                    )
                candidate.unlink()

    def test_python_import_and_optimization_environment_is_cleared_and_bound(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-python-env-") as temporary:
            project = Path(temporary).resolve()
            with mock.patch.dict(os.environ, {"PYTHONOPTIMIZE": "2"}):
                compiled = compile_entrypoints(
                    project,
                    [self._entrypoint()],
                    target_os="posix",
                    native_worker_ceiling=4,
                )
            environment = compiled["atoms"][0]["operation"]["env"]
            self.assertEqual(environment["PYTHONPATH"], "")
            self.assertEqual(environment["PYTHONHOME"], "")
            self.assertEqual(environment["PYTHONOPTIMIZE"], "")
            for variable, value in (
                ("PYTHONPATH", str(project)),
                ("PYTHONHOME", str(project)),
                ("PYTHONOPTIMIZE", "1"),
            ):
                with self.subTest(variable=variable), self.assertRaisesRegex(
                    AtomError,
                    f"{variable} must be empty",
                ):
                    compile_entrypoints(
                        project,
                        [self._entrypoint(env={variable: value})],
                        target_os="posix",
                        native_worker_ceiling=4,
                    )

    def test_windows_pythonoptimize_keys_are_case_insensitive_and_unique(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-windows-env-") as temporary:
            project = Path(temporary).resolve()
            windows_runner = [sys.executable, "-m", "pytest"]
            cases = (
                (
                    {"pythonoptimize": "1"},
                    "PYTHONOPTIMIZE must be empty",
                ),
                (
                    {"PYTHONOPTIMIZE": "", "pythonoptimize": ""},
                    "duplicate PYTHONOPTIMIZE keys",
                ),
            )
            for environment, message in cases:
                with self.subTest(environment=environment), self.assertRaisesRegex(
                    AtomError,
                    message,
                ):
                    compile_entrypoints(
                        project,
                        [
                            self._entrypoint(
                                runner_argv=windows_runner,
                                env=environment,
                            )
                        ],
                        target_os="nt",
                        native_worker_ceiling=4,
                    )

    def test_runtime_rejects_hash_bound_pythonoptimize_tampering(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-optimize-tamper-") as temporary:
            project = Path(temporary).resolve()
            plan = mcp_server.test_suite_plan(
                {
                    "project_path": str(project),
                    "runner_argv": [sys.executable, "-m", "pytest"],
                    "arguments": [],
                    "worker_count": 1,
                    "effects_declared_complete": True,
                    "env": {"PYTEST_ADDOPTS": ""},
                    "max_concurrency": 1,
                }
            )
            changed = copy.deepcopy(plan)
            changed_environment = changed["atoms"][0]["operation"]["env"]
            changed_environment["PYTHONOPTIMIZE"] = "1"
            changed_contract = changed["test_suites"][0]["selection_contract"]
            changed_contract["env"] = copy.deepcopy(changed_environment)
            changed["test_suites"][0]["selection_fingerprint"] = (
                mcp_server._pytest_selection_fingerprint(changed_contract)
            )

            with self.assertRaisesRegex(
                mcp_server.InputError,
                "envelope was changed",
            ):
                mcp_server._verify_compiled_plan(
                    {"compiled_plan": changed, "plan_hash": plan["plan_hash"]}
                )
            with self.assertRaisesRegex(
                mcp_server.InputError,
                "selection contract is inconsistent",
            ):
                mcp_server._test_suite_execution_context(changed)

    def test_atomlane_owned_pytest_options_are_rejected_everywhere(self) -> None:
        cases = (
            {"arguments": ["-n", "2"]},
            {"arguments": ["--numprocesses=2"]},
            {"arguments": ["--maxprocesses", "2"]},
            {"arguments": ["--dist", "load"]},
            {"arguments": ["-d"]},
            {"arguments": ["-vd"]},
            {"arguments": ["-f"]},
            {"arguments": ["--looponfail"]},
            {"arguments": ["--lf"]},
            {"arguments": ["--stepwise"]},
            {"arguments": ["--trace"]},
            {"arguments": ["--setup-only"]},
            {"arguments": ["--setupplan"]},
            {"arguments": ["-V"]},
            {"arguments": ["-VV"]},
            {"arguments": ["-qca.ini"]},
            {"arguments": ["-qn0"]},
            {"arguments": ["-qpcacheprovider"]},
            {"arguments": ["-cother.ini"]},
            {"arguments": ["--config-file=other.ini"]},
            {"arguments": ["--rootdir", "."]},
            {"arguments": ["--tx=8*popen"]},
            {"arguments": ["@opts.txt"]},
            {"arguments": ["--override-ini", "addopts=-n 8"]},
            {"arguments": ["--basetemp=/tmp/caller-owned"]},
            {"arguments": ["--junit-xml=caller.xml"]},
            {"env": {"PYTEST_ADDOPTS": "-n 8"}},
            {"env": {"PYTEST_ADDOPTS": "@opts.txt"}},
            {"env": {"PYTEST_ADDOPTS": "\"-n\" 63 --"}},
            {"env": {"PYTEST_ADDOPTS": "-f"}},
            {"env": {"PYTEST_ADDOPTS": "--cache-clear"}},
            {"env": {"PYTEST_ADDOPTS": "-pno:xdist"}},
            {"env": {"PYTEST_PLUGINS": "xdist.plugin"}},
            {"env": {"PYTEST_PLUGINS": "cacheprovider"}},
        )
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-owned-") as temporary:
            project = Path(temporary).resolve()
            for override in cases:
                with self.subTest(override=override), self.assertRaisesRegex(
                    AtomError,
                    "AtomLane",
                ):
                    compile_entrypoints(
                        project,
                        [self._entrypoint(**override)],
                        target_os="posix",
                        native_worker_ceiling=8,
                    )

    def test_config_and_environment_addopts_are_preserved_and_hash_bound(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-addopts-") as temporary:
            project = Path(temporary).resolve()
            config = project / "pytest.ini"
            config.write_text("[pytest]\naddopts = -ra -q\n", encoding="utf-8")
            compiled = compile_entrypoints(
                project,
                [self._entrypoint(env={"PYTEST_ADDOPTS": "-s"})],
                target_os="posix",
                native_worker_ceiling=4,
            )

        atom = compiled["atoms"][0]
        self.assertNotIn("addopts=", atom["operation"]["argv"])
        self.assertEqual(atom["operation"]["env"]["PYTEST_ADDOPTS"], "-s")
        contract = compiled["test_suites"][0]["selection_contract"]
        self.assertEqual(contract["config_addopts"], ["-ra", "-q"])
        self.assertEqual(contract["environment_addopts"], ["-s"])
        self.assertEqual(contract["config_addopts_policy"], "preserved_validated")
        self.assertEqual(contract["config_path"], str(config))

    def test_plain_pyproject_fallback_is_hash_bound_and_runtime_valid(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="atomlane-pytest-pyproject-fallback-"
        ) as temporary:
            project = Path(temporary).resolve()
            config = project / "pyproject.toml"
            config.write_text(
                "[build-system]\nrequires = []\nbuild-backend = 'example.backend'\n",
                encoding="utf-8",
            )
            selected = project / "test_sample.py"
            selected.write_text("def test_sample(): pass\n", encoding="utf-8")
            plan = mcp_server.test_suite_plan(
                {
                    "project_path": str(project),
                    "runner_argv": [sys.executable, "-m", "pytest"],
                    "arguments": [str(selected)],
                    "worker_count": 1,
                    "effects_declared_complete": True,
                    "env": {"PYTEST_ADDOPTS": ""},
                    "max_concurrency": 1,
                }
            )

            contract = plan["test_suites"][0]["selection_contract"]
            self.assertEqual(contract["config_path"], str(config))
            self.assertEqual(contract["config_selection_kind"], "fallback_pyproject")
            self.assertEqual(contract["config_addopts"], [])
            self.assertFalse(contract["uses_bundled_empty_config"])
            self.assertIsNotNone(mcp_server._test_suite_execution_context(plan))

            inconsistent = copy.deepcopy(plan)
            inconsistent_contract = inconsistent["test_suites"][0][
                "selection_contract"
            ]
            inconsistent_contract["config_selection_kind"] = "pytest_config"
            inconsistent["test_suites"][0]["selection_fingerprint"] = (
                mcp_server._pytest_selection_fingerprint(inconsistent_contract)
            )
            with self.assertRaisesRegex(
                mcp_server.InputError,
                "addopts metadata is inconsistent",
            ):
                mcp_server._test_suite_execution_context(inconsistent)

            config.write_text(
                "[tool.pytest.ini_options]\naddopts = '-q'\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                mcp_server.InputError,
                "addopts metadata is inconsistent",
            ):
                mcp_server._test_suite_execution_context(plan)

    def test_non_table_tool_value_is_not_treated_as_pytest_configuration(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="atomlane-pytest-nontable-tool-"
        ) as temporary:
            project = Path(temporary).resolve()
            config = project / "pyproject.toml"
            config.write_text("tool = 'metadata'\n", encoding="utf-8")
            compiled = compile_entrypoints(
                project,
                [self._entrypoint()],
                target_os="posix",
                native_worker_ceiling=4,
            )

        contract = compiled["test_suites"][0]["selection_contract"]
        self.assertEqual(contract["config_selection_kind"], "fallback_pyproject")
        self.assertEqual(contract["config_addopts"], [])

    def test_nested_selector_chooses_and_binds_nested_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-nested-config-") as temporary:
            project = Path(temporary).resolve()
            tests = project / "pkg" / "tests"
            tests.mkdir(parents=True)
            config = project / "pkg" / "pytest.ini"
            config.write_text("[pytest]\naddopts = -q\n", encoding="utf-8")
            compiled = compile_entrypoints(
                project,
                [self._entrypoint(arguments=["pkg/tests"])],
                target_os="posix",
                native_worker_ceiling=4,
            )

        contract = compiled["test_suites"][0]["selection_contract"]
        self.assertEqual(contract["config_path"], str(config))
        self.assertEqual(contract["config_addopts"], ["-q"])

    def test_config_discovery_excludes_known_option_values_like_pytest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-config-argv-") as temporary:
            project = Path(temporary).resolve()
            tests = project / "tests"
            tests.mkdir()
            (project / "pytest.ini").write_text(
                "[pytest]\naddopts = -q\n", encoding="utf-8"
            )
            nested = tests / "pytest.ini"
            nested.write_text("[pytest]\naddopts = -s\n", encoding="utf-8")
            compiled = compile_entrypoints(
                project,
                [self._entrypoint(arguments=["-k", "tests"])],
                target_os="posix",
                native_worker_ceiling=4,
            )

        contract = compiled["test_suites"][0]["selection_contract"]
        self.assertEqual(contract["config_path"], str(project / "pytest.ini"))
        self.assertEqual(contract["config_addopts"], ["-q"])

    def test_same_directory_selectors_share_bundled_empty_config(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-shared-root-") as temporary:
            project = Path(temporary).resolve()
            first = project / "test_first.py"
            second = project / "test_second.py"
            first.write_text("def test_first(): pass\n", encoding="utf-8")
            second.write_text("def test_second(): pass\n", encoding="utf-8")
            compiled = compile_entrypoints(
                project,
                [self._entrypoint(arguments=[str(first), str(second)])],
                target_os="posix",
                native_worker_ceiling=4,
            )

        self.assertTrue(
            compiled["test_suites"][0]["selection_contract"][
                "uses_bundled_empty_config"
            ]
        )
        self.assertEqual(
            {
                Path(snapshot["path"]).name
                for snapshot in compiled["test_suites"][0]["selection_contract"][
                    "source_snapshots"
                ]
            },
            {"empty-pytest.ini", "test_first.py", "test_second.py"},
        )

    def test_empty_dot_pytest_ini_does_not_shadow_valid_pyproject(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-config-priority-") as temporary:
            project = Path(temporary).resolve()
            (project / ".pytest.ini").write_text("", encoding="utf-8")
            config = project / "pyproject.toml"
            config.write_text(
                "[tool.pytest.ini_options]\naddopts = '-q'\n",
                encoding="utf-8",
            )
            compiled = compile_entrypoints(
                project,
                [self._entrypoint()],
                target_os="posix",
                native_worker_ceiling=4,
            )

        contract = compiled["test_suites"][0]["selection_contract"]
        self.assertEqual(contract["config_path"], str(config))
        self.assertEqual(contract["config_addopts"], ["-q"])

    def test_parent_config_outside_project_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-parent-config-") as temporary:
            parent = Path(temporary).resolve()
            (parent / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            project = parent / "project"
            project.mkdir()
            with self.assertRaisesRegex(AtomError, "outside project_path"):
                compile_entrypoints(
                    project,
                    [self._entrypoint()],
                    target_os="posix",
                    native_worker_ceiling=4,
                )

    def test_test_selectors_cannot_escape_or_appear_after_compilation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-selector-scope-") as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            project.mkdir()
            config = project / "pytest.ini"
            config.write_text("[pytest]\n", encoding="utf-8")
            outside = root / "outside_test.py"
            outside.write_text("def test_outside(): pass\n", encoding="utf-8")
            cases = (
                self._entrypoint(
                    arguments=[str(outside)],
                    config_path=str(config),
                ),
                self._entrypoint(
                    arguments=[str(root / "not-created-yet.py")],
                    config_path=str(config),
                ),
                self._entrypoint(
                    arguments=["not-created-yet.py"],
                    config_path=str(config),
                ),
                self._entrypoint(arguments=["--pyargs", "external_package"]),
                self._entrypoint(arguments=["-otestpaths=/tmp"]),
            )
            for entrypoint in cases:
                with self.subTest(arguments=entrypoint["arguments"]), self.assertRaises(
                    AtomError
                ):
                    compile_entrypoints(
                        project,
                        [entrypoint],
                        target_os="posix",
                        native_worker_ceiling=4,
                    )

    def test_config_testpaths_and_pythonpath_stay_inside_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-config-scope-") as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            project.mkdir()
            config = project / "pytest.ini"
            for option in ("testpaths", "pythonpath"):
                config.write_text(
                    f"[pytest]\n{option} = ../outside\n",
                    encoding="utf-8",
                )
                with self.subTest(option=option), self.assertRaisesRegex(
                    AtomError,
                    "must stay inside project_path",
                ):
                    compile_entrypoints(
                        project,
                        [self._entrypoint(config_path=str(config))],
                        target_os="posix",
                        native_worker_ceiling=4,
                    )

    @unittest.skipUnless(os.name == "posix", "POSIX collection-link contract")
    def test_collection_directory_cannot_follow_link_outside_project(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-link-scope-") as temporary:
            root = Path(temporary).resolve()
            project = root / "project"
            tests = project / "tests"
            outside = root / "outside"
            tests.mkdir(parents=True)
            outside.mkdir()
            (outside / "test_escape.py").write_text(
                "def test_escape(): pass\n", encoding="utf-8"
            )
            (tests / "linked").symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(AtomError, "collection link escapes"):
                compile_entrypoints(
                    project,
                    [self._entrypoint(arguments=["tests"])],
                    target_os="posix",
                    native_worker_ceiling=4,
                )

    def test_baseline_static_scope_requires_selected_file_and_conftest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-baseline-scope-") as temporary:
            project = Path(temporary).resolve()
            selected = project / "test_selected.py"
            unrelated = project / "unrelated.py"
            conftest = project / "conftest.py"
            selected.write_text("def test_selected(): pass\n", encoding="utf-8")
            unrelated.write_text("VALUE = 1\n", encoding="utf-8")
            conftest.write_text("# fixture configuration\n", encoding="utf-8")

            missing_selected = compile_entrypoints(
                project,
                [
                    self._entrypoint(
                        arguments=[str(selected)],
                        snapshot_paths=[str(unrelated)],
                        baseline_source_closure_declared=True,
                    )
                ],
                target_os="posix",
                native_worker_ceiling=4,
            )
            missing_conftest = compile_entrypoints(
                project,
                [
                    self._entrypoint(
                        arguments=[str(selected)],
                        snapshot_paths=[str(selected)],
                        baseline_source_closure_declared=True,
                    )
                ],
                target_os="posix",
                native_worker_ceiling=4,
            )
            complete = compile_entrypoints(
                project,
                [
                    self._entrypoint(
                        arguments=[str(selected)],
                        snapshot_paths=[str(selected), str(conftest)],
                        baseline_source_closure_declared=True,
                    )
                ],
                target_os="posix",
                native_worker_ceiling=4,
            )

        self.assertFalse(missing_selected["test_suites"][0]["baseline_source_coverage"])
        self.assertFalse(missing_conftest["test_suites"][0]["baseline_source_coverage"])
        self.assertTrue(complete["test_suites"][0]["baseline_source_coverage"])

    def test_directory_baseline_requires_nested_conftest_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-nested-conftest-") as temporary:
            project = Path(temporary).resolve()
            unit = project / "tests" / "unit"
            unit.mkdir(parents=True)
            selected = unit / "test_selected.py"
            conftest = unit / "conftest.py"
            selected.write_text("def test_selected(): pass\n", encoding="utf-8")
            conftest.write_text("# nested fixture configuration\n", encoding="utf-8")

            missing = compile_entrypoints(
                project,
                [
                    self._entrypoint(
                        arguments=["tests"],
                        snapshot_paths=[str(selected)],
                        baseline_source_closure_declared=True,
                    )
                ],
                target_os="posix",
                native_worker_ceiling=4,
            )
            complete = compile_entrypoints(
                project,
                [
                    self._entrypoint(
                        arguments=["tests"],
                        snapshot_paths=[str(selected), str(conftest)],
                        baseline_source_closure_declared=True,
                    )
                ],
                target_os="posix",
                native_worker_ceiling=4,
            )

        self.assertFalse(missing["test_suites"][0]["baseline_source_coverage"])
        self.assertTrue(complete["test_suites"][0]["baseline_source_coverage"])

    @unittest.skipUnless(os.name == "posix", "requires symbolic links")
    def test_collection_symlink_disables_serial_baseline_coverage(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="atomlane-pytest-collection-link-"
        ) as temporary:
            project = Path(temporary).resolve()
            tests = project / "tests"
            tests.mkdir()
            implementation = project / "test_implementation.py"
            implementation.write_text("def test_case(): pass\n", encoding="utf-8")
            (tests / "test_link.py").symlink_to(implementation)
            compiled = compile_entrypoints(
                project,
                [
                    self._entrypoint(
                        arguments=[str(tests)],
                        snapshot_paths=[str(implementation)],
                        baseline_source_closure_declared=True,
                        worker_count=1,
                    )
                ],
                target_os="posix",
                native_worker_ceiling=2,
            )

        self.assertFalse(
            compiled["test_suites"][0]["baseline_source_coverage"]
        )

    def test_ini_defaults_and_uppercase_addopts_are_not_materialized(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-ini-case-") as temporary:
            project = Path(temporary).resolve()
            (project / "pytest.ini").write_text(
                "[DEFAULT]\naddopts = should-not-run\n[pytest]\nADDOPTS = also-not-run\n",
                encoding="utf-8",
            )
            compiled = compile_entrypoints(
                project,
                [self._entrypoint()],
                target_os="posix",
                native_worker_ceiling=4,
            )

        self.assertEqual(
            compiled["test_suites"][0]["selection_contract"]["config_addopts"],
            [],
        )

    def test_incomplete_effects_compile_but_execution_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-effects-") as temporary:
            project = Path(temporary).resolve()
            plan = mcp_server.atomic_task_plan(
                {
                    "project_path": str(project),
                    "entrypoints": [
                        self._entrypoint(
                            worker_count=1,
                            effects_declared_complete=False,
                        )
                    ],
                    "max_concurrency": 1,
                }
            )

            self.assertFalse(plan["execution_eligible"])
            atom = plan["atoms"][0]
            self.assertEqual(atom["assurance"]["effects"], "unknown")
            self.assertIn("INCOMPLETE_TEST_EFFECT_MODEL", atom["assurance"]["blockers"])
            with self.assertRaisesRegex(mcp_server.InputError, "not execution eligible"):
                mcp_server._verify_compiled_plan(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )

    def test_parallel_suite_requires_explicit_case_independence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-independence-") as temporary:
            project = Path(temporary).resolve()
            compiled = compile_entrypoints(
                project,
                [self._entrypoint(independence_declared=False)],
                target_os="posix",
                native_worker_ceiling=4,
            )

        self.assertIn(
            "TEST_CASE_INDEPENDENCE_NOT_DECLARED",
            compiled["atoms"][0]["assurance"]["blockers"],
        )
        self.assertIn(
            "TEST_CASE_INDEPENDENCE_NOT_DECLARED",
            {item["code"] for item in compiled["diagnostics"]},
        )

    def test_timeout_is_bound_into_the_immutable_plan_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-timeout-hash-") as temporary:
            project = Path(temporary).resolve()
            plan = mcp_server.atomic_task_plan(
                {
                    "project_path": str(project),
                    "entrypoints": [self._entrypoint(worker_count=1, timeout_seconds=0.25)],
                    "max_concurrency": 1,
                }
            )
            self.assertEqual(plan["atoms"][0]["operation"]["timeout_seconds"], 0.25)

            changed = copy.deepcopy(plan)
            changed["atoms"][0]["operation"]["timeout_seconds"] = 10.0
            with self.assertRaisesRegex(mcp_server.InputError, "envelope was changed"):
                mcp_server._verify_compiled_plan(
                    {"compiled_plan": changed, "plan_hash": plan["plan_hash"]}
                )

    def test_windows_python_exe_module_runner_is_recognized_without_host_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-windows-") as temporary:
            project = Path(temporary).resolve()
            windows_python = (
                sys.executable if os.name == "nt" else r"C:\Python312\python.exe"
            )
            windows_runner = [windows_python, "-m", "pytest"]
            with mock.patch.object(
                atom_frontends.subprocess,
                "run",
                side_effect=AssertionError("cross-platform parsing must stay static"),
            ):
                compiled = compile_entrypoints(
                    project,
                    [self._entrypoint(runner_argv=windows_runner, worker_count=2)],
                    target_os="nt",
                    native_worker_ceiling=4,
                )

        atom = compiled["atoms"][0]
        self.assertEqual(atom["operation"]["argv"][:3], windows_runner)
        self.assertEqual(
            atom_frontends._native_parallelism(
                [*windows_runner, "-n", "2", "tests"]
            ),
            {"kind": "bounded", "tokens": 2},
        )

    def test_windows_paths_and_command_lines_use_native_identity_limits(self) -> None:
        self.assertEqual(
            atom_frontends._path_identity(r"C:\\Project\\OUT.xml", "nt"),
            atom_frontends._path_identity(r"c:/project/out.xml", "nt"),
        )
        with self.assertRaisesRegex(mcp_server.InputError, "CreateProcessW"):
            mcp_server._validate_windows_command_line(
                ["python.exe", "x" * 33_000],
                "long-pytest",
            )

    def test_windows_case_alias_junit_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-windows-paths-") as temporary:
            project = Path(temporary).resolve()
            source = project / "test_sample.py"
            source.write_text("def test_sample(): pass\n", encoding="utf-8")
            first = self._entrypoint(
                id="first",
                arguments=[str(source)],
                junit_path="OUT.xml",
                worker_count=1,
            )
            second = self._entrypoint(
                id="second",
                arguments=[str(source)],
                junit_path="out.xml",
                worker_count=1,
            )
            with self.assertRaisesRegex(AtomError, "junit_path must be unique"):
                compile_entrypoints(
                    project,
                    [first, second],
                    target_os="nt",
                    native_worker_ceiling=2,
                )

    def test_windows_ambiguous_or_reserved_junit_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="atomlane-pytest-windows-ambiguous-"
        ) as temporary:
            project = Path(temporary).resolve()
            source = project / "test_sample.py"
            source.write_text("def test_sample(): pass\n", encoding="utf-8")
            unsafe_names = (
                "report.xml.",
                "report.xml ",
                "report.xml:stream",
                "report.xml::$DATA",
                "CON.xml",
                "nul",
                "COM1.txt",
                "LPT\N{SUPERSCRIPT ONE}.xml",
                "safe/NUL/report.xml",
                "NUL .xml",
                "bad?.xml",
                r"C:report.xml",
                r"\\?\C:\safe\report.xml",
                "//?/C:/safe/report.xml",
                "//?/UNC/server/share/report.xml",
                r"\\server.\share\report.xml",
                r"\\server\share.\report.xml",
            )
            for unsafe_name in unsafe_names:
                with self.subTest(unsafe_name=unsafe_name), self.assertRaisesRegex(
                    AtomError,
                    "ambiguous or reserved Windows pathname",
                ):
                    compile_entrypoints(
                        project,
                        [
                            self._entrypoint(
                                arguments=[str(source)],
                                junit_path=unsafe_name,
                                worker_count=1,
                            )
                        ],
                        target_os="nt",
                        native_worker_ceiling=2,
                    )

            for safe_name in (
                "results.xml",
                r"reports\run.xml",
                r"C:\safe\report.xml",
                r"\\server\share\report.xml",
                ".run.xml",
                "COM10.xml",
                "NULsafe.xml",
            ):
                with self.subTest(safe_name=safe_name):
                    self.assertTrue(
                        atom_frontends._windows_output_path_spelling_is_unambiguous(
                            safe_name
                        )
                    )

    @unittest.skipUnless(os.name == "posix", "requires symbolic links")
    def test_symlinked_selector_and_config_paths_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="atomlane-pytest-link-selector-"
        ) as temporary:
            project = Path(temporary).resolve()
            target = project / "target"
            target.mkdir()
            source = target / "test_sample.py"
            source.write_text("def test_sample(): pass\n", encoding="utf-8")
            linked_parent = project / "linked-parent"
            linked_parent.mkdir()
            selector_link = linked_parent / "tests"
            selector_link.symlink_to(target, target_is_directory=True)

            with self.assertRaisesRegex(AtomError, "symbolic-link aliases"):
                compile_entrypoints(
                    project,
                    [self._entrypoint(arguments=[str(selector_link)], worker_count=1)],
                    target_os="posix",
                    native_worker_ceiling=2,
                )

            config = project / "pytest.ini"
            config.write_text(
                "[pytest]\ntestpaths = linked-parent/tests\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AtomError, "symbolic-link aliases"):
                compile_entrypoints(
                    project,
                    [self._entrypoint(config_path=str(config), worker_count=1)],
                    target_os="posix",
                    native_worker_ceiling=2,
                )

            helper = project / "helper.py"
            helper.symlink_to(source)
            with self.assertRaisesRegex(AtomError, "symbolic-link aliases"):
                compile_entrypoints(
                    project,
                    [
                        self._entrypoint(
                            arguments=[str(source)],
                            snapshot_paths=[str(source), str(helper)],
                            worker_count=1,
                        )
                    ],
                    target_os="posix",
                    native_worker_ceiling=2,
                )

    def test_posix_case_alias_junit_paths_fail_closed_for_macos_portability(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-posix-paths-") as temporary:
            project = Path(temporary).resolve()
            source = project / "test_sample.py"
            source.write_text("def test_sample(): pass\n", encoding="utf-8")
            first = self._entrypoint(
                id="first",
                arguments=[str(source)],
                junit_path="Results.xml",
                worker_count=1,
            )
            second = self._entrypoint(
                id="second",
                arguments=[str(source)],
                junit_path="results.xml",
                worker_count=1,
            )

            with self.assertRaisesRegex(AtomError, "junit_path must be unique"):
                compile_entrypoints(
                    project,
                    [first, second],
                    target_os="posix",
                    native_worker_ceiling=2,
                )

        self.assertEqual(
            mcp_server._pytest_output_lease_identity("/tmp/Results.xml"),
            mcp_server._pytest_output_lease_identity("/tmp/results.xml"),
        )
        self.assertEqual(
            atom_frontends._path_identity("/tmp/r\N{LATIN SMALL LETTER E WITH ACUTE}.xml", "posix"),
            atom_frontends._path_identity("/tmp/re\N{COMBINING ACUTE ACCENT}.xml", "posix"),
        )

    def test_junit_directory_aliases_cannot_enter_collection_scope(self) -> None:
        aliases = (
            ("tests", "TESTS"),
            (
                "t\N{LATIN SMALL LETTER E WITH ACUTE}sts",
                "te\N{COMBINING ACUTE ACCENT}sts",
            ),
        )
        for root_name, alias_name in aliases:
            with self.subTest(alias=alias_name), tempfile.TemporaryDirectory(
                prefix="atomlane-pytest-collection-alias-"
            ) as temporary:
                project = Path(temporary).resolve()
                collection_root = project / root_name
                collection_root.mkdir()
                alias_root = project / alias_name
                if not alias_root.exists():
                    alias_root.mkdir()
                victim = collection_root / "test_victim.py"
                victim.write_text("def test_victim(): pass\n", encoding="utf-8")
                original = victim.read_bytes()

                with self.assertRaisesRegex(AtomError, "collection scope"):
                    compile_entrypoints(
                        project,
                        [
                            self._entrypoint(
                                arguments=[str(collection_root)],
                                junit_path=str(alias_root / "report.xml"),
                                worker_count=1,
                            )
                        ],
                        target_os="posix",
                        native_worker_ceiling=2,
                    )

                self.assertEqual(victim.read_bytes(), original)

    def test_junit_outputs_cannot_cross_another_suite_collection_scope(self) -> None:
        for prior_report_inside_current in (False, True):
            with self.subTest(
                prior_report_inside_current=prior_report_inside_current
            ), tempfile.TemporaryDirectory(
                prefix="atomlane-pytest-cross-suite-output-"
            ) as temporary:
                project = Path(temporary).resolve()
                first_root = project / "first-tests"
                second_root = project / "second-tests"
                output_root = project / "reports"
                first_root.mkdir()
                second_root.mkdir()
                output_root.mkdir()
                first = self._entrypoint(
                    id="first",
                    arguments=[str(first_root)],
                    junit_path=str(
                        second_root / "prior.xml"
                        if prior_report_inside_current
                        else output_root / "first.xml"
                    ),
                    worker_count=1,
                )
                second = self._entrypoint(
                    id="second",
                    arguments=[str(second_root)],
                    junit_path=str(
                        output_root / "second.xml"
                        if prior_report_inside_current
                        else first_root / "current.xml"
                    ),
                    worker_count=1,
                )

                with self.assertRaisesRegex(AtomError, "another suite"):
                    compile_entrypoints(
                        project,
                        [first, second],
                        target_os="posix",
                        native_worker_ceiling=2,
                    )

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS firmlinks")
    def test_macos_firmlink_cannot_alias_collection_or_plan_outputs(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(
            prefix="atomlane-pytest-firmlink-",
            dir=source_root,
        ) as temporary:
            project = Path(temporary).resolve()
            alias_project = Path("/System/Volumes/Data") / project.relative_to("/")
            if not alias_project.is_dir() or not os.path.samefile(
                project,
                alias_project,
            ):
                self.skipTest("the source volume has no /System/Volumes/Data firmlink")
            tests = project / "tests"
            reports = project / "reports"
            tests.mkdir()
            reports.mkdir()
            victim = tests / "test_victim.py"
            victim.write_text("def test_victim(): pass\n", encoding="utf-8")
            original = victim.read_bytes()

            with self.assertRaisesRegex(AtomError, "collection scope"):
                compile_entrypoints(
                    project,
                    [
                        self._entrypoint(
                            arguments=[str(tests)],
                            junit_path=str(alias_project / "tests" / victim.name),
                            worker_count=1,
                        )
                    ],
                    target_os="posix",
                    native_worker_ceiling=2,
                )
            self.assertEqual(victim.read_bytes(), original)

            first = self._entrypoint(
                id="first",
                arguments=[str(victim)],
                junit_path=str(reports / "shared.xml"),
                worker_count=1,
            )
            second = self._entrypoint(
                id="second",
                arguments=[str(victim)],
                junit_path=str(alias_project / "reports" / "shared.xml"),
                worker_count=1,
            )
            with self.assertRaisesRegex(AtomError, "unique within the plan"):
                compile_entrypoints(
                    project,
                    [first, second],
                    target_os="posix",
                    native_worker_ceiling=2,
                )

    def test_reserved_basetemp_namespace_cannot_contain_explicit_junit(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-reserved-temp-") as temporary:
            project = Path(temporary).resolve()
            source = project / "test_sample.py"
            source.write_text("def test_sample(): pass\n", encoding="utf-8")
            reserved = project / f"atomlane-pytest-{'a' * 32}-tmp"
            reserved.mkdir()

            with self.assertRaisesRegex(AtomError, "reserved pytest base-temp"):
                compile_entrypoints(
                    project,
                    [
                        self._entrypoint(
                            arguments=[str(source)],
                            junit_path=str(reserved / "report.xml"),
                            worker_count=1,
                        )
                    ],
                    target_os="posix",
                    native_worker_ceiling=2,
                )

    def test_zero_inode_directory_visit_keys_fall_back_to_paths(self) -> None:
        state = mock.Mock(st_ino=0, st_dev=0)
        first = atom_frontends._pytest_directory_visit_key(
            Path("/tmp/first"),
            state,
        )
        second = atom_frontends._pytest_directory_visit_key(
            Path("/tmp/second"),
            state,
        )

        self.assertNotEqual(first, second)
        self.assertEqual(first[0], "path")
        self.assertNotEqual(
            atom_frontends._pytest_directory_visit_key(Path("/tmp/A"), state),
            atom_frontends._pytest_directory_visit_key(Path("/tmp/a"), state),
        )
        self.assertNotEqual(
            atom_frontends._pytest_directory_visit_key(
                Path("/tmp/r\N{LATIN SMALL LETTER E WITH ACUTE}"),
                state,
            ),
            atom_frontends._pytest_directory_visit_key(
                Path("/tmp/re\N{COMBINING ACUTE ACCENT}"),
                state,
            ),
        )
        with mock.patch.object(Path, "stat", return_value=state), self.assertRaisesRegex(
            AtomError,
            "physical identity is unavailable",
        ):
            atom_frontends._path_physical_anchor_identity(
                Path("/tmp/no-inode"),
                "posix",
            )

    def test_output_lease_root_is_stable_and_windows_parent_is_explicit(self) -> None:
        expected = mcp_server._pytest_output_lease_root()
        with mock.patch.object(
            mcp_server.tempfile,
            "gettempdir",
            return_value="/different/per-process/temp",
        ):
            self.assertEqual(mcp_server._pytest_output_lease_root(), expected)
        self.assertEqual(
            mcp_server._pytest_output_lease_root(
                host_os="nt",
                known_local_app_data=Path("C:/Users/Example/AppData/Local"),
            ),
            Path("C:/Users/Example/AppData/Local/AtomLane/pytest-output-leases-v1"),
        )

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS firmlinks")
    def test_output_leases_collapse_macos_firmlink_aliases(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(
            prefix="atomlane-lease-firmlink-",
            dir=source_root,
        ) as temporary:
            primary = Path(temporary).resolve()
            alias = Path("/System/Volumes/Data") / primary.relative_to("/")
            if not alias.is_dir() or not os.path.samefile(primary, alias):
                self.skipTest("the source volume has no /System/Volumes/Data firmlink")

            missing_primary = {
                "test_suites": [
                    {
                        "junit_path": str(primary / "missing.xml"),
                        "basetemp_path": str(primary / "temp-a"),
                    }
                ]
            }
            missing_alias = {
                "test_suites": [
                    {
                        "junit_path": str(alias / "missing.xml"),
                        "basetemp_path": str(alias / "temp-b"),
                    }
                ]
            }
            existing = primary / "existing.xml"
            existing.write_text("existing", encoding="utf-8")
            existing_primary = {
                "test_suites": [
                    {
                        "junit_path": str(existing),
                        "basetemp_path": str(primary / "temp-c"),
                    }
                ]
            }
            existing_alias = {
                "test_suites": [
                    {
                        "junit_path": str(alias / existing.name),
                        "basetemp_path": str(alias / "temp-d"),
                    }
                ]
            }
            temp_primary = {
                "test_suites": [
                    {
                        "junit_path": str(primary / "first.xml"),
                        "basetemp_path": str(primary / "shared-temp"),
                    }
                ]
            }
            temp_alias = {
                "test_suites": [
                    {
                        "junit_path": str(alias / "second.xml"),
                        "basetemp_path": str(alias / "shared-temp"),
                    }
                ]
            }
            with mcp_server._acquire_pytest_output_leases(missing_primary):
                (primary / "missing.xml").write_text("created", encoding="utf-8")
                with (
                    self.assertRaisesRegex(mcp_server.InputError, "already in use"),
                    mcp_server._acquire_pytest_output_leases(missing_alias),
                ):
                    self.fail("post-creation firmlink alias acquired a second lease")
            for first_context, second_context in (
                (existing_primary, existing_alias),
                (temp_primary, temp_alias),
            ):
                with (
                    self.subTest(first=first_context),
                    mcp_server._acquire_pytest_output_leases(first_context),
                    self.assertRaisesRegex(mcp_server.InputError, "already in use"),
                    mcp_server._acquire_pytest_output_leases(second_context),
                ):
                    self.fail("physical output alias acquired a second lease")

    @unittest.skipUnless(os.name == "nt", "requires native Windows known folders")
    def test_windows_lease_root_ignores_profile_environment(self) -> None:
        original = mcp_server._pytest_output_lease_root()
        with mock.patch.dict(
            os.environ,
            {
                "USERPROFILE": r"Z:\untrusted-profile",
                "HOMEDRIVE": "Z:",
                "HOMEPATH": r"\untrusted-profile",
                "LOCALAPPDATA": r"Z:\untrusted-local-app-data",
            },
        ):
            changed = mcp_server._pytest_output_lease_root()
        self.assertEqual(changed, original)

    def test_output_leases_cross_process_with_different_temp_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-lease-process-") as temporary:
            root = Path(temporary).resolve()
            other_temp = root / "other-temp"
            other_temp.mkdir()
            fake_profile = root / "fake-profile"
            fake_profile.mkdir()
            context = {
                "test_suites": [
                    {
                        "junit_path": str(root / "report.xml"),
                        "basetemp_path": str(root / "pytest-temp"),
                    }
                ]
            }
            child_code = """
import json
import sys
import mcp_server

context = json.load(sys.stdin)
try:
    lease = mcp_server._acquire_pytest_output_leases(context)
except mcp_server.InputError as exc:
    raise SystemExit(23 if "already in use" in str(exc) else 24)
else:
    lease.close()
    raise SystemExit(0)
"""
            environment = os.environ.copy()
            environment.update(
                {
                    "PYTHONPATH": str(SCRIPT_DIR),
                    "TMPDIR": str(other_temp),
                    "TMP": str(other_temp),
                    "TEMP": str(other_temp),
                    "USERPROFILE": str(fake_profile),
                    "LOCALAPPDATA": str(fake_profile / "AppData" / "Local"),
                }
            )
            with mcp_server._acquire_pytest_output_leases(context):
                completed = subprocess.run(
                    [sys.executable, "-c", child_code],
                    input=json.dumps(context),
                    text=True,
                    capture_output=True,
                    env=environment,
                    timeout=10,
                    check=False,
                )

        self.assertEqual(
            completed.returncode,
            23,
            completed.stdout + completed.stderr,
        )

    def test_junit_output_cannot_overlap_config_snapshot_or_runner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-output-alias-") as temporary:
            project = Path(temporary).resolve()
            config = project / "pytest.ini"
            config.write_text("[pytest]\n", encoding="utf-8")
            source = project / "test_source.py"
            source.write_text("def test_ok(): pass\n", encoding="utf-8")
            cases = (
                self._entrypoint(junit_path=str(config)),
                self._entrypoint(
                    snapshot_paths=[str(source)],
                    junit_path=str(source),
                ),
                self._entrypoint(junit_path=str(Path(sys.executable).resolve())),
            )
            for entrypoint in cases:
                with self.subTest(junit_path=entrypoint["junit_path"]), self.assertRaisesRegex(
                    AtomError,
                    "junit_path (?:overlaps|aliases)",
                ):
                    compile_entrypoints(
                        project,
                        [entrypoint],
                        target_os="posix",
                        native_worker_ceiling=4,
                    )

    def test_junit_output_cannot_hardlink_a_snapshotted_input(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-output-hardlink-") as temporary:
            project = Path(temporary).resolve()
            config = project / "pytest.ini"
            config.write_text("[pytest]\n", encoding="utf-8")
            source = project / "test_source.py"
            source.write_text("def test_ok(): pass\n", encoding="utf-8")
            for index, target in enumerate((config, source)):
                output = project / f"hardlink-{index}.xml"
                os.link(target, output)
                with self.subTest(target=target), self.assertRaisesRegex(
                    AtomError,
                    "aliases",
                ):
                    compile_entrypoints(
                        project,
                        [
                            self._entrypoint(
                                snapshot_paths=[str(source)],
                                junit_path=str(output),
                            )
                        ],
                        target_os="posix",
                        native_worker_ceiling=4,
                    )
                output.unlink()

    def test_junit_output_cannot_overwrite_or_hardlink_a_direct_selector(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-selector-output-") as temporary:
            project = Path(temporary).resolve()
            selected = project / "test_victim.py"
            selected.write_text("def test_victim(): pass\n", encoding="utf-8")
            with self.assertRaisesRegex(AtomError, "collection scope"):
                compile_entrypoints(
                    project,
                    [
                        self._entrypoint(
                            arguments=[str(selected)],
                            snapshot_paths=[],
                            junit_path=str(selected),
                        )
                    ],
                    target_os="posix",
                    native_worker_ceiling=4,
                )
            alias = project / "outside-selected-path.xml"
            os.link(selected, alias)
            with self.assertRaisesRegex(AtomError, "aliases"):
                compile_entrypoints(
                    project,
                    [
                        self._entrypoint(
                            arguments=[str(selected)],
                            snapshot_paths=[],
                            junit_path=str(alias),
                        )
                    ],
                    target_os="posix",
                    native_worker_ceiling=4,
                )

    @unittest.skipUnless(os.name == "posix", "POSIX symlink contract")
    def test_absolute_virtualenv_python_symlink_is_not_dereferenced(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-venv-") as temporary:
            project = Path(temporary).resolve()
            runner = project / "venv" / "bin" / "python"
            runner.parent.mkdir(parents=True)
            (project / "venv" / "pyvenv.cfg").write_text(
                "home = /usr/bin\n", encoding="utf-8"
            )
            runner.symlink_to(sys.executable)
            compiled = compile_entrypoints(
                project,
                [self._entrypoint(runner_argv=[str(runner), "-m", "pytest"])],
                target_os="posix",
                native_worker_ceiling=4,
            )
            resolved_runner = str(runner.resolve())

        self.assertEqual(compiled["atoms"][0]["operation"]["argv"][0], str(runner))
        self.assertNotEqual(str(runner), resolved_runner)

    def test_public_test_suite_tool_returns_the_standard_atomic_exec_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-pytest-tool-") as temporary:
            project = Path(temporary).resolve()
            (project / "tests").mkdir()
            arguments = {
                "project_path": str(project),
                "runner_argv": [sys.executable, "-m", "pytest"],
                "arguments": ["tests"],
                "worker_count": 1,
                "effects_declared_complete": True,
                "env": {"PYTEST_ADDOPTS": ""},
                "max_concurrency": 1,
            }
            plan = mcp_server.test_suite_plan(arguments)

            self.assertEqual(plan["execution_contract"]["tool"], "atomic_exec")
            self.assertEqual(plan["atoms"][0]["provenance"]["adapter"], "test_suite")
            self.assertEqual(plan["test_suites"][0]["framework"], "pytest")
            tool = next(
                item for item in mcp_server.TOOLS if item["name"] == "test_suite_plan"
            )
            self.assertIn("runner_argv", tool["inputSchema"]["required"])
            response = asyncio.run(mcp_server.call_tool("test_suite_plan", arguments))
            self.assertEqual(
                response["structuredContent"]["execution_contract"]["tool"],
                "atomic_exec",
            )


class PytestTestSuiteRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runner_temporary = tempfile.TemporaryDirectory(
            prefix="atomlane-fake-pytest-runner-"
        )
        runner_root = Path(cls.runner_temporary.name) / "venv"
        venv.EnvBuilder(with_pip=False).create(runner_root)
        if os.name == "nt":
            cls.fake_runner = runner_root / "Scripts" / "python.exe"
            site_packages = runner_root / "Lib" / "site-packages"
        else:
            cls.fake_runner = runner_root / "bin" / "python"
            site_packages = (
                runner_root
                / "lib"
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
        fake_package = site_packages / "pytest"
        fake_package.mkdir(parents=True, exist_ok=True)
        (fake_package / "__init__.py").write_text("", encoding="utf-8")
        cls.fake_pytest_main = fake_package / "__main__.py"
        cls.fake_pytest_main.write_text("", encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.runner_temporary.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atomlane-pytest-runtime-")
        self.project = Path(self.temporary.name).resolve()
        self.stats_path = self.project / "stats.json"
        self.environment = mock.patch.dict(
            os.environ,
            {"ATOMLANE_STATS_PATH": str(self.stats_path), "PYTEST_ADDOPTS": ""},
        )
        self.environment.start()
        (self.project / "subject.py").write_text("# immutable selector\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    @staticmethod
    def _resource_plan(
        profile: str,
        requested: int | None = None,
        reserve_cores: int | None = None,
        estimated_memory_mb_per_task: float | None = None,
        responsiveness: str = "interactive",
    ) -> dict[str, object]:
        chosen = requested or 4
        return {
            "profile": profile,
            "responsiveness": responsiveness,
            "recommended_concurrency": 4,
            "chosen_concurrency": chosen,
            "reserve_cores": reserve_cores or 0,
            "reserve_cores_source": "explicit",
            "nice_adjustment": 0,
            "qos_clamp": None,
            "estimated_memory_mb_per_task": estimated_memory_mb_per_task,
            "memory_limited_concurrency": None,
            "reasons": ["deterministic test fixture"],
            "machine": {
                "memory_available_bytes_approx": 8 * 1024 * 1024 * 1024,
            },
        }

    def _plan(self, *, timeout_seconds: float, workers: int = 1) -> dict[str, object]:
        return mcp_server.atomic_task_plan(
            {
                "project_path": str(self.project),
                "profile": "cpu",
                "entrypoints": [
                    {
                        "adapter": "test_suite",
                        "id": "fake-pytest",
                        "framework": "pytest",
                        "runner_argv": [str(self.fake_runner), "-m", "pytest"],
                        "arguments": ["subject.py"],
                        "worker_count": workers,
                        "distribution": "loadfile",
                        "case_count_hint": 100,
                        "timeout_seconds": timeout_seconds,
                        "effects_declared_complete": True,
                        "independence_declared": workers > 1,
                        "snapshot_paths": ["subject.py"],
                        "baseline_source_closure_declared": True,
                        "env": {"PYTEST_ADDOPTS": ""},
                        "junit_path": "results.xml",
                    }
                ],
                "max_concurrency": 4,
                "reserve_cores": 0,
            }
        )

    def test_operation_timeout_is_enforced_by_atomic_execution(self) -> None:
        self.fake_pytest_main.write_text(
            "import time\ntime.sleep(5)\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            plan = self._plan(timeout_seconds=0.05)
            result = asyncio.run(
                asyncio.wait_for(
                    mcp_server.run_atomic(
                        {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                    ),
                    timeout=5,
                )
            )

        self.assertEqual(result["results"][0]["status"], "timed_out")
        self.assertLess(result["results"][0]["duration_seconds"], 2.0)
        self.assertFalse(result["indicator"]["savings_eligible"])

    def test_ambient_pythonoptimize_cannot_strip_helper_assertions(self) -> None:
        helper = self.project / "helper.py"
        helper.write_text(
            "def verify():\n"
            "    assert False, 'assertion must remain active'\n",
            encoding="utf-8",
        )
        self.fake_pytest_main.write_text(
            "from helper import verify\n"
            "verify()\n",
            encoding="utf-8",
        )
        with (
            mock.patch.dict(os.environ, {"PYTHONOPTIMIZE": "1"}),
            mock.patch.object(
                mcp_server,
                "concurrency_plan",
                side_effect=self._resource_plan,
            ),
        ):
            plan = mcp_server.atomic_task_plan(
                {
                    "project_path": str(self.project),
                    "profile": "cpu",
                    "entrypoints": [
                        {
                            "adapter": "test_suite",
                            "id": "assertion-safety",
                            "framework": "pytest",
                            "runner_argv": [str(self.fake_runner), "-m", "pytest"],
                            "arguments": ["subject.py"],
                            "worker_count": 1,
                            "timeout_seconds": 5,
                            "effects_declared_complete": True,
                            "snapshot_paths": ["subject.py", "helper.py"],
                            "baseline_source_closure_declared": True,
                            "env": {"PYTEST_ADDOPTS": ""},
                            "junit_path": "assertion-results.xml",
                        }
                    ],
                    "max_concurrency": 1,
                    "reserve_cores": 0,
                }
            )
            self.assertEqual(
                plan["atoms"][0]["operation"]["env"]["PYTHONOPTIMIZE"],
                "",
            )
            result = asyncio.run(
                mcp_server.run_atomic(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )
            )

        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertNotEqual(result["results"][0]["returncode"], 0)
        self.assertIn("assertion must remain active", result["results"][0]["stderr"])

    def test_native_pool_emits_multiple_live_heartbeats_before_completion(self) -> None:
        self.fake_pytest_main.write_text(
            "from pathlib import Path\n"
            "import sys, time\n"
            "target = next(a.split('=', 1)[1] for a in sys.argv[1:] "
            "if a.startswith('--junitxml='))\n"
            "time.sleep(0.35)\n"
            "Path(target).write_text('<testsuite tests=\"1\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\"><testcase name=\"live\" "
            "time=\"0.2\" /></testsuite>', encoding='utf-8')\n",
            encoding="utf-8",
        )
        progress: list[dict[str, object]] = []
        with (
            mock.patch.object(
                mcp_server,
                "concurrency_plan",
                side_effect=self._resource_plan,
            ),
            mock.patch.dict(
                os.environ,
                {"ATOMLANE_PROGRESS_INTERVAL": "0.05"},
            ),
        ):
            plan = self._plan(timeout_seconds=5.0, workers=2)
            result = asyncio.run(
                asyncio.wait_for(
                    mcp_server.run_atomic(
                        {"compiled_plan": plan, "plan_hash": plan["plan_hash"]},
                        progress.append,
                    ),
                    timeout=10,
                )
            )

        running = [
            item
            for item in progress
            if item.get("running_tasks") == 1
            and item.get("completed_tasks") == 0
        ]
        self.assertEqual(result["results"][0]["status"], "succeeded")
        self.assertGreaterEqual(len(running), 3, progress)
        self.assertGreater(
            float(running[-1]["elapsed_seconds"]),
            float(running[0]["elapsed_seconds"]),
        )
        self.assertTrue(
            all(item.get("native_workers_configured") == 2 for item in running)
        )
        self.assertTrue(
            all(item.get("estimated_saved_so_far_seconds") is None for item in running)
        )

    @unittest.skipUnless(os.name == "posix", "POSIX runner symlink contract")
    def test_runtime_rejects_runner_symlink_retarget(self) -> None:
        runner_dir = self.project / "runner-bin"
        runner_dir.mkdir()
        runner = runner_dir / "python"
        runner.symlink_to(self.fake_runner)
        plan = mcp_server.atomic_task_plan(
            {
                "project_path": str(self.project),
                "entrypoints": [
                    {
                        "adapter": "test_suite",
                        "id": "runner-swap",
                        "framework": "pytest",
                        "runner_argv": [str(runner), "-m", "pytest"],
                        "arguments": ["subject.py"],
                        "worker_count": 1,
                        "effects_declared_complete": True,
                        "snapshot_paths": ["subject.py"],
                        "env": {"PYTEST_ADDOPTS": ""},
                    }
                ],
                "max_concurrency": 1,
            }
        )
        runner.unlink()
        runner.symlink_to("/bin/echo")

        with self.assertRaisesRegex(mcp_server.InputError, "runner changed"):
            mcp_server._test_suite_execution_context(plan)

    @unittest.skipUnless(os.name == "posix", "POSIX source-snapshot contract")
    def test_runtime_rejects_source_snapshot_retargeted_to_same_bytes(self) -> None:
        helper = self.project / "helper.py"
        helper.write_text("VALUE = 1\n", encoding="utf-8")
        plan = mcp_server.atomic_task_plan(
            {
                "project_path": str(self.project),
                "entrypoints": [
                    {
                        "adapter": "test_suite",
                        "id": "source-swap",
                        "framework": "pytest",
                        "runner_argv": [str(self.fake_runner), "-m", "pytest"],
                        "arguments": ["subject.py"],
                        "worker_count": 1,
                        "effects_declared_complete": True,
                        "snapshot_paths": ["subject.py", "helper.py"],
                        "baseline_source_closure_declared": True,
                        "env": {"PYTEST_ADDOPTS": ""},
                    }
                ],
                "max_concurrency": 1,
            }
        )
        replacement = self.project / "replacement.py"
        replacement.write_bytes(helper.read_bytes())
        helper.unlink()
        helper.symlink_to(replacement)

        with self.assertRaisesRegex(
            mcp_server.InputError,
            "canonical|revalidated|snapshot",
        ):
            asyncio.run(
                mcp_server.run_atomic(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )
            )

    def test_same_plan_cannot_reuse_pytest_outputs_concurrently(self) -> None:
        self.fake_pytest_main.write_text(
            "from pathlib import Path\n"
            "import sys, time\n"
            "target = next(a.split('=', 1)[1] for a in sys.argv[1:] "
            "if a.startswith('--junitxml='))\n"
            "time.sleep(0.25)\n"
            "Path(target).write_text('<testsuite tests=\"1\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\"><testcase name=\"owner\" "
            "time=\"0.1\" /></testsuite>', encoding='utf-8')\n",
            encoding="utf-8",
        )

        async def exercise(plan: dict[str, object]) -> tuple[dict[str, object], float]:
            started = asyncio.Event()

            def progress(item: dict[str, object]) -> None:
                if item.get("running_tasks") == 1:
                    started.set()

            first = asyncio.create_task(
                mcp_server.run_atomic(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]},
                    progress,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            conflict_started = time.monotonic()
            with self.assertRaisesRegex(mcp_server.InputError, "already in use"):
                await mcp_server.run_atomic(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )
            conflict_elapsed = time.monotonic() - conflict_started
            return await asyncio.wait_for(first, timeout=5), conflict_elapsed

        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            plan = self._plan(timeout_seconds=5.0)
            result, conflict_elapsed = asyncio.run(exercise(plan))
            retry = asyncio.run(
                mcp_server.run_atomic(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )
            )

        self.assertEqual(result["results"][0]["status"], "succeeded")
        self.assertEqual(retry["results"][0]["status"], "succeeded")
        self.assertLess(conflict_elapsed, 0.5)

    def test_different_plans_cannot_share_one_junit_path_concurrently(self) -> None:
        other = self.project / "other.py"
        other.write_text("# second immutable selector\n", encoding="utf-8")
        self.fake_pytest_main.write_text(
            "from pathlib import Path\n"
            "import sys, time\n"
            "target = next(a.split('=', 1)[1] for a in sys.argv[1:] "
            "if a.startswith('--junitxml='))\n"
            "selector = next(a for a in reversed(sys.argv[1:]) if a.endswith('.py'))\n"
            "time.sleep(0.25)\n"
            "name = Path(selector).stem\n"
            "Path(target).write_text(f'<testsuite tests=\"1\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\"><testcase name=\"{name}\" "
            "time=\"0.1\" /></testsuite>', encoding='utf-8')\n",
            encoding="utf-8",
        )

        def compile_plan(selector: str, identifier: str) -> dict[str, object]:
            return mcp_server.atomic_task_plan(
                {
                    "project_path": str(self.project),
                    "entrypoints": [
                        {
                            "adapter": "test_suite",
                            "id": identifier,
                            "framework": "pytest",
                            "runner_argv": [
                                str(self.fake_runner),
                                "-m",
                                "pytest",
                            ],
                            "arguments": [selector],
                            "worker_count": 1,
                            "effects_declared_complete": True,
                            "snapshot_paths": [selector],
                            "env": {"PYTEST_ADDOPTS": ""},
                            "junit_path": "shared-results.xml",
                        }
                    ],
                    "max_concurrency": 1,
                }
            )

        async def exercise(
            first_plan: dict[str, object],
            second_plan: dict[str, object],
        ) -> dict[str, object]:
            started = asyncio.Event()

            def progress(item: dict[str, object]) -> None:
                if item.get("running_tasks") == 1:
                    started.set()

            first = asyncio.create_task(
                mcp_server.run_atomic(
                    {
                        "compiled_plan": first_plan,
                        "plan_hash": first_plan["plan_hash"],
                    },
                    progress,
                )
            )
            await asyncio.wait_for(started.wait(), timeout=2)
            with self.assertRaisesRegex(mcp_server.InputError, "already in use"):
                await mcp_server.run_atomic(
                    {
                        "compiled_plan": second_plan,
                        "plan_hash": second_plan["plan_hash"],
                    }
                )
            return await asyncio.wait_for(first, timeout=5)

        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            first_plan = compile_plan("subject.py", "first-suite")
            second_plan = compile_plan("other.py", "second-suite")
            first_result = asyncio.run(exercise(first_plan, second_plan))
            second_result = asyncio.run(
                mcp_server.run_atomic(
                    {
                        "compiled_plan": second_plan,
                        "plan_hash": second_plan["plan_hash"],
                    }
                )
            )

        self.assertEqual(first_result["test_report"]["passed"], 1)
        self.assertEqual(second_result["test_report"]["passed"], 1)
        self.assertIn(
            'name="other"',
            (self.project / "shared-results.xml").read_text(encoding="utf-8"),
        )

    @unittest.skipUnless(os.name == "posix", "POSIX selector symlink contract")
    def test_runtime_rejects_junit_alias_inside_collection_scope(self) -> None:
        tests = self.project / "tests"
        tests.mkdir()
        (tests / "test_victim.py").write_text(
            "def test_victim(): pass\n",
            encoding="utf-8",
        )
        alias_root = self.project / "TESTS"
        if not alias_root.exists():
            alias_root.mkdir()
        plan = mcp_server.atomic_task_plan(
            {
                "project_path": str(self.project),
                "entrypoints": [
                    {
                        "adapter": "test_suite",
                        "id": "output-alias",
                        "framework": "pytest",
                        "runner_argv": [str(self.fake_runner), "-m", "pytest"],
                        "arguments": [str(tests)],
                        "worker_count": 1,
                        "effects_declared_complete": True,
                        "env": {"PYTEST_ADDOPTS": ""},
                        "junit_path": str(self.project / "safe-report.xml"),
                    }
                ],
                "max_concurrency": 1,
            }
        )
        unsafe_report = str(alias_root / "report.xml")
        plan["test_suites"][0]["junit_path"] = unsafe_report
        argv = plan["atoms"][0]["operation"]["argv"]
        plan["atoms"][0]["operation"]["argv"] = [
            f"--junitxml={unsafe_report}"
            if item.startswith("--junitxml=")
            else item
            for item in argv
        ]

        with self.assertRaisesRegex(
            mcp_server.InputError,
            "cannot be revalidated",
        ) as caught:
            mcp_server._test_suite_execution_context(plan)
        self.assertIn("overlaps", str(caught.exception.__cause__))

    def test_runtime_rejects_selector_swapped_to_external_symlink(self) -> None:
        tests = self.project / "tests"
        tests.mkdir()
        with tempfile.TemporaryDirectory(prefix="atomlane-external-tests-") as outside_raw:
            outside = Path(outside_raw).resolve()
            plan = mcp_server.atomic_task_plan(
                {
                    "project_path": str(self.project),
                    "entrypoints": [
                        {
                            "adapter": "test_suite",
                            "id": "selector-swap",
                            "framework": "pytest",
                            "runner_argv": [sys.executable, "-m", "pytest"],
                            "arguments": ["tests"],
                            "worker_count": 1,
                            "effects_declared_complete": True,
                            "env": {"PYTEST_ADDOPTS": ""},
                        }
                    ],
                    "max_concurrency": 1,
                }
            )
            tests.rmdir()
            tests.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(mcp_server.InputError, "cannot be revalidated"):
                asyncio.run(
                    mcp_server.run_atomic(
                        {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                    )
                )

    @unittest.skipUnless(os.name == "posix", "POSIX config-path contract")
    def test_runtime_rejects_config_parent_swapped_to_external_symlink(self) -> None:
        config_dir = self.project / "config"
        config_dir.mkdir()
        config = config_dir / "pytest.ini"
        config.write_text("[pytest]\n", encoding="utf-8")
        selected = self.project / "test_ok.py"
        selected.write_text("def test_ok(): pass\n", encoding="utf-8")
        plan = mcp_server.test_suite_plan(
            {
                "project_path": str(self.project),
                "runner_argv": [sys.executable, "-m", "pytest"],
                "arguments": [str(selected)],
                "config_path": str(config),
                "worker_count": 1,
                "effects_declared_complete": True,
                "snapshot_paths": [str(selected)],
                "baseline_source_closure_declared": True,
                "env": {"PYTEST_ADDOPTS": ""},
                "max_concurrency": 1,
            }
        )
        with tempfile.TemporaryDirectory(prefix="atomlane-external-config-") as outside_raw:
            outside = Path(outside_raw).resolve()
            (outside / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            config_dir.rename(self.project / "original-config")
            config_dir.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(mcp_server.InputError, "config snapshot is invalid"):
                mcp_server._test_suite_execution_context(plan)

    @unittest.skipUnless(os.name == "posix", "POSIX output-path contract")
    def test_runtime_rejects_junit_parent_swapped_to_external_symlink(self) -> None:
        output_dir = self.project / "output"
        output_dir.mkdir()
        plan = mcp_server.atomic_task_plan(
            {
                "project_path": str(self.project),
                "entrypoints": [
                    {
                        "adapter": "test_suite",
                        "id": "output-parent-swap",
                        "framework": "pytest",
                        "runner_argv": [
                            str(self.fake_runner),
                            "-m",
                            "pytest",
                        ],
                        "arguments": ["subject.py"],
                        "worker_count": 1,
                        "effects_declared_complete": True,
                        "snapshot_paths": ["subject.py"],
                        "env": {"PYTEST_ADDOPTS": ""},
                        "junit_path": "output/results.xml",
                    }
                ],
                "max_concurrency": 1,
            }
        )
        with tempfile.TemporaryDirectory(prefix="atomlane-external-output-") as outside:
            output_dir.rename(self.project / "original-output")
            output_dir.symlink_to(Path(outside), target_is_directory=True)

            with self.assertRaisesRegex(
                mcp_server.InputError,
                "output path identity changed",
            ):
                mcp_server._test_suite_execution_context(plan)

    def test_runtime_rejects_junit_hardlink_to_snapshot(self) -> None:
        self.fake_pytest_main.write_text("print('must not execute')\n", encoding="utf-8")
        plan = self._plan(timeout_seconds=5.0, workers=1)
        os.link(self.project / "subject.py", self.project / "results.xml")

        with self.assertRaisesRegex(mcp_server.InputError, "single-link"):
            asyncio.run(
                mcp_server.run_atomic(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )
            )

    def test_junit_parser_rejects_dtd_and_entity_declarations(self) -> None:
        report_path = self.project / "unsafe.xml"
        report_path.write_text(
            "<!DOCTYPE testsuite [<!ENTITY local SYSTEM 'file:///etc/passwd'>]>"
            "<testsuite><testcase name='unsafe'>&local;</testcase></testsuite>",
            encoding="utf-8",
        )

        parsed = mcp_server._parse_junit_report(report_path)

        self.assertEqual(parsed["status"], "unavailable")
        self.assertIn("prohibited", parsed["reason"])

    def test_fresh_junit_report_drives_native_pool_counts_and_savings(self) -> None:
        fake_pytest = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "target = next(a.split('=', 1)[1] for a in sys.argv[1:] "
            "if a.startswith('--junitxml='))\n"
            "cases = ''.join(f'<testcase classname=\"suite\" name=\"case_{i}\" "
            "time=\"0.005\" />' for i in range(100))\n"
            "Path(target).write_text('<testsuites><testsuite tests=\"100\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\">' + cases + '</testsuite></testsuites>', "
            "encoding='utf-8')\n"
            "print('fake pytest completed 100 cases', flush=True)\n"
            "time.sleep(0.3)\n"
        )
        self.fake_pytest_main.write_text(fake_pytest, encoding="utf-8")

        progress: list[dict[str, object]] = []
        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            plan = self._plan(timeout_seconds=5.0, workers=2)
            result = asyncio.run(
                asyncio.wait_for(
                    mcp_server.run_atomic(
                        {"compiled_plan": plan, "plan_hash": plan["plan_hash"]},
                        progress.append,
                    ),
                    timeout=10,
                )
            )

        self.assertEqual(
            result["results"][0]["status"],
            "succeeded",
            result["results"][0].get("stdout", "")
            + result["results"][0].get("stderr", ""),
        )
        self.assertIn("fake pytest completed 100 cases", result["results"][0]["stdout"])
        report = result["test_report"]
        self.assertEqual(report["tests"], 100)
        self.assertEqual(report["passed"], 100)
        self.assertEqual(report["failures"], 0)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(report["skipped"], 0)
        self.assertAlmostEqual(report["testcase_time_sum_seconds"], 0.5, places=5)
        self.assertEqual(report["fresh_report_count"], 1)
        self.assertTrue(report["reports"][0]["fresh"])

        indicator = result["indicator"]
        self.assertTrue(indicator["parallel"])
        self.assertEqual(indicator["parallelism_kind"], "native_worker_pool")
        self.assertEqual(indicator["native_workers_configured"], 2)
        self.assertIsNone(indicator["native_workers_observed"])
        self.assertEqual(
            indicator["speedup_kind"],
            "estimated_sum_of_testcase_durations",
        )
        self.assertTrue(indicator["savings_eligible"])
        self.assertFalse(indicator["ledger_credit_eligible"])
        self.assertIsNone(indicator["measured_time_saved_seconds"])
        self.assertEqual(
            indicator["estimated_time_saved_seconds"],
            indicator["time_saved_seconds"],
        )
        self.assertEqual(indicator["credited_time_saved_seconds"], 0.0)
        self.assertEqual(indicator["cumulative_saved_seconds"], 0.0)
        self.assertEqual(indicator["cumulative_run_count"], 0)
        self.assertEqual(indicator["cumulative_estimated_run_count"], 1)
        self.assertEqual(
            indicator["cumulative_estimated_saved_seconds"],
            indicator["time_saved_seconds"],
        )
        self.assertIn("未入账", indicator["display"])
        # Process startup can legitimately outweigh this deliberately tiny fake
        # workload.  The real 100-case xdist integration below proves positive
        # savings; this unit test only verifies that complete JUnit timings make
        # the result eligible for a bounded, non-negative comparison.
        self.assertGreaterEqual(indicator["time_saved_seconds"], 0)
        self.assertIn("2 workers", indicator["display"])
        self.assertTrue(
            any(item.get("native_workers_configured") == 2 for item in progress),
            progress,
        )

    def test_stale_junit_report_is_not_used_as_savings_evidence(self) -> None:
        (self.project / "results.xml").write_text(
            "<testsuite><testcase name='old' time='999' /></testsuite>",
            encoding="utf-8",
        )
        self.fake_pytest_main.write_text(
            "print('completed without writing junit', flush=True)\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            plan = self._plan(timeout_seconds=5.0, workers=2)
            result = asyncio.run(
                asyncio.wait_for(
                    mcp_server.run_atomic(
                        {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                    ),
                    timeout=10,
                )
            )

        self.assertEqual(result["results"][0]["status"], "succeeded")
        report = result["test_report"]
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["fresh_report_count"], 0)
        self.assertFalse(report["savings_comparison_eligible"])
        self.assertFalse(result["indicator"]["savings_eligible"])
        self.assertEqual(
            result["indicator"]["speedup_kind"],
            "unavailable_native_execution_evidence",
        )

    def test_serial_run_issues_session_attestation_consumed_by_parallel_run(self) -> None:
        fake_pytest = (
            "from pathlib import Path\n"
            "import sys, time\n"
            "target = next(a.split('=', 1)[1] for a in sys.argv[1:] "
            "if a.startswith('--junitxml='))\n"
            "cases = ''.join(f'<testcase classname=\"suite\" name=\"case_{i}\" "
            "time=\"0.002\" />' for i in range(20))\n"
            "Path(target).write_text('<testsuite tests=\"20\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\">' + cases + '</testsuite>', encoding='utf-8')\n"
            "time.sleep(0.06)\n"
        )
        self.fake_pytest_main.write_text(fake_pytest, encoding="utf-8")
        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            serial_plan = self._plan(timeout_seconds=5.0, workers=1)
            serial = asyncio.run(
                mcp_server.run_atomic(
                    {
                        "compiled_plan": serial_plan,
                        "plan_hash": serial_plan["plan_hash"],
                    }
                )
            )
            evidence = serial["serial_baseline_evidence"]
            registered = mcp_server._SERIAL_BASELINE_ATTESTATIONS[
                evidence["attestation_id"]
            ]
            self.assertIsNot(registered, evidence)
            self.assertIsNot(
                registered["suite_fingerprints"], evidence["suite_fingerprints"]
            )
            parallel_plan = self._plan(timeout_seconds=5.0, workers=2)
            parallel = asyncio.run(
                mcp_server.run_atomic(
                    {
                        "compiled_plan": parallel_plan,
                        "plan_hash": parallel_plan["plan_hash"],
                        "serial_baseline_evidence": evidence,
                    }
                )
            )

        self.assertEqual(parallel["indicator"]["speedup_kind"], "measured_serial_baseline")
        self.assertTrue(parallel["indicator"]["serial_baseline_compatible"])
        self.assertEqual(evidence["test_count"], 20)
        self.assertEqual(evidence["passed_count"], 20)
        self.assertEqual(evidence["skipped_count"], 0)

    @unittest.skipUnless(os.name == "posix", "requires symbolic links")
    def test_collection_link_retarget_cannot_reuse_serial_attestation(self) -> None:
        tests = self.project / "tests"
        tests.mkdir()
        selected = tests / "test_case.py"
        selected.write_text("def test_case(): pass\n", encoding="utf-8")
        self.fake_pytest_main.write_text(
            "from pathlib import Path\n"
            "import sys\n"
            "target = next(a.split('=', 1)[1] for a in sys.argv[1:] "
            "if a.startswith('--junitxml='))\n"
            "Path(target).write_text('<testsuite tests=\"1\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\"><testcase classname=\"suite\" "
            "name=\"case\" time=\"0.01\" /></testsuite>', encoding='utf-8')\n",
            encoding="utf-8",
        )

        def plan_for(workers: int, snapshot: Path, report_name: str) -> dict[str, object]:
            return mcp_server.atomic_task_plan(
                {
                    "project_path": str(self.project),
                    "entrypoints": [
                        {
                            "adapter": "test_suite",
                            "id": "link-retarget",
                            "framework": "pytest",
                            "runner_argv": [str(self.fake_runner), "-m", "pytest"],
                            "arguments": ["tests"],
                            "worker_count": workers,
                            "independence_declared": workers > 1,
                            "effects_declared_complete": True,
                            "snapshot_paths": [str(snapshot)],
                            "baseline_source_closure_declared": True,
                            "env": {"PYTEST_ADDOPTS": ""},
                            "junit_path": report_name,
                        }
                    ],
                    "max_concurrency": 2,
                    "reserve_cores": 0,
                }
            )

        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            serial_plan = plan_for(1, selected, "serial-link.xml")
            serial = asyncio.run(
                mcp_server.run_atomic(
                    {
                        "compiled_plan": serial_plan,
                        "plan_hash": serial_plan["plan_hash"],
                    }
                )
            )
            evidence = serial["serial_baseline_evidence"]

            replacement = tests / "fast_implementation.py"
            replacement.write_bytes(selected.read_bytes())
            selected.unlink()
            selected.symlink_to(replacement)
            parallel_plan = plan_for(2, replacement, "parallel-link.xml")
            self.assertFalse(
                parallel_plan["test_suites"][0]["baseline_source_coverage"]
            )
            with self.assertRaisesRegex(
                mcp_server.InputError,
                "does not match the compiled pytest selection",
            ):
                asyncio.run(
                    mcp_server.run_atomic(
                        {
                            "compiled_plan": parallel_plan,
                            "plan_hash": parallel_plan["plan_hash"],
                            "serial_baseline_evidence": evidence,
                        }
                    )
                )

    def test_native_pool_rejects_unattested_numeric_or_forged_baseline(self) -> None:
        self.fake_pytest_main.write_text("print('unused')\n", encoding="utf-8")
        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            plan = self._plan(timeout_seconds=5.0, workers=2)
            with self.assertRaisesRegex(mcp_server.InputError, "serial_baseline_evidence"):
                asyncio.run(
                    mcp_server.run_atomic(
                        {
                            "compiled_plan": plan,
                            "plan_hash": plan["plan_hash"],
                            "serial_baseline_seconds": 999.0,
                        }
                    )
                )
            forged = {
                "schema": "atomlane/serial-test-baseline/v1",
                "attestation_id": "baseline_" + "0" * 64,
                "source_plan_hash": "sha256:" + "1" * 64,
                "elapsed_seconds": 999.0,
                "suite_fingerprints": [
                    plan["test_suites"][0]["selection_fingerprint"]
                ],
                "test_count": 100,
                "passed_count": 100,
                "skipped_count": 0,
                "case_set_sha256": "sha256:" + "2" * 64,
                "status": "passed",
            }
            with self.assertRaisesRegex(mcp_server.InputError, "not issued"):
                asyncio.run(
                    mcp_server.run_atomic(
                        {
                            "compiled_plan": plan,
                            "plan_hash": plan["plan_hash"],
                            "serial_baseline_evidence": forged,
                        }
                    )
                )

    def test_serial_run_without_explicit_source_snapshot_issues_no_attestation(self) -> None:
        fake_pytest = (
            "from pathlib import Path\n"
            "import sys\n"
            "target = next(a.split('=', 1)[1] for a in sys.argv[1:] "
            "if a.startswith('--junitxml='))\n"
            "Path(target).write_text('<testsuite tests=\"1\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\"><testcase classname=\"suite\" "
            "name=\"case\" time=\"0.001\" /></testsuite>', encoding='utf-8')\n"
        )
        self.fake_pytest_main.write_text(fake_pytest, encoding="utf-8")
        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            plan = mcp_server.atomic_task_plan(
                {
                    "project_path": str(self.project),
                    "entrypoints": [
                        {
                            "adapter": "test_suite",
                            "id": "unsnapshotted-pytest",
                            "framework": "pytest",
                            "runner_argv": [str(self.fake_runner), "-m", "pytest"],
                            "arguments": ["subject.py"],
                            "worker_count": 1,
                            "case_count_hint": 1,
                            "timeout_seconds": 5.0,
                            "effects_declared_complete": True,
                            "env": {"PYTEST_ADDOPTS": ""},
                            "junit_path": "unsnapshotted-results.xml",
                        }
                    ],
                    "max_concurrency": 1,
                }
            )
            result = asyncio.run(
                mcp_server.run_atomic(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )
            )

        self.assertEqual(result["results"][0]["status"], "succeeded")
        self.assertNotIn("serial_baseline_evidence", result)

    def test_impossible_or_counter_inconsistent_junit_cannot_credit_savings(self) -> None:
        oversized_count = mcp_server._parse_junit_payload(
            b'<testsuite tests="'
            + b"9" * 4301
            + b'" failures="0" errors="0" skipped="0"/>'
        )
        self.assertFalse(oversized_count["counter_consistent"])

        parsed = mcp_server._parse_junit_payload(
            b'<testsuite tests="0" failures="0" errors="0" skipped="0">'
            b'<testcase classname="suite" name="not-run" time="0" />'
            b'</testsuite>'
        )
        self.assertFalse(parsed["counter_consistent"])

        fake_pytest = (
            "from pathlib import Path\n"
            "import sys\n"
            "target = next(a.split('=', 1)[1] for a in sys.argv[1:] "
            "if a.startswith('--junitxml='))\n"
            "Path(target).write_text('<testsuite tests=\"1\" failures=\"0\" "
            "errors=\"0\" skipped=\"0\"><testcase classname=\"suite\" name=\"fake\" "
            "time=\"10\" /></testsuite>', encoding='utf-8')\n"
        )
        self.fake_pytest_main.write_text(fake_pytest, encoding="utf-8")
        with mock.patch.object(
            mcp_server,
            "concurrency_plan",
            side_effect=self._resource_plan,
        ):
            plan = self._plan(timeout_seconds=5.0, workers=2)
            result = asyncio.run(
                mcp_server.run_atomic(
                    {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                )
            )

        self.assertFalse(result["test_report"]["timing_plausible"])
        self.assertFalse(result["indicator"]["savings_eligible"])

    def test_junit_utf16_and_symlink_inputs_fail_closed(self) -> None:
        utf16 = "<!DOCTYPE testsuite><testsuite tests='0' failures='0' errors='0' skipped='0'/>".encode(
            "utf-16"
        )
        self.assertEqual(mcp_server._parse_junit_payload(utf16)["status"], "unavailable")
        if os.name == "posix":
            target = self.project / "target.xml"
            target.write_text(
                '<testsuite tests="0" failures="0" errors="0" skipped="0"/>',
                encoding="utf-8",
            )
            link = self.project / "link.xml"
            link.symlink_to(target)
            parsed = mcp_server._parse_junit_report(link)
            self.assertEqual(parsed["status"], "unavailable")
            self.assertIn("non-link regular file", parsed["reason"])
            hardlink = self.project / "hardlink.xml"
            os.link(target, hardlink)
            parsed = mcp_server._parse_junit_report(hardlink)
            self.assertEqual(parsed["status"], "unavailable")
            self.assertIn("single-link", parsed["reason"])

    def test_junit_actual_testcase_count_is_memory_bounded(self) -> None:
        payload = (
            b'<testsuite tests="3" failures="0" errors="0" skipped="0">'
            b'<testcase name="a"/><testcase name="b"/><testcase name="c"/>'
            b"</testsuite>"
        )
        with mock.patch.object(mcp_server, "MAX_JUNIT_TEST_CASES", 2):
            parsed = mcp_server._parse_junit_payload(payload)
        self.assertEqual(parsed["status"], "unavailable")
        self.assertIn("testcase limit", parsed["reason"])

        with mock.patch.object(mcp_server, "MAX_JUNIT_XML_ELEMENTS", 2):
            parsed = mcp_server._parse_junit_payload(
                b"<testsuite><metadata><value/></metadata></testsuite>"
            )
        self.assertEqual(parsed["status"], "unavailable")
        self.assertIn("XML element limit", parsed["reason"])

        parsed = mcp_server._parse_junit_payload(b"<not-junit><testcase/></not-junit>")
        self.assertEqual(parsed["status"], "unavailable")
        self.assertIn("root", parsed["reason"])

        parsed = mcp_server._parse_junit_payload(
            b'<testsuites tests="1" failures="0" errors="0" skipped="0">'
            b'<testcase name="outside-suite"/></testsuites>'
        )
        self.assertEqual(parsed["status"], "unavailable")
        self.assertIn("contained", parsed["reason"])

    def test_metrics_ledger_failure_never_discards_execution_result(self) -> None:
        results = [{"id": "ok", "status": "succeeded", "duration_seconds": 1.0}]
        with mock.patch.object(mcp_server, "_record_time_saved", side_effect=OSError("locked")):
            indicator = mcp_server._execution_indicator(results, 0.5, 1, 1.0)
        self.assertTrue(indicator["savings_eligible"])
        self.assertTrue(indicator["ledger_credit_eligible"])
        self.assertFalse(indicator["ledger_credit_recorded"])
        self.assertEqual(indicator["credited_time_saved_seconds"], 0.0)
        self.assertFalse(indicator["cumulative_ledger_available"])
        self.assertIsNone(indicator["cumulative_saved_seconds"])
        self.assertIsNone(indicator["cumulative_measured_saved_seconds"])
        self.assertIsNone(indicator["cumulative_estimated_saved_seconds"])
        self.assertIn("累计已入账 不可用", indicator["display"])

    def test_v1_savings_ledger_migrates_without_claiming_measurement(self) -> None:
        self.stats_path.write_text(
            json.dumps(
                {
                    "run_count": 3,
                    "cumulative_saved_seconds": 12.5,
                    "updated_at_epoch_seconds": 100.0,
                }
            ),
            encoding="utf-8",
        )

        migrated = mcp_server._read_time_saved()
        self.assertEqual(migrated["schema"], "atomlane/savings-ledger/v2")
        self.assertEqual(migrated["run_count"], 3)
        self.assertEqual(migrated["cumulative_saved_seconds"], 12.5)
        self.assertEqual(migrated["legacy_unclassified_run_count"], 3)
        self.assertEqual(
            migrated["cumulative_legacy_unclassified_saved_seconds"],
            12.5,
        )
        self.assertEqual(migrated["measured_run_count"], 0)
        self.assertEqual(migrated["estimated_run_count"], 0)

        recorded = mcp_server._record_time_saved(2.0, evidence_kind="estimated")
        self.assertEqual(recorded["run_count"], 3)
        self.assertEqual(recorded["cumulative_saved_seconds"], 12.5)
        self.assertEqual(recorded["estimated_run_count"], 1)
        self.assertEqual(recorded["cumulative_estimated_saved_seconds"], 2.0)

    def test_invalid_existing_ledger_is_never_overwritten(self) -> None:
        valid_v2 = {
            "schema": "atomlane/savings-ledger/v2",
            "run_count": 2,
            "cumulative_saved_seconds": 4.0,
            "measured_run_count": 2,
            "cumulative_measured_saved_seconds": 4.0,
            "estimated_run_count": 1,
            "cumulative_estimated_saved_seconds": 3.0,
            "legacy_unclassified_run_count": 0,
            "cumulative_legacy_unclassified_saved_seconds": 0.0,
            "updated_at_epoch_seconds": 100.0,
        }
        invalid_documents = [
            b'{"schema":"atomlane/savings-ledger/v2","run_count":',
            json.dumps(
                {
                    **valid_v2,
                    "cumulative_measured_saved_seconds": "bad",
                }
            ).encode(),
            json.dumps({**valid_v2, "schema": "unknown/v3"}).encode(),
            json.dumps({**valid_v2, "cumulative_saved_seconds": 99.0}).encode(),
        ]
        for invalid in invalid_documents:
            with self.subTest(invalid=invalid[:80]):
                self.stats_path.write_bytes(invalid)
                with self.assertRaisesRegex(OSError, "invalid or unreadable"):
                    mcp_server._record_time_saved(3.0)
                self.assertEqual(self.stats_path.read_bytes(), invalid)

    def test_native_savings_are_not_credited_for_a_mixed_workload(self) -> None:
        indicator = mcp_server._execution_indicator(
            [
                {"id": "pytest", "status": "succeeded", "duration_seconds": 0.5},
                {"id": "other", "status": "succeeded", "duration_seconds": 4.0},
            ],
            elapsed=4.0,
            peak_concurrency=2,
            serial_baseline_seconds=10.0,
            execution_context={
                "native_workers_configured": 4,
                "native_worker_pool_count": 1,
                "test_workload_exclusive": False,
                "serial_baseline_evidence": {"test_count": 1},
                "test_report": {
                    "execution_evidence_eligible": True,
                    "tests": 1,
                    "passed": 1,
                    "skipped": 0,
                    "case_set_sha256": "sha256:" + "1" * 64,
                    "savings_comparison_eligible": True,
                    "testcase_time_sum_seconds": 10.0,
                },
            },
        )

        self.assertFalse(indicator["savings_eligible"])
        self.assertEqual(indicator["speedup_kind"], "unavailable_mixed_native_workload")
        self.assertEqual(indicator["time_saved_seconds"], 0.0)

    def test_mixed_workload_junit_is_not_attributed_as_execution_evidence(self) -> None:
        report_path = self.project / "mixed.xml"
        before = mcp_server._junit_report_state(report_path)
        report_path.write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            '<testcase name="forged" time="0.1"/></testsuite>',
            encoding="utf-8",
        )
        report = mcp_server._collect_test_report(
            {
                "test_workload_exclusive": False,
                "test_suites": [
                    {
                        "id": "suite",
                        "atom_id": "pytest",
                        "junit_path": str(report_path),
                        "configured_workers": 2,
                    }
                ],
            },
            {str(report_path): before},
            [{"id": "pytest", "status": "succeeded", "duration_seconds": 0.2}],
        )

        self.assertIsNotNone(report)
        self.assertFalse(report["execution_evidence_eligible"])
        self.assertEqual(report["report_attribution"], "unavailable_mixed_workload")
        self.assertFalse(report["reports"][0]["attributed_to_suite"])


@unittest.skipUnless(
    importlib.util.find_spec("xdist") is not None,
    "pytest-xdist is an optional runtime dependency",
)
class RealPytestXdistIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stats_temporary = tempfile.TemporaryDirectory(
            prefix="atomlane-real-xdist-stats-"
        )
        self.stats_path = Path(self.stats_temporary.name) / "stats.json"
        self.environment = mock.patch.dict(
            os.environ,
            {"ATOMLANE_STATS_PATH": str(self.stats_path), "PYTEST_ADDOPTS": ""},
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.stats_temporary.cleanup()

    @staticmethod
    def _resource_plan(
        profile: str,
        requested: int | None = None,
        reserve_cores: int | None = None,
        estimated_memory_mb_per_task: float | None = None,
        responsiveness: str = "interactive",
    ) -> dict[str, object]:
        return {
            "profile": profile,
            "responsiveness": responsiveness,
            "recommended_concurrency": 4,
            "chosen_concurrency": min(requested or 4, 4),
            "reserve_cores": reserve_cores or 0,
            "reserve_cores_source": "explicit",
            "nice_adjustment": 0,
            "qos_clamp": None,
            "estimated_memory_mb_per_task": estimated_memory_mb_per_task,
            "memory_limited_concurrency": None,
            "reasons": ["deterministic integration-test envelope"],
            "machine": {"memory_available_bytes_approx": 8 * 1024**3},
        }

    def test_one_native_pool_runs_one_hundred_independent_cases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-real-xdist-") as temporary:
            project = Path(temporary).resolve()
            # Some hermetic macOS runners ship a broken optional readline
            # extension. Pytest treats ImportError as supported absence, so a
            # local sentinel keeps this integration focused on xdist itself.
            readline_sentinel = project / "readline.py"
            readline_sentinel.write_text(
                "raise ImportError('readline intentionally unavailable in fixture')\n",
                encoding="utf-8",
            )
            test_file = project / "test_parallel_cases.py"
            test_file.write_text(
                "import time\n"
                "import pytest\n\n"
                "@pytest.mark.parametrize('case_id', range(100))\n"
                "def test_independent_case(case_id, tmp_path, worker_id):\n"
                "    time.sleep(0.05)\n"
                "    assert worker_id == 'master' or worker_id.startswith('gw')\n"
                "    marker = tmp_path / f'{case_id}.txt'\n"
                "    marker.write_text(str(case_id), encoding='utf-8')\n"
                "    assert marker.read_text(encoding='utf-8') == str(case_id)\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                mcp_server,
                "concurrency_plan",
                side_effect=self._resource_plan,
            ):
                common = {
                    "project_path": str(project),
                    "runner_argv": [sys.executable, "-m", "pytest"],
                    "arguments": ["-q", str(test_file)],
                    "case_count_hint": 100,
                    "timeout_seconds": 30,
                    "snapshot_paths": [str(test_file), str(readline_sentinel)],
                    "baseline_source_closure_declared": True,
                    "effects_declared_complete": True,
                    "env": {
                        "PYTEST_ADDOPTS": "",
                        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    },
                    "max_concurrency": 4,
                    "reserve_cores": 0,
                }
                serial_plan = mcp_server.test_suite_plan(
                    {
                        **common,
                        "worker_count": 1,
                        "junit_path": str(project / "serial-junit.xml"),
                    }
                )
                serial_result = asyncio.run(
                    mcp_server.run_atomic(
                        {
                            "compiled_plan": serial_plan,
                            "plan_hash": serial_plan["plan_hash"],
                        }
                    )
                )
                evidence = serial_result["serial_baseline_evidence"]
                plan = mcp_server.test_suite_plan(
                    {
                        **common,
                        "worker_count": 4,
                        "independence_declared": True,
                        "junit_path": str(project / "parallel-junit.xml"),
                    }
                )
                result = asyncio.run(
                    mcp_server.run_atomic(
                        {
                            "compiled_plan": plan,
                            "plan_hash": plan["plan_hash"],
                            "serial_baseline_evidence": evidence,
                        }
                    )
                )

        self.assertEqual(
            result["results"][0]["status"],
            "succeeded",
            result["results"][0].get("stdout", "")
            + result["results"][0].get("stderr", ""),
        )
        self.assertEqual(result["test_report"]["tests"], 100)
        self.assertEqual(result["test_report"]["passed"], 100)
        self.assertEqual(result["test_report"]["failures"], 0)
        self.assertEqual(result["indicator"]["native_workers_configured"], 4)
        self.assertEqual(plan["test_suites"][0]["distribution"], "worksteal")
        self.assertEqual(result["indicator"]["speedup_kind"], "measured_serial_baseline")
        self.assertGreater(result["indicator"]["time_saved_seconds"], 0)
        ledger = mcp_server._read_savings_stats_document(self.stats_path)
        self.assertEqual(ledger["run_count"], 1)
        self.assertEqual(ledger["measured_run_count"], 1)
        self.assertEqual(ledger["estimated_run_count"], 1)
        self.assertAlmostEqual(
            ledger["cumulative_saved_seconds"],
            result["indicator"]["time_saved_seconds"],
            places=5,
        )

    def test_xdist_loads_with_and_without_plugin_autoload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-xdist-autoload-") as temporary:
            project = Path(temporary).resolve()
            readline_sentinel = project / "readline.py"
            readline_sentinel.write_text(
                "raise ImportError('readline intentionally unavailable in fixture')\n",
                encoding="utf-8",
            )
            test_file = project / "test_smoke.py"
            test_file.write_text("def test_smoke(): assert True\n", encoding="utf-8")
            with mock.patch.object(
                mcp_server,
                "concurrency_plan",
                side_effect=self._resource_plan,
            ):
                for disabled in (False, True):
                    plan = mcp_server.test_suite_plan(
                        {
                            "project_path": str(project),
                            "runner_argv": [sys.executable, "-m", "pytest"],
                            "arguments": [str(test_file)],
                            "worker_count": 2,
                            "distribution": "load",
                            "snapshot_paths": [str(test_file), str(readline_sentinel)],
                            "effects_declared_complete": True,
                            "independence_declared": True,
                            "env": {
                                "PYTEST_ADDOPTS": "",
                                "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1" if disabled else "",
                            },
                            "junit_path": str(
                                project / f"autoload-{int(disabled)}.xml"
                            ),
                            "max_concurrency": 2,
                            "reserve_cores": 0,
                        }
                    )
                    result = asyncio.run(
                        mcp_server.run_atomic(
                            {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                        )
                    )
                    with self.subTest(disabled=disabled):
                        self.assertEqual(
                            result["results"][0]["status"],
                            "succeeded",
                            result["results"][0].get("stderr", ""),
                        )
                        self.assertEqual(result["test_report"]["passed"], 1)

    def test_pytest_8_4_plain_pyproject_fallback_executes(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="atomlane-real-pyproject-fallback-"
        ) as temporary:
            project = Path(temporary).resolve()
            config = project / "pyproject.toml"
            config.write_text(
                "[project]\nname = 'fallback-fixture'\nversion = '0.0.0'\n",
                encoding="utf-8",
            )
            readline_sentinel = project / "readline.py"
            readline_sentinel.write_text(
                "raise ImportError('readline intentionally unavailable in fixture')\n",
                encoding="utf-8",
            )
            test_file = project / "test_fallback.py"
            test_file.write_text("def test_fallback(): pass\n", encoding="utf-8")
            with mock.patch.object(
                mcp_server,
                "concurrency_plan",
                side_effect=self._resource_plan,
            ):
                plan = mcp_server.test_suite_plan(
                    {
                        "project_path": str(project),
                        "runner_argv": [sys.executable, "-m", "pytest"],
                        "arguments": [str(test_file)],
                        "worker_count": 1,
                        "snapshot_paths": [str(test_file), str(readline_sentinel)],
                        "baseline_source_closure_declared": True,
                        "effects_declared_complete": True,
                        "env": {
                            "PYTEST_ADDOPTS": "",
                            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                        },
                        "junit_path": str(project / "fallback-junit.xml"),
                        "max_concurrency": 1,
                        "reserve_cores": 0,
                    }
                )
                result = asyncio.run(
                    mcp_server.run_atomic(
                        {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                    )
                )

        contract = plan["test_suites"][0]["selection_contract"]
        self.assertEqual(contract["config_selection_kind"], "fallback_pyproject")
        self.assertEqual(contract["config_path"], str(config))
        self.assertEqual(
            result["results"][0]["status"],
            "succeeded",
            result["results"][0].get("stdout", "")
            + result["results"][0].get("stderr", ""),
        )
        self.assertEqual(result["test_report"]["passed"], 1)

    def test_project_boundary_blocks_parent_conftest_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="atomlane-parent-conftest-") as temporary:
            parent = Path(temporary).resolve()
            project = parent / "project"
            project.mkdir()
            (parent / "conftest.py").write_text(
                "def pytest_configure(config):\n"
                "    raise RuntimeError('external conftest executed')\n",
                encoding="utf-8",
            )
            test_file = project / "test_inside_project.py"
            test_file.write_text("def test_inside_project(): pass\n", encoding="utf-8")
            readline_sentinel = project / "readline.py"
            readline_sentinel.write_text(
                "raise ImportError('readline intentionally unavailable in fixture')\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                mcp_server,
                "concurrency_plan",
                side_effect=self._resource_plan,
            ):
                plan = mcp_server.test_suite_plan(
                    {
                        "project_path": str(project),
                        "runner_argv": [sys.executable, "-m", "pytest"],
                        "arguments": [str(test_file)],
                        "worker_count": 1,
                        "snapshot_paths": [str(test_file), str(readline_sentinel)],
                        "effects_declared_complete": True,
                        "env": {
                            "PYTEST_ADDOPTS": "",
                            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                        },
                        "junit_path": str(parent / "outside-collection-junit.xml"),
                        "max_concurrency": 1,
                        "reserve_cores": 0,
                    }
                )
                result = asyncio.run(
                    mcp_server.run_atomic(
                        {"compiled_plan": plan, "plan_hash": plan["plan_hash"]}
                    )
                )

        self.assertEqual(
            result["results"][0]["status"],
            "succeeded",
            result["results"][0].get("stdout", "")
            + result["results"][0].get("stderr", ""),
        )
        self.assertEqual(result["test_report"]["passed"], 1)


if __name__ == "__main__":
    unittest.main()
