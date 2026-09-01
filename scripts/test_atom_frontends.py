#!/usr/bin/env python3
"""Focused regression tests for the fail-closed Atom IR frontends."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import atom_frontends
from atom_engine import AtomError, validate_atoms
from atom_frontends import Compilation, compile_entrypoints, compile_shell


class ShellFrontendTests(unittest.TestCase):
    def test_native_windows_gate_is_separate_from_portable_parser_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            with self.assertRaisesRegex(AtomError, "Windows Preview"):
                compile_entrypoints(
                    project,
                    [{"adapter": "shell", "command": "printf safe"}],
                    target_os="nt",
                )
            with self.assertRaisesRegex(AtomError, "target_os"):
                compile_entrypoints(project, [], target_os="windows")

    def test_exact_success_and_order_edges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            compilation = Compilation(project)
            fragment = compile_shell(
                compilation,
                "printf a && printf b; printf c",
                prefix="shell",
                cwd=project,
                source="task_plan",
                symbol="shell",
            )
            self.assertEqual(len(fragment.atoms), 3)
            atoms = compilation.atoms
            self.assertEqual(atoms[1]["dependencies"], [{"atom": atoms[0]["id"], "kind": "success"}])
            self.assertEqual(atoms[2]["dependencies"], [{"atom": atoms[1]["id"], "kind": "order"}])
            self.assertTrue(all(atom["assurance"]["parse"] == "exact" for atom in atoms))
            self.assertEqual(len(validate_atoms(atoms, project)), 3)

            terminal_chain = Compilation(project)
            terminal_fragment = compile_shell(
                terminal_chain,
                "printf a && printf b",
                prefix="terminal-chain",
                cwd=project,
                source="task_plan",
                symbol="terminal-chain",
            )
            self.assertEqual(len(terminal_fragment.atoms), 2)
            self.assertEqual(
                terminal_chain.atoms[1]["dependencies"],
                [{"atom": terminal_chain.atoms[0]["id"], "kind": "success"}],
            )

    def test_unsupported_shell_regions_are_one_opaque_island(self) -> None:
        cases = (
            ("printf a | cat", "UNSUPPORTED_PIPELINE_OR_OR_LIST"),
            ("printf a || printf b", "UNSUPPORTED_PIPELINE_OR_OR_LIST"),
            ('printf "%s" "$(touch marker)"; printf unsafe', "UNSUPPORTED_DYNAMIC_SHELL"),
            ("(printf a; printf b)", "UNSUPPORTED_CONTROL_STRUCTURE"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            for index, (command, diagnostic) in enumerate(cases):
                compilation = Compilation(project)
                fragment = compile_shell(
                    compilation,
                    command,
                    prefix=f"opaque-{index}",
                    cwd=project,
                    source="task_plan",
                    symbol=f"opaque-{index}",
                )
                self.assertEqual(len(fragment.atoms), 1)
                self.assertEqual(compilation.atoms[0]["operation"]["kind"], "opaque")
                self.assertEqual(compilation.atoms[0]["assurance"]["effects"], "unknown")
                self.assertIn(diagnostic, {item["code"] for item in compilation.diagnostics})
            self.assertFalse((project / "marker").exists())

    def test_literal_inner_worker_count_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            result = compile_entrypoints(
                project,
                [{"adapter": "shell", "id": "tests", "command": "pytest -n 4 tests"}],
                target_os="posix",
            )
            self.assertEqual(
                result["atoms"][0]["operation"]["internal_parallelism"],
                {"kind": "bounded", "tokens": 4},
            )


class PackageFrontendTests(unittest.TestCase):
    def _package(self, root: Path) -> Path:
        package = root / "package.json"
        package.write_text(
            json.dumps(
                {
                    "packageManager": "npm@10.8.0",
                    "scripts": {
                        "ci": "npm test",
                        "ci-with-args": "npm test -- --reporter=dot",
                        "pretest": "npm run prepare-test",
                        "prepare-test": "printf prepare",
                        "test": "npm run unit",
                        "unit": "vitest run",
                        "posttest": "printf post",
                        "cycle:a": "npm run cycle:b",
                        "cycle:b": "npm run cycle:a",
                    },
                }
            ),
            encoding="utf-8",
        )
        return package

    def test_npm_test_lifecycle_and_recursive_expansion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            package = self._package(project)
            result = compile_entrypoints(
                project,
                [{"adapter": "package_script", "package_json": str(package), "script": "ci"}],
                target_os="posix",
            )
            symbols = [atom["provenance"]["symbol"] for atom in result["atoms"]]
            self.assertEqual(
                symbols,
                ["scripts.prepare-test", "scripts.unit", "scripts.posttest"],
            )
            atoms = result["atoms"]
            self.assertEqual(atoms[1]["dependencies"], [{"atom": atoms[0]["id"], "kind": "success"}])
            self.assertEqual(atoms[2]["dependencies"], [{"atom": atoms[1]["id"], "kind": "success"}])
            self.assertEqual(
                atoms[1]["operation"]["internal_parallelism"],
                {"kind": "native_scheduler", "tokens": None},
            )
            self.assertEqual(len(validate_atoms(atoms, project)), 3)

            forwarded = compile_entrypoints(
                project,
                [{"adapter": "package_script", "package_json": str(package), "script": "ci-with-args"}],
                target_os="posix",
            )
            test_atom = next(
                atom for atom in forwarded["atoms"]
                if atom["provenance"]["symbol"] == "scripts.test"
            )
            # npm forwards arguments to the selected script only. Once that
            # body becomes a second npm invocation without its own `--`, the
            # frontend stops rather than inventing transitive forwarding.
            self.assertEqual(
                test_atom["operation"]["argv"],
                ["npm", "run", "unit", "--reporter=dot"],
            )

    def test_recursive_cycle_collapses_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            package = self._package(project)
            result = compile_entrypoints(
                project,
                [{"adapter": "package_script", "package_json": str(package), "script": "cycle:a"}],
                target_os="posix",
            )
            self.assertEqual(len(result["atoms"]), 1)
            atom = result["atoms"][0]
            self.assertEqual(atom["operation"]["kind"], "opaque")
            self.assertIn("CYCLE_COLLAPSED", atom["assurance"]["blockers"])
            self.assertIn("CYCLE_COLLAPSED", {item["code"] for item in result["diagnostics"]})
            self.assertEqual(len(validate_atoms(result["atoms"], project)), 1)


class MakeFrontendTests(unittest.TestCase):
    def test_static_discovery_never_executes_shell_and_recipe_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            marker = project / "make-parser-must-not-run"
            makefile = project / "Makefile"
            makefile.write_text(
                "\n".join(
                    [
                        f"SNEAK := $(shell touch {marker})",
                        ".PHONY: all prep",
                        "all: prep",
                        "\t@echo first",
                        "\t@echo second",
                        "prep:",
                        "\t@printf prep",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with mock.patch("atom_frontends.subprocess.run", side_effect=AssertionError("Make discovery executed a process")):
                result = compile_entrypoints(
                    project,
                    [{"adapter": "make_target", "makefile": str(makefile), "target": "all"}],
                    target_os="posix",
                )
            self.assertFalse(marker.exists())
            by_symbol = {atom["provenance"]["symbol"]: atom for atom in result["atoms"]}
            self.assertEqual(set(by_symbol), {"all", "prep"})
            self.assertEqual(by_symbol["all"]["operation"]["kind"], "make_recipe")
            self.assertEqual(by_symbol["all"]["operation"]["command"], "@echo first\n@echo second")
            self.assertFalse(by_symbol["all"]["semantics"]["splittable"])
            self.assertEqual(
                by_symbol["all"]["operation"]["internal_parallelism"],
                {"kind": "native_scheduler", "tokens": None},
            )
            self.assertEqual(len(result["native_delegates"]), 1)
            self.assertEqual(result["native_delegates"][0]["kind"], "make_native_graph")
            self.assertIn("-f", result["native_delegates"][0]["argv"])
            self.assertEqual(len(validate_atoms(result["atoms"], project)), 2)

    def test_python_argparse_dataflow_repairs_sibling_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "jobs.py").write_text(
                "from pathlib import Path\n"
                "import argparse\n"
                "def main():\n"
                "    p = argparse.ArgumentParser()\n"
                "    s = p.add_subparsers(dest='command', required=True)\n"
                "    produce = s.add_parser('produce')\n"
                "    produce.add_argument('--output', type=Path, default=Path('result.json'))\n"
                "    consume = s.add_parser('consume')\n"
                "    consume.add_argument('--input', type=Path, default=Path('result.json'))\n"
                "    args = p.parse_args()\n"
                "    if args.command == 'produce':\n"
                "        args.output.write_text('{}')\n"
                "    else:\n"
                "        args.input.read_text()\n"
                "if __name__ == '__main__':\n"
                "    main()\n",
                encoding="utf-8",
            )
            (project / "Makefile").write_text(
                "all: produce consume\n\n"
                "produce:\n\tpython3 jobs.py produce\n\n"
                "consume:\n\tpython3 jobs.py consume\n",
                encoding="utf-8",
            )
            result = compile_entrypoints(
                project,
                [{"adapter": "make_target", "makefile": "Makefile", "target": "all"}],
                target_os="posix",
            )
            by_symbol = {atom["provenance"]["symbol"]: atom for atom in result["atoms"]}
            self.assertIn(
                {"atom": by_symbol["produce"]["id"], "kind": "data"},
                by_symbol["consume"]["dependencies"],
            )
            self.assertIn(
                "MAKE_DATAFLOW_REPAIRED",
                {item["code"] for item in result["diagnostics"]},
            )


@unittest.skipUnless(shutil.which("ruby"), "safe Compose YAML frontend requires system Ruby")
class ComposeFrontendTests(unittest.TestCase):
    def test_safe_yaml_parser_pins_utf8_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "compose.yml"
            compose.write_text("name: 研究\nservices: {}\n", encoding="utf-8")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"name":"研究","name_interpolation":false,"services":{}}',
                stderr="",
            )
            with mock.patch.object(
                atom_frontends.subprocess, "run", return_value=completed
            ) as run:
                payload = atom_frontends._parse_compose_with_safe_yaml(compose)

        self.assertEqual(payload["name"], "研究")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "strict")
        self.assertIn("研究", run.call_args.kwargs["input"])

    def test_safe_ruby_environment_is_minimal_and_platform_native(self) -> None:
        with (
            mock.patch.object(atom_frontends.os, "name", "nt"),
            mock.patch.dict(
                atom_frontends.os.environ,
                {
                    "SystemRoot": r"C:\Windows",
                    "TEMP": r"C:\Temp",
                    "RUBYOPT": "-rhostile",
                    "RUBYLIB": r"C:\hostile",
                    "GEM_HOME": r"C:\hostile-gems",
                },
                clear=True,
            ),
        ):
            windows_environment = atom_frontends._safe_ruby_environment(
                r"C:\Ruby\bin\ruby.exe"
            )
        self.assertEqual(windows_environment["SystemRoot"], r"C:\Windows")
        self.assertEqual(windows_environment["WINDIR"], r"C:\Windows")
        self.assertIn(r"C:\Ruby\bin", windows_environment["PATH"])
        self.assertIn(r"C:\Windows\System32", windows_environment["PATH"])
        self.assertEqual(windows_environment["TEMP"], r"C:\Temp")
        for forbidden in ("RUBYOPT", "RUBYLIB", "GEM_HOME"):
            self.assertNotIn(forbidden, windows_environment)

        with (
            mock.patch.object(atom_frontends.os, "name", "posix"),
            mock.patch.dict(
                atom_frontends.os.environ,
                {"RUBYOPT": "-rhostile", "RUBYLIB": "/hostile"},
                clear=True,
            ),
        ):
            posix_environment = atom_frontends._safe_ruby_environment("/usr/bin/ruby")
        self.assertEqual(
            posix_environment,
            {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    def _compose(self, project: Path) -> Path:
        compose = project / "compose.json"
        compose.write_text(
            json.dumps(
                {
                    "name": "atom-suite",
                    "services": {
                        "db": {
                            "image": "postgres",
                            "container_name": "atom-suite-db",
                            "healthcheck": {"test": ["CMD", "pg_isready"]},
                            "ports": [
                                {
                                    "target": 5432,
                                    "published": "15432",
                                    "protocol": "tcp",
                                    "host_ip": "127.0.0.1",
                                }
                            ],
                        },
                        "migrate": {
                            "image": "migration",
                            "depends_on": {"db": {"condition": "service_healthy"}},
                        },
                        "api": {
                            "image": "api",
                            "profiles": ["app"],
                            "ports": ["18080:8080"],
                            "depends_on": {
                                "migrate": {"condition": "service_completed_successfully"}
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        return compose

    def test_mapping_syntax_profiles_completion_and_leases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            compose = self._compose(project)
            result = compile_entrypoints(
                project,
                [
                    {
                        "adapter": "compose_services",
                        "compose_file": str(compose),
                        "services": ["api"],
                        "profiles": ["app"],
                    }
                ],
                target_os="posix",
            )
            by_symbol = {atom["provenance"]["symbol"]: atom for atom in result["atoms"]}
            self.assertEqual(set(by_symbol), {"db", "migrate", "api"})
            self.assertEqual(by_symbol["db"]["operation"]["completion"], "healthy")
            self.assertEqual(by_symbol["migrate"]["operation"]["completion"], "successful_service_exit")
            self.assertIn(
                {"atom": by_symbol["db"]["id"], "kind": "after_healthy"},
                by_symbol["migrate"]["dependencies"],
            )
            self.assertIn(
                {"atom": by_symbol["migrate"]["id"], "kind": "after_completion"},
                by_symbol["api"]["dependencies"],
            )
            db_effects = {(item["domain"], item["key"], item["mode"]) for item in by_symbol["db"]["effects"]}
            api_effects = {(item["domain"], item["key"], item["mode"]) for item in by_symbol["api"]["effects"]}
            self.assertIn(("container-name", "atom-suite-db", "lease"), db_effects)
            self.assertIn(("host-port", "tcp://127.0.0.1:15432", "lease"), db_effects)
            self.assertIn(("host-port", "tcp://0.0.0.0:18080", "lease"), api_effects)
            delegate = result["native_delegates"][0]
            self.assertEqual(delegate["internal_parallelism"], {"kind": "native_scheduler", "tokens": None})
            self.assertEqual(delegate["profiles"], ["app"])
            self.assertEqual(len(validate_atoms(result["atoms"], project)), 3)

    def test_missing_healthcheck_and_profile_dependency_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            compose = project / "compose.json"
            compose.write_text(
                json.dumps(
                    {
                        "services": {
                            "db": {"image": "db", "profiles": ["db"]},
                            "api": {
                                "image": "api",
                                "depends_on": {"db": {"condition": "service_healthy"}},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AtomError, "excluded by the active profile"):
                compile_entrypoints(
                    project,
                    [
                        {
                            "adapter": "compose_services",
                            "compose_file": str(compose),
                            "services": ["api"],
                            "profiles": [],
                        }
                    ],
                    target_os="posix",
                )
            with self.assertRaisesRegex(AtomError, "no enabled healthcheck"):
                compile_entrypoints(
                    project,
                    [
                        {
                            "adapter": "compose_services",
                            "compose_file": str(compose),
                            "services": ["api"],
                            "profiles": ["db"],
                        }
                    ],
                    target_os="posix",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
