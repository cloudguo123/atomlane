# OpenAI community showcase submission

## Project

Mac Parallel Accelerator

## One-line description

A Codex plugin that compiles local macOS work into semantics-safe atomic plans, executes only proven concurrency, keeps long runs visibly live, and reports per-run and cumulative time saved.

## What problem does it solve?

Builds, tests, Docker pipelines, paper workflows, and batch tasks often contain independent work, but naïve command fan-out can change `&&`/`||` behavior, race shared outputs, oversubscribe native worker pools, or hide failures. This plugin performs bounded static analysis, represents control, artifacts, effects, lifecycle, and resources in a typed Atom IR, then fails closed when independence cannot be proven.

## Evidence

The public test dashboard retains a controlled five-minute run: four independent workloads completed in 5m10s of wall time versus 20m41s serial equivalent, saving 15m31s at 4.00× observed speedup. The serial equivalent is the sum of observed task durations, not a separately rerun serial workflow, and the project does not claim every workload will achieve this result.

## Links

- Source: https://github.com/cloudguo123/mac-parallel-accelerator
- Live report: https://cloudguo123.github.io/mac-parallel-accelerator/
- Installation: https://github.com/cloudguo123/mac-parallel-accelerator#install-in-two-commands

## Open-source status

MIT licensed. macOS, Python 3.10+, no third-party Python runtime dependency. Project inspection and optional trace analysis remain local; results are never uploaded automatically.
