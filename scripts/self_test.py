#!/usr/bin/env python3
"""Smoke-test MCP initialization, parallel execution, mapping, and DAG behavior."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile

import mcp_server

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "mcp_server.py"
LIVE_RUNNER = ROOT / "scripts" / "live_runner.py"


def request(request_id: int, method: str, params: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="atomlane-") as temp_dir:
        project = pathlib.Path(temp_dir)
        (project / "experiments").mkdir()
        (project / "paper").mkdir()
        (project / "tests").mkdir()
        (project / "src" / "app" / "mcp").mkdir(parents=True)
        (project / "src" / "lib").mkdir(parents=True)
        (project / "server" / "src").mkdir(parents=True)
        (project / "packages" / "contract").mkdir(parents=True)
        (project / "prisma" / "migrations" / "001_init").mkdir(parents=True)
        (project / "scripts").mkdir()
        (project / "scripts" / "pure_map.py").write_text(
            "def square(value):\n"
            "    return value * value\n\n"
            "def main():\n"
            "    values = list(range(100))\n"
            "    results = [square(value) for value in values]\n"
            "    return results\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n",
            encoding="utf-8",
        )
        (project / "experiments" / "sweep.py").write_text(
            "from concurrent.futures import ProcessPoolExecutor\nimport json\nimport numpy as np\nprint(json.dumps(np.arange(4).tolist()))\n",
            encoding="utf-8",
        )
        (project / "paper" / "main.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
        (project / "tests" / "test_sample.py").write_text("# test\n", encoding="utf-8")
        (project / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n', encoding="utf-8")
        (project / "server" / "package.json").write_text('{"scripts":{"test":"jest"}}\n', encoding="utf-8")
        (project / "packages" / "contract" / "package.json").write_text('{"name":"contract"}\n', encoding="utf-8")
        (project / "next.config.ts").write_text("export default {}\n", encoding="utf-8")
        (project / "tsconfig.json").write_text('{"compilerOptions":{"incremental":true}}\n', encoding="utf-8")
        (project / "docker-compose.yml").write_text(
            "services:\n"
            "  db:\n"
            "    image: postgres:17\n"
            "    healthcheck:\n"
            "      test: ['CMD', 'true']\n"
            "    volumes:\n"
            "      - db-data:/var/lib/postgresql/data\n"
            "  api:\n"
            "    build: .\n"
            "    depends_on:\n"
            "      db:\n"
            "        condition: service_healthy\n"
            "volumes:\n"
            "  db-data:\n",
            encoding="utf-8",
        )
        (project / "Dockerfile").write_text(
            "FROM python:3.13 AS build\n"
            "RUN --mount=type=cache,target=/root/.cache true\n"
            "FROM python:3.13\n"
            "COPY --from=build /usr/local /usr/local\n",
            encoding="utf-8",
        )
        (project / ".dockerignore").write_text(".git\nnode_modules\n", encoding="utf-8")
        (project / "AGENTS.md").write_text("# Agent guidance\n", encoding="utf-8")
        (project / "collab-hocuspocus.ts").write_text("// realtime collaboration\n", encoding="utf-8")
        (project / "src" / "app" / "page.tsx").write_text("export default function Page() {}\n", encoding="utf-8")
        (project / "src" / "app" / "page.spec.ts").write_text("// test\n", encoding="utf-8")
        (project / "src" / "app" / "mcp" / "agent-auth.ts").write_text("// MCP auth\n", encoding="utf-8")
        (project / "src" / "lib" / "worker-pool.ts").write_text(
            "import { Worker } from 'worker_threads'; import { createReadStream } from 'node:fs'; import { createHash } from 'node:crypto'; const parsed = JSON.parse('{}'); fetch('http://localhost'); new Worker('worker.js'); createReadStream('input'); createHash('sha256');\n",
            encoding="utf-8",
        )
        (project / "server" / "src" / "project.service.ts").write_text(
            "const reads = Promise.all([prisma.project.findMany(), prisma.task.findMany()]); const writes = prisma.event.createMany({ data: [] });\n",
            encoding="utf-8",
        )
        (project / "server" / "src" / "realtime.gateway.ts").write_text("// websocket\n", encoding="utf-8")
        (project / "prisma" / "schema.prisma").write_text("datasource db { provider = \"postgresql\" }\n", encoding="utf-8")
        (project / "prisma" / "seed.ts").write_text("// seed\n", encoding="utf-8")
        (project / "prisma" / "seed-demo.ts").write_text("// seed\n", encoding="utf-8")
        (project / "prisma" / "migrations" / "001_init" / "migration.sql").write_text("SELECT 1;\n", encoding="utf-8")
        (project / "scripts" / "outbox-worker.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (project / "scripts" / "retention-worker.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (project / "scripts" / "automation-cron.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (project / "Makefile").write_text(
            "experiment-run:\n\t@true\n\npaper:\n\t@true\n\ntest:\n\t@true\n",
            encoding="utf-8",
        )
        messages = [
            request(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "self-test", "version": "1"},
                },
            ),
            request(
                2,
                "tools/call",
                {
                    "name": "parallel_exec",
                    "_meta": {"progressToken": "self-test-progress"},
                    "arguments": {
                        "default_cwd": temp_dir,
                        "max_concurrency": 2,
                        "tasks": [
                            {"id": "one", "argv": [sys.executable, "-c", "print('one')"]},
                            {"id": "two", "argv": [sys.executable, "-c", "print('two')"]},
                        ],
                    },
                },
            ),
            request(
                3,
                "tools/call",
                {
                    "name": "parallel_map",
                    "arguments": {
                        "default_cwd": temp_dir,
                        "argv_template": [sys.executable, "-c", "import sys; print(sys.argv[1])", "{item}-{index}"],
                        "items": ["a", "b"],
                    },
                },
            ),
            request(
                4,
                "tools/call",
                {
                    "name": "parallel_dag",
                    "arguments": {
                        "default_cwd": temp_dir,
                        "max_concurrency": 2,
                        "tasks": [
                            {"id": "root", "argv": [sys.executable, "-c", "print('root')"]},
                            {"id": "bad", "argv": [sys.executable, "-c", "raise SystemExit(7)"]},
                            {
                                "id": "child",
                                "argv": [sys.executable, "-c", "print('child')"],
                                "depends_on": ["root"],
                            },
                            {
                                "id": "blocked",
                                "argv": [sys.executable, "-c", "print('should not run')"],
                                "depends_on": ["bad"],
                            },
                        ],
                    },
                },
            ),
            request(
                5,
                "tools/call",
                {
                    "name": "parallel_exec",
                    "arguments": {
                        "default_cwd": temp_dir,
                        "serial_baseline_seconds": 2.0,
                        "tasks": [
                            {"id": "serial", "argv": [sys.executable, "-c", "print('serial')"]}
                        ],
                    },
                },
            ),
            request(
                6,
                "tools/call",
                {
                    "name": "mac_resource_plan",
                    "arguments": {"profile": "cpu", "responsiveness": "interactive"},
                },
            ),
            request(
                7,
                "tools/call",
                {
                    "name": "mac_accelerator_plan",
                    "arguments": {"workload": "ml_inference", "responsiveness": "interactive"},
                },
            ),
            request(
                8,
                "tools/call",
                {
                    "name": "scenario_plan",
                    "arguments": {
                        "project_path": temp_dir,
                        "task_hint": "Run the AI-generated full-stack quality gate, realtime agent/MCP checks, database seed matrix, background workers, experiments, tests, and paper build",
                        "include_trace_history": False,
                        "max_scenarios": 20,
                    },
                },
            ),
            request(
                9,
                "tools/call",
                {
                    "name": "container_resource_plan",
                    "arguments": {
                        "docker_vm_cpus": 8,
                        "docker_vm_memory_mb": 8192,
                        "reserve_vm_cpus": 2,
                        "reserve_vm_memory_mb": 2048,
                        "pin_cpus": True,
                        "services": [
                            {
                                "id": "build",
                                "profile": "build",
                                "weight": 2,
                                "estimated_memory_mb": 1024,
                            },
                            {
                                "id": "db",
                                "profile": "database",
                                "requested_cpus": 1.5,
                                "requested_memory_mb": 1536,
                            },
                            {
                                "id": "api",
                                "profile": "mixed",
                                "estimated_memory_mb": 512,
                                "depends_on": ["db"],
                            },
                        ],
                    },
                },
            ),
            request(
                10,
                "tools/call",
                {
                    "name": "task_parallel_scan",
                    "arguments": {
                        "project_path": temp_dir,
                        "task_summary": "Run lint and tests together, then build; keep shared writes and release mutation safe",
                        "discover_project_commands": False,
                        "include_scenario_context": False,
                        "responsiveness": "throughput",
                        "max_concurrency": 4,
                        "candidate_units": [
                            {
                                "id": "lint",
                                "kind": "test",
                                "argv": ["npm", "run", "lint"],
                                "reads": ["src"],
                                "estimated_seconds": 2.0,
                            },
                            {
                                "id": "tests",
                                "kind": "test",
                                "argv": ["npm", "test"],
                                "reads": ["src", "tests"],
                                "writes": ["work/test-results"],
                                "estimated_seconds": 4.0,
                            },
                            {
                                "id": "write-a",
                                "kind": "transform",
                                "argv": ["tool", "a"],
                                "writes": ["work/shared.json"],
                                "estimated_seconds": 1.0,
                            },
                            {
                                "id": "write-b",
                                "kind": "transform",
                                "argv": ["tool", "b"],
                                "writes": ["work/shared.json"],
                                "estimated_seconds": 1.0,
                            },
                            {
                                "id": "build",
                                "kind": "build",
                                "argv": ["npm", "run", "build"],
                                "depends_on": ["lint", "tests"],
                                "writes": ["dist"],
                                "estimated_seconds": 3.0,
                            },
                            {
                                "id": "release",
                                "kind": "mutation",
                                "argv": ["release-tool"],
                                "depends_on": ["build"],
                                "estimated_seconds": 1.0,
                            },
                            {
                                "id": "tiny-a",
                                "kind": "read",
                                "argv": ["stat", "a"],
                                "batch_key": "metadata",
                                "estimated_seconds": 0.01,
                            },
                            {
                                "id": "tiny-b",
                                "kind": "read",
                                "argv": ["stat", "b"],
                                "batch_key": "metadata",
                                "estimated_seconds": 0.01,
                            },
                        ],
                    },
                },
            ),
            request(
                11,
                "tools/call",
                {
                    "name": "python_parallel_advisor",
                    "arguments": {
                        "project_path": temp_dir,
                        "paths": ["scripts/pure_map.py"],
                        "max_workers": 2,
                        "hotspots": [
                            {
                                "path": "scripts/pure_map.py",
                                "line": 6,
                                "wall_seconds": 60.0,
                                "item_count": 100,
                            }
                        ],
                    },
                },
            ),
            request(12, "tools/list", {}),
            request(13, "resources/list", {}),
            request(
                14,
                "resources/read",
                {"uri": mcp_server.INDICATOR_RESOURCE_URI},
            ),
        ]
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        environment = dict(os.environ)
        environment["ATOMLANE_STATS_PATH"] = str(
            pathlib.Path(temp_dir) / "stats.json"
        )
        environment["ATOMLANE_PROGRESS_INTERVAL"] = "0.01"
        completed = subprocess.run(
            [sys.executable, str(SERVER)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=15,
            env=environment,
        )
        if completed.returncode != 0:
            raise AssertionError(completed.stderr)
        messages = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        progress = [item for item in messages if item.get("method") == "notifications/progress"]
        responses = [item for item in messages if "id" in item]
        assert len(responses) == 14
        assert progress
        assert all(item["params"]["progressToken"] == "self-test-progress" for item in progress)
        assert any("当前预计节约" in item["params"]["message"] for item in progress)
        assert responses[0]["result"]["serverInfo"]["name"] == "atomlane"
        assert responses[0]["result"]["serverInfo"]["version"] == mcp_server.SERVER_VERSION
        assert "cheaply assess parallel eligibility" in responses[0]["result"]["instructions"]

        parallel = responses[1]["result"]["structuredContent"]
        assert parallel["summary"]["status_counts"] == {"succeeded": 2}
        expected_peak = min(2, parallel["resource_plan"]["chosen_concurrency"])
        assert parallel["indicator"]["parallel"] is (expected_peak > 1)
        assert parallel["indicator"]["peak_concurrency"] == expected_peak
        assert parallel["indicator"]["speedup_kind"] == "estimated_sum_of_task_durations"
        assert "time_saved_seconds" in parallel["indicator"]
        assert "cumulative_saved_seconds" in parallel["summary"]
        expected_prefix = "⚡ 并行" if expected_peak > 1 else "→ 串行"
        assert responses[1]["result"]["content"][0]["text"].startswith(expected_prefix)
        assert responses[1]["result"]["_meta"]["ui/resourceUri"].startswith("ui://widget/")
        assert parallel["resource_plan"]["responsiveness"] == "interactive"
        assert parallel["resource_plan"]["reserve_cores_source"] == "adaptive"
        assert parallel["resource_plan"]["nice_adjustment"] == (0 if os.name == "nt" else 10)
        assert {item["stdout"].strip() for item in parallel["results"]} == {"one", "two"}

        mapped = responses[2]["result"]["structuredContent"]
        assert mapped["indicator"]["parallel"] is (mapped["indicator"]["peak_concurrency"] > 1)
        assert [item["stdout"].strip() for item in mapped["results"]] == ["a-0", "b-1"]

        dag = responses[3]["result"]["structuredContent"]
        by_id = {item["id"]: item for item in dag["results"]}
        assert by_id["root"]["status"] == "succeeded"
        assert by_id["bad"]["status"] == "failed"
        assert by_id["child"]["status"] == "succeeded"
        assert by_id["blocked"]["status"] == "skipped"
        assert dag["indicator"]["parallel"] is (dag["indicator"]["peak_concurrency"] > 1)
        assert dag["indicator"]["savings_eligible"] is False

        serial = responses[4]["result"]["structuredContent"]
        assert serial["indicator"]["parallel"] is False
        assert serial["indicator"]["peak_concurrency"] == 1
        assert serial["indicator"]["speedup_kind"] == "measured_serial_baseline"
        assert serial["indicator"]["time_saved_seconds"] > 1.0
        assert responses[4]["result"]["content"][0]["text"].startswith("→ 串行｜峰值 1 路｜实测 ")

        resource_plan = responses[5]["result"]["structuredContent"]
        assert resource_plan["reserve_cores"] >= 1
        assert resource_plan["chosen_concurrency"] <= resource_plan["machine"]["logical_cpus"]
        assert resource_plan["machine"]["thermal_state"] in {
            "nominal", "fair", "serious", "critical", "unknown"
        }

        accelerator = responses[6]["result"]["structuredContent"]
        if os.name == "nt":
            assert accelerator["status"] == "unavailable_on_this_platform"
            assert accelerator["selected_backend"] is None
        else:
            assert accelerator["selected_backend"] in {
                "core_ml_all_compute_units", "parallel_cpu"
            }
            assert accelerator["transparent_offload"] is False
            assert "inventory" in accelerator

        scenario = responses[7]["result"]["structuredContent"]
        assert scenario["catalog"]["scenario_count"] >= 63
        assert scenario["catalog"]["optimization_goal_count"] >= 189
        assert scenario["catalog"]["layer_counts"]["workflow"] >= 39
        assert scenario["catalog"]["layer_counts"]["resource"] >= 4
        matched = {item["id"] for item in scenario["matched_scenarios"]}
        assert "scientific-experiment-sweep" in matched
        assert "scientific-paper-pipeline" in matched
        assert "software-test-matrix" in matched
        assert "fullstack-quality-gate" in matched
        assert "ai-generated-project-stabilization" in matched
        assert "realtime-collaboration-stack" in matched
        assert "ai-agent-governance-audit" in matched
        assert "database-fixture-seed-matrix" in matched
        assert "background-worker-cron-verification" in matched
        assert "python-gil-executor" in matched
        assert "javascript-worker-pool" in matched
        assert "database-read-fanout" in matched
        assert "database-bulk-write-transaction" in matched
        assert scenario["trace_history"]["available"] is False
        assert scenario["optimization_targets"]

        container_plan = responses[8]["result"]["structuredContent"]
        assert container_plan["vm_envelope"]["usable_cpus"] == 6
        assert container_plan["vm_envelope"]["usable_memory_mb"] == 6144
        assert len(container_plan["allocations"]) == 3
        assert container_plan["totals"]["allocated_cpu_quota"] <= 6
        assert container_plan["totals"]["allocated_memory_limit_mb"] <= 6144
        assert all(item["cpuset"] for item in container_plan["allocations"])
        assert "services:" in container_plan["compose_override_yaml"]
        assert "max-parallelism" in container_plan["buildkit"]["config_toml"]

        task_scan = responses[9]["result"]["structuredContent"]
        assert task_scan["decision"]["recommended_executor"] == "none"
        assert task_scan["decision"]["status"] == "serial_or_blocked"
        assert task_scan["plan_hash"].startswith("sha256:")
        assert task_scan["compiled_plan"]["execution_eligible"] is False
        assert task_scan["compiled_plan"]["schedule"]["estimated_time_saved_seconds"] >= 0
        assert any(
            item.get("code") == "UNORDERED_ARTIFACT_CONFLICT"
            and item.get("atoms") == ["write-a", "write-b"]
            for item in task_scan["diagnostics"]
        )
        assert task_scan["execution_contract"]["manual_wave_translation_forbidden"] is True
        assert "atomic_task_plan" in task_scan["deprecation"]

        python_advice = responses[10]["result"]["structuredContent"]
        assert python_advice["analysis_mode"] == "static_non_executing"
        assert python_advice["execution_performed"] is False
        assert python_advice["files_modified"] is False
        if python_advice["resource_plan"]["chosen_concurrency"] >= 2:
            assert python_advice["candidates"][0]["benefit"]["kind"] == "measured_serial_modeled_parallel"
            assert python_advice["summary"]["classification_counts"]["reviewable_rewrite"] == 1
            assert python_advice["candidates"][0]["rewrite_preview"]["source_sha256"].startswith("sha256:")
        else:
            assert python_advice["candidates"][0]["benefit"]["kind"] == "not_applicable_until_safety"
            assert python_advice["summary"]["classification_counts"]["blocked"] == 1
            assert "rewrite_preview" not in python_advice["candidates"][0]
            assert any(
                item["code"] == "INSUFFICIENT_WORKERS"
                for item in python_advice["candidates"][0]["blockers"]
            )

        tools = {item["name"]: item for item in responses[11]["result"]["tools"]}
        assert "scenario_plan" in tools
        assert "python_parallel_advisor" in tools
        assert "task_parallel_scan" in tools
        assert "atomic_task_plan" in tools
        assert "atomic_exec" in tools
        assert "host_resource_plan" in tools
        assert "container_resource_plan" in tools
        for name in ("atomic_exec", "parallel_exec", "parallel_map", "parallel_dag"):
            assert tools[name]["_meta"]["openai/outputTemplate"].startswith("ui://widget/")
            assert tools[name]["_meta"]["openai/toolInvocation/invoking"]

        resources = responses[12]["result"]["resources"]
        assert len(resources) == 1
        assert resources[0]["mimeType"] == "text/html;profile=mcp-app"
        resource = responses[13]["result"]["contents"][0]
        assert resource["uri"] == resources[0]["uri"]
        assert "并行加速已完成" in resource["text"]
        assert "ui/notifications/tool-result" in resource["text"]
        assert "本次节约" in resource["text"]
        assert "累计已入账" in resource["text"]
        assert "累计估算（未入账）" in resource["text"]
        assert "setInterval" in resource["text"]
        assert "指示器预览" in resource["text"]

        live_input = pathlib.Path(temp_dir) / "live-input.json"
        live_input.write_text(
            json.dumps(
                {
                    "default_cwd": temp_dir,
                    "max_concurrency": 2,
                    "tasks": [
                        {"id": "live-one", "argv": [sys.executable, "-c", "import time; time.sleep(.12)"]},
                        {"id": "live-two", "argv": [sys.executable, "-c", "import time; time.sleep(.12)"]},
                    ],
                }
            ),
            encoding="utf-8",
        )
        live_completed = subprocess.run(
            [sys.executable, str(LIVE_RUNNER), "--mode", "exec", "--input", str(live_input)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
            env=environment,
        )
        assert live_completed.returncode == 0, live_completed.stderr
        assert "⏱️ 实时" in live_completed.stdout
        live_line = next(
            line for line in live_completed.stdout.splitlines() if line.startswith("LIVE_RESULT_JSON=")
        )
        live_result = json.loads(live_line.removeprefix("LIVE_RESULT_JSON="))
        assert live_result["summary"]["status_counts"] == {"succeeded": 2}
        live_expected_peak = min(2, live_result["resource_plan"]["chosen_concurrency"])
        assert live_result["indicator"]["peak_concurrency"] == live_expected_peak
        assert live_result["indicator"]["parallel"] is (live_expected_peak > 1)

        atomic_atoms = []
        for atom_id in ("atomic-one", "atomic-two"):
            atomic_atoms.append(
                {
                    "id": atom_id,
                    "operation": {
                        "kind": "read",
                        "argv": [sys.executable, "-c", "import time; time.sleep(.12)"],
                        "cwd": temp_dir,
                        "completion": "process_exit",
                        "internal_parallelism": {"kind": "none", "tokens": None},
                    },
                    "accesses": [{"resource": "src", "mode": "read"}],
                    "effects": [],
                    "side_effect": False,
                    "profile": "io",
                    "cost": {"duration_seconds": 0.12},
                    "semantics": {
                        "idempotent": True,
                        "retryable": True,
                        "deterministic": True,
                        "cacheable": False,
                        "commutative": False,
                        "cancel_safe": True,
                        "splittable": False,
                        "reorderable": "explicit",
                    },
                    "assurance": {
                        "parse": "exact",
                        "control": "exact",
                        "effects": "complete_declared",
                        "codegen": "exact_argv",
                        "rank": 1.0,
                        "blockers": [],
                    },
                }
            )
        atomic_plan = mcp_server.atomic_task_plan(
            {
                "project_path": temp_dir,
                "task_summary": "two independent exact read atoms",
                "atoms": atomic_atoms,
                "max_concurrency": 2,
                "responsiveness": "throughput",
            }
        )
        assert atomic_plan["execution_eligible"] is True
        assert atomic_plan["schedule"]["peak_parallelism"] >= 1
        atomic_input = pathlib.Path(temp_dir) / "atomic-live-input.json"
        atomic_input.write_text(
            json.dumps({"compiled_plan": atomic_plan, "plan_hash": atomic_plan["plan_hash"]}),
            encoding="utf-8",
        )
        atomic_completed = subprocess.run(
            [sys.executable, str(LIVE_RUNNER), "--mode", "atomic", "--input", str(atomic_input)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=5,
            env=environment,
        )
        assert atomic_completed.returncode == 0, atomic_completed.stderr
        assert "⏱️ 实时" in atomic_completed.stdout
        atomic_line = next(
            line for line in atomic_completed.stdout.splitlines() if line.startswith("LIVE_RESULT_JSON=")
        )
        atomic_result = json.loads(atomic_line.removeprefix("LIVE_RESULT_JSON="))
        assert atomic_result["plan_hash"] == atomic_plan["plan_hash"]
        assert atomic_result["summary"]["status_counts"] == {"succeeded": 2}
        assert len(atomic_result["event_journal"]) == 4

        stats = json.loads((pathlib.Path(temp_dir) / "stats.json").read_text(encoding="utf-8"))
        completed_runs = [parallel, mapped, dag, serial, live_result, atomic_result]
        expected_credited_runs = sum(
            item["indicator"]["ledger_credit_eligible"] is True
            for item in completed_runs
        )
        expected_estimated_runs = sum(
            item["indicator"]["savings_eligible"] is True
            and item["indicator"]["ledger_credit_eligible"] is False
            for item in completed_runs
        )
        assert stats["run_count"] == expected_credited_runs
        assert stats["estimated_run_count"] == expected_estimated_runs

    print("Self-test passed: in-task scanning, scenario routing, adaptive resources, live execution, map, and DAG")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
