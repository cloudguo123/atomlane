# Mac Parallel Accelerator

[![CI](https://github.com/cloudguo123/mac-parallel-accelerator/actions/workflows/ci.yml/badge.svg)](https://github.com/cloudguo123/mac-parallel-accelerator/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Mac Parallel Accelerator is a Codex plugin for semantics-preserving local
acceleration on macOS. It compiles shell commands, package scripts, Make
targets, Compose services, tests, builds, and declared work into a typed Atom
IR before deciding what may run concurrently.

The goal is not maximum process count. The goal is minimum wall time without
changing control flow, corrupting shared state, hiding failures, or
oversubscribing Apple silicon.

## Highlights

- Typed control edges for success, failure, order, data, stream, readiness,
  health, completion, and cleanup.
- Static effect and artifact analysis with fail-closed handling of unknowns.
- Event-driven, capacity-aware scheduling without unrelated wave barriers.
- Native concurrency delegation for Make, Compose, test runners, BuildKit,
  compiler drivers, and other semantic owners.
- macOS resource adaptation for CPU, memory pressure, power mode, thermal
  state, and nested worker pools.
- Immutable public plan hash plus an independently recompiled semantic hash.
- Source-snapshot validation immediately before execution.
- Live elapsed/running/ready/completed/failed counters and per-run plus
  cumulative estimated or measured time savings.
- A catalog of software, research, data, container, media, ML, release, and
  low-level resource optimization scenarios.

## Architecture

```text
entrypoints
   │
   ▼
bounded static frontends
   │  shell · package scripts · Make · Compose · declared atoms
   ▼
typed Atom IR
   │  control · artifacts · effects · claims · lifecycle · assurance
   ▼
canonical compiler + execution gate
   │  conflict checks · data-edge lowering · snapshots · dual hashes
   ▼
event-driven resource scheduler
   │  dynamic readiness · multidimensional admission · native delegation
   ▼
verified execution + live progress + savings ledger
```

Unknown is never treated as empty, read-only, deterministic, or safe.
Unsupported native lifecycle and stream contracts remain advisory and are
rejected by the direct process executor.

## Requirements

- macOS
- Codex with plugin and MCP support
- Python 3.10 or newer
- Ruby is needed only for the safe Compose YAML frontend
- Node.js 20 or newer is needed only to rebuild the browser indicator

The runtime itself has no third-party Python dependency.

## Install from GitHub

```bash
codex plugin marketplace add cloudguo123/mac-parallel-accelerator
codex plugin add mac-parallel-accelerator@mac-parallel-accelerator
```

Open a new Codex task after installation so the current skill and MCP metadata
are loaded.

## Use

Ask Codex naturally, for example:

```text
Use $accelerate-local-work to inspect this build and run the safe parts in parallel.
```

For work expected to exceed ten seconds, the skill uses `scripts/live_runner.py`
in a PTY so progress remains visible while commands are running.

The execution handshake is deliberately strict:

```text
atomic_task_plan
  -> complete compiled_plan + plan_hash
  -> atomic_exec with that exact object and hash
```

Do not translate the compiler result into hand-written legacy waves or DAGs.

## Privacy and safety

- Project and trace inspection is local and bounded.
- Trace analysis is opt-in and returns aggregate routing signals, not prompts,
  reasoning, command bodies, or tool outputs.
- Planning does not grant permission to run new commands or perform new remote
  mutations.
- Unknown effects, ambiguous writers, stale snapshots, changed plans, and
  unsupported lifecycle contracts block execution.
- Timeouts terminate process groups; timed-out side effects have an unknown
  outcome and are not automatically retried.

See [SECURITY.md](SECURITY.md) for reporting and the security model.

## Development

Run the Python checks:

```bash
python3 -m py_compile scripts/*.py
python3 -m unittest discover -s scripts -p 'test*.py' -v
python3 scripts/self_test.py
uvx ruff check scripts
```

Validate the skill when Codex's system skill validator is available:

```bash
uv run --with pyyaml python \
  "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" \
  skills/accelerate-local-work
```

Rebuild the MCP App indicator:

```bash
npm ci
npm run build:indicator
```

The lockfile and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) record the
browser bundle inputs. `node_modules` is never part of the plugin package.

## Documentation

- [Architecture and safety invariants](DESIGN.md)
- [Skill workflow](skills/accelerate-local-work/SKILL.md)
- [Atom IR reference](skills/accelerate-local-work/references/atom-ir.md)
- [Release history](CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).
