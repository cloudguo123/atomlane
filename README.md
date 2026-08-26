# Mac Parallel Accelerator

**Make Codex finish builds, tests, Docker, and research pipelines faster on macOS—without breaking task semantics.**

[![CI](https://github.com/cloudguo123/mac-parallel-accelerator/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudguo123/mac-parallel-accelerator/actions/workflows/ci.yml)
[![Five-minute benchmark](https://github.com/cloudguo123/mac-parallel-accelerator/actions/workflows/long-benchmark.yml/badge.svg)](https://github.com/cloudguo123/mac-parallel-accelerator/actions/workflows/long-benchmark.yml)
[![Test report](https://img.shields.io/badge/test_report-live-65e6b4.svg)](https://cloudguo123.github.io/mac-parallel-accelerator/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Discussions](https://img.shields.io/github/discussions/cloudguo123/mac-parallel-accelerator?color=80b7ff)](https://github.com/cloudguo123/mac-parallel-accelerator/discussions)

[中文说明](README.zh-CN.md) · [Live report](https://cloudguo123.github.io/mac-parallel-accelerator/) · [Share a benchmark](https://github.com/cloudguo123/mac-parallel-accelerator/issues/new?template=benchmark.yml)

![Mac Parallel Accelerator: controlled benchmark showing 20m41s serial equivalent, 5m10s parallel wall time, and 15m31s saved](assets/growth/social-preview.svg)

## Install in two commands

```bash
codex plugin marketplace add cloudguo123/mac-parallel-accelerator
codex plugin add mac-parallel-accelerator@mac-parallel-accelerator
```

Open a new Codex task, then ask:

```text
Use $accelerate-local-work to inspect this project and run the safe parts in parallel.
Keep progress visible and report time saved for this run and cumulatively.
```

Requirements: macOS, Codex with plugin and MCP support, and Python 3.10+. Ruby is only needed for Compose YAML analysis; Node.js 20+ is only needed to rebuild the browser indicator.

## Why this exists

Most “parallel” wrappers split command text and hope for the best. That can reorder `&&`/`||`, race `.next`, JUnit, database, Docker volume, or Git state, multiply nested worker pools, and hide failures until the end.

Mac Parallel Accelerator first compiles the requested work into a typed Atom IR. Only atoms proven independent are admitted concurrently. Unknown effects, ambiguous writers, stale source snapshots, unsupported lifecycle events, and changed plans fail closed.

```text
shell · package scripts · Make · Compose · tests · builds · declared work
                              │
                              ▼
             static frontends → typed Atom IR
                              │
                              ▼
        conflict checks → resource-aware event scheduler
                              │
                              ▼
              exact verified execution + live savings
```

## What it accelerates

| Project situation | Optimization target | Safety boundary |
| --- | --- | --- |
| Web / TypeScript | Quality gates, package graphs, browser matrices | Preserves success gates; isolates `.next`, coverage, JUnit, and caches |
| Docker / Compose | Multi-image builds, health DAGs, test matrices | Honors VM CPU/memory envelope, ports, volumes, readiness, and migrations |
| Research / papers | Data preparation, validation, figures, document builds | Infers data edges and preserves formal timing/provenance fences |
| Native builds / tests | Make, compiler drivers, test runners | Delegates to semantic owners and budgets nested workers |
| Batch media / data / ML | Independent inputs and deterministic merges | Requires disjoint outputs, bounded resources, and explicit merge semantics |

The scenario catalog includes more than 50 presets covering software, research, containers, media, ML, release, database, and low-level CPU/GPU/I/O work.

## Live execution—not a blank spinner

![Twenty-second live execution demo showing running, ready, completed, failed, and estimated savings](assets/growth/demo.gif)

Long runs use a PTY-backed runner and continuously show:

```text
elapsed 2m 15s · running 4 · ready 2 · completed 7 · failed 0
estimated saved this run 4m 31s · cumulative saved 19m 52s
```

At completion, every atom's status, return code, timeout, skip reason, output truncation, peak concurrency, per-run savings, and cumulative savings are checked.

## Five-minute benchmark

The retained public run executed four isolated low-load workloads through the real parallel executor. Every task ran for at least five minutes.

| Evidence | Result |
| --- | ---: |
| Parallel wall time | **5m 10s** |
| Serial equivalent | **20m 41s** |
| Time saved | **15m 31s** |
| Observed speedup | **4.00×** |
| Parallel efficiency | **100.0%** |

The serial equivalent is the sum of the observed independent task runtimes; it is not a separately executed serial run. This demonstrates scheduler overhead and reporting behavior under controlled independent work, not a universal claim that every project will be 4× faster. See the [visual report](https://cloudguo123.github.io/mac-parallel-accelerator/), [raw evidence](https://cloudguo123.github.io/mac-parallel-accelerator/benchmark-results.json), and [benchmark protocol](BENCHMARKING.md).

## Execution contract

The safety handshake is deliberately strict:

```text
atomic_task_plan
  → complete immutable compiled_plan + plan_hash
  → atomic_exec with that exact object and hash
```

Plans are not translated back into hand-written waves or generic DAG calls. Typed control edges distinguish success, failure, order, data, stream, readiness, health, completion, and cleanup. Artifacts, non-file effects, capacity resources, lifecycle events, and source snapshots remain part of the execution contract.

## Privacy and authorization

- Project and optional trace inspection are local and bounded.
- Trace analysis returns aggregate routing signals—not prompts, reasoning, command bodies, or tool outputs.
- Parallelism changes timing, never permission. Planning does not authorize new commands, remote mutations, destructive cleanup, or retries.
- No run result is uploaded automatically. Sharing is explicit and reviewable.
- Timeouts terminate process groups; timed-out side effects are treated as unknown and are not automatically retried.

Read [SECURITY.md](SECURITY.md) for the threat model and reporting process.

## Development

```bash
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s scripts -p 'test*.py' -v
python3 scripts/self_test.py
uvx ruff check scripts
npm ci && npm run build:indicator
```

Generate public verification and sharing assets:

```bash
python3 scripts/generate_test_report.py
python3 scripts/generate_growth_assets.py
python3 scripts/render_growth_media.py  # optional PNG/GIF, requires Chrome + ffmpeg
```

Useful references:

- [Architecture and safety invariants](DESIGN.md)
- [Atom IR reference](skills/accelerate-local-work/references/atom-ir.md)
- [Benchmark and external-result protocol](BENCHMARKING.md)
- [Contributing](CONTRIBUTING.md)
- [Release history](CHANGELOG.md)

## Help it grow

Try it on one real task, then share the sanitized result card or submit a benchmark. If the planner blocks work that should be safe, that report is just as valuable as a speedup—it identifies the next missing semantic rule.

[Open a benchmark report](https://github.com/cloudguo123/mac-parallel-accelerator/issues/new?template=benchmark.yml) · [Ask a question](https://github.com/cloudguo123/mac-parallel-accelerator/discussions) · [View the roadmap](ROADMAP.md)

[MIT licensed](LICENSE) · [Privacy](PRIVACY.md) · [Terms](TERMS.md)
