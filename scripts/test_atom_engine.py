#!/usr/bin/env python3
"""Regression and invariant tests for the typed atomic planner."""

from __future__ import annotations

import copy
import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from atom_engine import (
    AtomError,
    _normalize_resource,
    _normalize_windows_path,
    _resource_overlap,
    compile_atomic_plan,
    finalize_atomic_plan,
    lower_exact_data_edges,
    validate_atoms,
    validate_source_snapshots,
)


class AtomEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def atom(
        self,
        atom_id: str,
        *,
        duration: float = 1.0,
        dependencies: list[dict[str, str] | str] | None = None,
        accesses: list[dict[str, str]] | None = None,
        claims: list[dict[str, float | str]] | None = None,
        argv: list[str] | None = None,
        reorderable: str = "unknown",
        side_effect: bool = False,
        splittable: bool | None = False,
        batch: dict[str, str] | None = None,
        internal_parallelism: str = "none",
    ) -> dict:
        return {
            "id": atom_id,
            "operation": {
                "kind": "transform",
                "argv": argv or ["/usr/bin/true", atom_id],
                "cwd": str(self.project),
                "completion": "process_exit",
                "internal_parallelism": {"kind": internal_parallelism, "tokens": None},
            },
            "dependencies": dependencies or [],
            "accesses": accesses or [],
            "claims": claims or [],
            "side_effect": side_effect,
            "semantics": {
                "idempotent": True,
                "retryable": False,
                "deterministic": True,
                "cacheable": False,
                "commutative": False,
                "cancel_safe": True,
                "splittable": splittable,
                "reorderable": reorderable,
            },
            "cost": {"duration_seconds": duration, "startup_seconds": 0},
            "batch": batch,
            "assurance": {
                "parse": "exact",
                "control": "exact",
                "effects": "complete_declared",
                "codegen": "exact_argv",
                "rank": 1.0,
                "blockers": [],
            },
        }

    def test_lower_exact_data_edges_handles_bidirectional_flow(self) -> None:
        raw = [
            self.atom(
                "a",
                accesses=[
                    {"resource": "x.bin", "mode": "create"},
                    {"resource": "y.bin", "mode": "read"},
                ],
                side_effect=True,
            ),
            self.atom(
                "b",
                accesses=[
                    {"resource": "x.bin", "mode": "read"},
                    {"resource": "y.bin", "mode": "create"},
                ],
                side_effect=True,
            ),
        ]
        normalized = validate_atoms(raw, self.project)
        lowered, diagnostics = lower_exact_data_edges(normalized)
        self.assertIn("BIDIRECTIONAL_DATA_FLOW", {item["code"] for item in diagnostics})
        self.assertTrue(all(not atom["dependencies"] for atom in lowered))

    def test_file_roots_overlap_descendants_on_every_path_flavor(self) -> None:
        self.assertTrue(_resource_overlap("file:/", "file:/tmp/atomlane/result.json"))
        self.assertTrue(
            _resource_overlap("file:C:\\", "file:C:\\work\\result.json")
        )
        self.assertTrue(
            _resource_overlap(
                "file:\\\\server\\share\\",
                "file:\\\\server\\share\\work\\result.json",
            )
        )
        self.assertFalse(
            _resource_overlap("file:C:\\", "file:D:\\work\\result.json")
        )

    def test_windows_relative_and_absolute_paths_share_lexical_rules(self) -> None:
        cwd = r"C:\Work\Project"
        equivalent_paths = [
            ("artifact. ", r"C:\Work\Project\artifact"),
            ("artifact. :stream. ", r"C:\Work\Project\artifact:stream"),
            (r"future\..\artifact", r"C:\Work\Project\artifact"),
        ]
        for relative, absolute in equivalent_paths:
            with self.subTest(relative=relative):
                self.assertEqual(
                    _normalize_windows_path(relative, cwd=cwd),
                    _normalize_windows_path(absolute),
                )

        rejected_paths = [
            "NUL.txt",
            r"nested\COM1.log",
            "nested\\LPT¹.txt",
            r"NUL\..\output.txt",
            r"future\.. \artifact",
            r"directory:stream\output.txt",
            r"artifact.txt:stream:$INDEX_ALLOCATION",
        ]
        for relative in rejected_paths:
            with self.subTest(rejected=relative), self.assertRaises(AtomError):
                _normalize_windows_path(relative, cwd=cwd)

    def test_windows_unc_and_extended_namespaces_normalize_or_fail_closed(self) -> None:
        self.assertEqual(
            _normalize_windows_path(r"\\?\C:\Work\Artifact. "),
            _normalize_windows_path(r"C:\Work\Artifact"),
        )
        self.assertEqual(
            _normalize_windows_path(r"\\?\UNC\server\share\Output. "),
            _normalize_windows_path(r"\\server\share\Output"),
        )
        rejected_paths = [
            r"\\server",
            r"\\.\C:\device-path",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1",
            r"C:drive-relative",
        ]
        for path in rejected_paths:
            with self.subTest(path=path), self.assertRaises(AtomError):
                _normalize_windows_path(path, cwd=r"C:\Work")

    def test_lower_exact_data_edges_combines_same_direction_resources(self) -> None:
        raw = [
            self.atom(
                "producer",
                accesses=[
                    {"resource": "a.bin", "mode": "create"},
                    {"resource": "b.bin", "mode": "create"},
                ],
                side_effect=True,
            ),
            self.atom(
                "consumer",
                accesses=[
                    {"resource": "b.bin", "mode": "read"},
                    {"resource": "a.bin", "mode": "snapshot"},
                ],
            ),
        ]
        lowered, diagnostics = lower_exact_data_edges(validate_atoms(raw, self.project))
        by_id = {atom["id"]: atom for atom in lowered}
        self.assertEqual(by_id["consumer"]["dependencies"], [{"atom": "producer", "kind": "data"}])
        inferred = [item for item in diagnostics if item["code"] == "INFERRED_DATA_EDGE"]
        self.assertEqual(len(inferred), 1)
        self.assertEqual(len(inferred[0]["resources"]), 2)

    def test_unordered_write_conflict_is_serialized_and_ineligible(self) -> None:
        shared = {"resource": "shared.json", "mode": "overwrite"}
        plan = compile_atomic_plan(
            [
                self.atom("writer-b", accesses=[shared], side_effect=True),
                self.atom("writer-a", accesses=[shared], side_effect=True),
            ],
            self.project,
            capacities={"worker_slot": 2},
        )
        self.assertFalse(plan["execution_eligible"])
        self.assertIn("UNORDERED_SEMANTIC_CONFLICT", {item["code"] for item in plan["diagnostics"]})
        timeline = {item["atom"]: item for item in plan["schedule"]["timeline"]}
        first, second = sorted(timeline.values(), key=lambda item: item["start_seconds"])
        self.assertGreaterEqual(second["start_seconds"], first["end_seconds"])

    def test_explicit_data_order_makes_overwrite_read_safe(self) -> None:
        plan = compile_atomic_plan(
            [
                self.atom(
                    "producer",
                    accesses=[{"resource": "result.json", "mode": "overwrite"}],
                    side_effect=True,
                ),
                self.atom(
                    "consumer",
                    dependencies=[{"atom": "producer", "kind": "data"}],
                    accesses=[{"resource": "result.json", "mode": "read"}],
                ),
            ],
            self.project,
        )
        self.assertNotIn(
            "UNORDERED_ARTIFACT_CONFLICT",
            {item["code"] for item in plan["diagnostics"]},
        )
        self.assertTrue(plan["execution_eligible"])

    def test_event_scheduler_releases_successor_without_wave_barrier(self) -> None:
        plan = compile_atomic_plan(
            [
                self.atom("slow-independent", duration=10),
                self.atom("quick-root", duration=1),
                self.atom(
                    "quick-child",
                    duration=2,
                    dependencies=[{"atom": "quick-root", "kind": "success"}],
                ),
            ],
            self.project,
            capacities={"worker_slot": 2},
        )
        timeline = {item["atom"]: item for item in plan["schedule"]["timeline"]}
        self.assertEqual(timeline["quick-child"]["start_seconds"], 1.0)
        self.assertLess(timeline["quick-child"]["start_seconds"], timeline["slow-independent"]["end_seconds"])
        self.assertEqual(plan["schedule"]["makespan_seconds"], 10.0)

    def test_capacity_claims_are_never_overcommitted(self) -> None:
        plan = compile_atomic_plan(
            [
                self.atom("gpu-a", duration=2, claims=[{"resource": "gpu_slot", "units": 1}]),
                self.atom("gpu-b", duration=2, claims=[{"resource": "gpu_slot", "units": 1}]),
                self.atom("cpu", duration=1),
            ],
            self.project,
            capacities={"worker_slot": 3, "gpu_slot": 1},
        )
        timeline = {item["atom"]: item for item in plan["schedule"]["timeline"]}
        gpu_a, gpu_b = timeline["gpu-a"], timeline["gpu-b"]
        self.assertTrue(
            gpu_a["end_seconds"] <= gpu_b["start_seconds"]
            or gpu_b["end_seconds"] <= gpu_a["start_seconds"]
        )
        self.assertEqual(plan["schedule"]["peak_parallelism"], 2)
        self.assertFalse(plan["schedule"]["blocked_claims"])

        impossible = compile_atomic_plan(
            [
                self.atom(
                    "too-large",
                    claims=[
                        {"resource": "gpu_slot", "units": 0.75},
                        {"resource": "gpu_slot", "units": 0.75},
                    ],
                )
            ],
            self.project,
            capacities={"gpu_slot": 1},
        )
        self.assertEqual(impossible["schedule"]["timeline"], [])
        self.assertEqual(impossible["schedule"]["unscheduled_atoms"], ["too-large"])
        self.assertFalse(impossible["execution_eligible"])

    def test_plan_hash_and_schedule_ignore_input_permutation(self) -> None:
        first_source = self.project / "first.cfg"
        second_source = self.project / "second.cfg"
        first_source.write_text("first", encoding="utf-8")
        second_source.write_text("second", encoding="utf-8")
        snapshots = [
            {
                "path": "first.cfg",
                "size": 5,
                "sha256": hashlib.sha256(b"first").hexdigest(),
            },
            {
                "path": "second.cfg",
                "size": 6,
                "sha256": hashlib.sha256(b"second").hexdigest(),
            },
        ]
        atoms = [
            self.atom(
                "alpha",
                accesses=[
                    {"resource": "second.cfg", "mode": "read"},
                    {"resource": "first.cfg", "mode": "snapshot"},
                ],
                claims=[
                    {"resource": "network_slot", "units": 1},
                    {"resource": "disk_slot", "units": 1},
                ],
            ),
            self.atom(
                "beta",
                accesses=[{"resource": "first.cfg", "mode": "read"}],
                claims=[{"resource": "disk_slot", "units": 1}],
            ),
        ]
        capacities = [
            {"resource": "worker_slot", "capacity": 2},
            {"resource": "disk_slot", "capacity": 2},
            {"resource": "network_slot", "capacity": 1},
        ]
        first = compile_atomic_plan(
            atoms,
            self.project,
            capacities=capacities,
            snapshots=snapshots,
        )
        permuted_atoms = copy.deepcopy(list(reversed(atoms)))
        permuted_atoms[1]["accesses"].reverse()
        permuted_atoms[1]["claims"].reverse()
        second = finalize_atomic_plan(
            permuted_atoms,
            self.project,
            capacities=list(reversed(capacities)),
            snapshots=list(reversed(snapshots)),
        )
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertEqual(first["atoms"], second["atoms"])
        self.assertEqual(first["schedule"]["timeline"], second["schedule"]["timeline"])

    def test_source_snapshot_detects_toctou_write(self) -> None:
        source = self.project / "task.json"
        source.write_text("alpha", encoding="utf-8")
        snapshot = {
            "path": "task.json",
            "size": 5,
            "sha256": hashlib.sha256(b"alpha").hexdigest(),
        }
        plan = compile_atomic_plan(
            [self.atom("inspect")],
            self.project,
            snapshots=[snapshot],
        )
        self.assertFalse(validate_source_snapshots(plan))
        source.write_text("omega", encoding="utf-8")
        diagnostics = validate_source_snapshots(plan)
        self.assertIn("SOURCE_SNAPSHOT_CHANGED", {item["code"] for item in diagnostics})

    def test_fusion_split_and_native_delegate_suggestions_are_explicit(self) -> None:
        batch = {"key": "lint-files", "strategy": "same_argv_shape"}
        atoms = [
            self.atom(
                "lint-a",
                argv=["lint", "a.py"],
                batch=batch,
                splittable=True,
            ),
            self.atom(
                "lint-b",
                argv=["lint", "b.py"],
                batch=batch,
                splittable=False,
            ),
        ]
        plan = compile_atomic_plan(
            atoms,
            self.project,
            native_delegates=[
                {
                    "kind": "lint_native_batch",
                    "argv": ["lint", "a.py", "b.py"],
                    "cwd": str(self.project),
                    "reason": "one native process",
                }
            ],
        )
        self.assertTrue(plan["fusion_suggestions"][0]["eligible"])
        self.assertEqual(plan["split_suggestions"][0]["atom"], "lint-a")
        self.assertTrue(plan["native_delegate_suggestions"])

    def test_unknown_effect_is_a_hard_execution_blocker(self) -> None:
        atom = self.atom("publish", side_effect=True)
        atom["operation"]["kind"] = "mutation"
        atom["assurance"]["effects"] = "unknown"
        atom["assurance"]["blockers"] = ["UNKNOWN_EFFECT"]
        plan = compile_atomic_plan([atom], self.project)
        self.assertFalse(plan["execution_eligible"])
        self.assertIn("UNKNOWN_EFFECT", {item["code"] for item in plan["execution_blockers"]})

    def test_macos_casefold_and_hardlink_aliases_fail_closed(self) -> None:
        case_plan = compile_atomic_plan(
            [
                self.atom(
                    "upper",
                    accesses=[{"resource": "Result.JSON", "mode": "overwrite"}],
                    reorderable="explicit",
                    side_effect=True,
                ),
                self.atom(
                    "lower",
                    accesses=[{"resource": "result.json", "mode": "overwrite"}],
                    reorderable="explicit",
                    side_effect=True,
                ),
            ],
            self.project,
        )
        self.assertFalse(case_plan["execution_eligible"])
        self.assertIn(
            "UNORDERED_ARTIFACT_CONFLICT",
            {item["code"] for item in case_plan["diagnostics"]},
        )

        original = self.project / "original.bin"
        alias = self.project / "hardlink.bin"
        original.write_bytes(b"x")
        os.link(original, alias)
        hardlink_plan = compile_atomic_plan(
            [
                self.atom(
                    "original-writer",
                    accesses=[{"resource": str(original), "mode": "overwrite"}],
                    reorderable="explicit",
                    side_effect=True,
                ),
                self.atom(
                    "alias-reader",
                    accesses=[{"resource": str(alias), "mode": "read"}],
                    reorderable="explicit",
                ),
            ],
            self.project,
        )
        self.assertFalse(hardlink_plan["execution_eligible"])

    def test_windows_drive_unc_and_extended_paths_cannot_bypass_conflicts(self) -> None:
        aliases = [
            (r"C:\Work\Artifact", r"c:/work/artifact/child.bin"),
            (r"\\server\share\Output", r"//SERVER/share/output/result.json"),
            (r"\\?\C:\Work\Artifact", r"c:\work\artifact"),
            (r"C:\Work\file.txt", r"c:\work\FILE.TXT:stream"),
        ]
        for index, (first, second) in enumerate(aliases):
            with self.subTest(index=index):
                plan = compile_atomic_plan(
                    [
                        self.atom(
                            "writer",
                            accesses=[{"resource": first, "mode": "overwrite"}],
                            side_effect=True,
                        ),
                        self.atom(
                            "reader",
                            accesses=[{"resource": second, "mode": "read"}],
                        ),
                    ],
                    self.project,
                )
                self.assertFalse(plan["execution_eligible"])

        with self.assertRaisesRegex(AtomError, "drive-relative"):
            compile_atomic_plan(
                [
                    self.atom(
                        "ambiguous",
                        accesses=[{"resource": r"C:relative\result.txt", "mode": "read"}],
                    )
                ],
                self.project,
            )

    @unittest.skipUnless(os.name == "nt", "requires native Windows path semantics")
    def test_native_windows_relative_file_resources_use_the_same_canonicalizer(self) -> None:
        relative = _normalize_resource("file:future\\artifact. ", self.project)
        absolute = _normalize_resource(
            "file:" + str(self.project / "future" / "artifact"),
            self.project,
        )
        self.assertEqual(relative.casefold(), absolute.casefold())

        base = _normalize_resource("file:future\\artifact.txt", self.project)
        stream = _normalize_resource("file:future\\artifact.txt:stream. ", self.project)
        self.assertTrue(_resource_overlap(base, stream))
        with self.assertRaisesRegex(AtomError, "reserved device"):
            _normalize_resource("file:future\\NUL.txt", self.project)

        existing_file = self.project / "not-a-directory"
        existing_file.write_bytes(b"x")
        with self.assertRaisesRegex(AtomError, "descends from a non-directory"):
            _normalize_resource("file:not-a-directory\\future.bin", self.project)

    @unittest.skipUnless(os.name == "nt", "requires native Windows reparse points")
    def test_native_windows_future_outputs_resolve_existing_reparse_ancestor(self) -> None:
        real = self.project / "real-output-root"
        real.mkdir()
        alias = self.project / "output-root-alias"
        junction = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(alias), str(real)],
            check=False,
            capture_output=True,
            text=True,
        )
        if junction.returncode != 0:
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"cannot create a Windows junction or symlink: {exc}")

        through_alias = _normalize_resource(
            "file:output-root-alias\\future\\result.bin",
            self.project,
        )
        through_target = _normalize_resource(
            "file:real-output-root\\future\\result.bin",
            self.project,
        )
        self.assertEqual(through_alias.casefold(), through_target.casefold())

        broken_alias = self.project / "broken-output-alias"
        try:
            broken_alias.symlink_to(
                self.project / "missing-output-root",
                target_is_directory=True,
            )
        except OSError as exc:
            self.skipTest(f"cannot create a broken Windows symlink: {exc}")
        with self.assertRaisesRegex(AtomError, "cannot resolve Windows file resource ancestor"):
            _normalize_resource(
                "file:broken-output-alias\\future\\result.bin",
                self.project,
            )

    def test_nested_parallelism_is_budgeted_or_outer_serialized(self) -> None:
        bounded = self.atom("bounded")
        bounded["operation"]["internal_parallelism"] = {"kind": "bounded", "tokens": 4}
        bounded_plan = compile_atomic_plan(
            [bounded], self.project, capacities={"worker_slot": 1, "cpu_core": 4}
        )
        claims = {item["resource"]: item["units"] for item in bounded_plan["atoms"][0]["claims"]}
        self.assertEqual(claims["cpu_core"], 4)

        unknown = self.atom("unknown", reorderable="explicit", internal_parallelism="unknown")
        known = self.atom("known", reorderable="explicit")
        serialized = compile_atomic_plan(
            [unknown, known], self.project, capacities={"worker_slot": 2, "cpu_core": 2}
        )
        self.assertEqual(serialized["schedule"]["peak_parallelism"], 1)
        self.assertTrue(serialized["execution_eligible"])
        self.assertIn(
            "SERIALIZED_REORDERABLE_CONFLICT",
            {item["code"] for item in serialized["diagnostics"]},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
