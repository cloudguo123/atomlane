# Benchmarking and external results

AtomLane reports two different kinds of evidence. Keep them separate:

- **Controlled scheduler benchmark:** isolated workloads with known independence, useful for measuring scheduler overhead, live lifecycle/savings reporting, and savings arithmetic. Captured task output is completion evidence, not a real-time UI stream.
- **Project benchmark:** a real project command or workflow, useful for measuring practical benefit and discovering safety blockers.
- **Python refactor validation:** a before/after program comparison in which the
  serial observation is measured, the advisor projection remains modeled, and
  the transformed implementation earns a performance claim only after semantic
  equivalence and repeatable measurements pass.

Never label an estimate as measured. Never rerun a stateful or destructive task solely to obtain a baseline.

## Python refactor acceptance protocol

The `python_parallel_advisor` does not execute target code and therefore cannot
produce a measured speedup. With a supplied hotspot it reports
`measured_serial_modeled_parallel`; without one it reports `not_estimated`.
Neither is a benchmark result.

Before accepting a reviewable preview:

1. Confirm the preview's `source_sha256` still matches the exact file and reject
   it after any edit.
2. Review worker picklability, import-time behavior, exception timing, ordering,
   cancellation, external effects, and nested/native worker budgets.
3. Compile the transformed source without importing it.
4. Run serial and parallel implementations on deterministic fixtures under an
   explicit `spawn` context on every claimed target platform; compare values,
   order, exception types/messages,
   stdout/stderr policy, and hashes of every produced file.
5. Measure at least three safe repetitions with identical cold/warm state;
   report p50, p90 when meaningful, peak RSS, worker count, item count, and
   chunksize. Include serialization and pool startup in parallel wall time.
6. Reject the refactor if correctness differs, memory exceeds its budget, the
   workload is below its amortization threshold, or measured p50 regresses.

The public dashboard's Python safety fixture is a static contract test, not a
performance benchmark. It proves that representative pure CPU, shared-state,
read-I/O, and native-library shapes are separated without executing targets.

## Minimum evidence

A publishable project result includes:

1. Plugin version and commit.
2. Native host model, OS version, architecture, power state, logical CPU count,
   and execution realm. On macOS include Low Power Mode. On Windows record the
   staged supervisor assignment, requested and queried Job limits, containment
   scope, broker boundary, and terminal mode. Job CPU/memory budgets include the
   supervisor and normally inherited target tree; memory requests must be at
   least 128 MiB. Report WSL and Docker VM facts separately. A `windows-2025`
   runner result must not be labeled Windows 11 Desktop UI evidence.
3. Project category and a sanitized description of the entrypoint.
4. Whether the run was cold or warm; cache and dependency state must be explicit.
5. Exact success criteria and output hashes or other correctness checks.
6. Parallel wall time and either a separately measured safe serial baseline or a clearly labeled serial equivalent.
7. Per-run savings, speedup, peak concurrency, failures, skips, and planner blockers.
8. At least three repetitions for stable work; report median (p50) and slowest observation (p90 when enough samples exist).

For Windows, report the live lifecycle/count/savings samples separately from
captured stdout/stderr, which is returned in the completed result. If ConPTY is
used, record that stdout and stderr are intentionally combined. Do not describe
the current supervisor-first, PID-assignment sequence as atomic creation: the
Preview does not use `CREATE_SUSPENDED` or `PROC_THREAD_ATTRIBUTE_JOB_LIST` for
the target. An empty queried Job proves only the Job is empty; it says nothing
about work handed to WSL, Docker, WMI, services, scheduled tasks, or another
broker.

Do not publish project names, paths, prompts, command bodies, tool output, environment variables, or file contents unless you have reviewed and intentionally disclosed them.

## Three useful real-project profiles

### Web quality gate

Typical entrypoint: lint, typecheck, tests, and production build.

Important checks:

- Preserve `&&` success control in exact mode.
- Treat `.next`, `dist`, coverage, JUnit, TypeScript build info, caches, and test databases as artifacts.
- Prefer one native Vitest, Jest, or compiler invocation when it owns a safe internal worker pool.

### Docker / Compose pipeline

Typical entrypoint: build several images, start healthy dependencies, run one-shot migrations, execute isolated tests, and collect reports.

Important checks:

- Record the Docker Desktop VM CPU and memory envelope.
- Preserve readiness, health, and successful-completion events.
- Model fixed ports, project names, container names, volumes, BuildKit capacity, and registry access.
- A container cpuset selects VM vCPUs; it does not select stable Apple performance or efficiency cores.

### Research and paper pipeline

Typical entrypoint: prepare data, calibrate or fit, validate, generate figures/tables, and build a document.

Important checks:

- Infer producer/consumer edges even when a Make aggregate lists prerequisites as siblings.
- Version or isolate result, figure, auxiliary, and temporary paths.
- Give formal timing and provenance runs an exclusive host fence.
- Mark correctness-only replays ineligible for the formal performance claim.

## Recommended procedure

1. Commit or otherwise snapshot the source state.
2. Ask Codex to plan only; review blockers and predicted concurrency.
3. Run through the live atomic runner.
4. Verify outputs and collect the sanitized JSON summary.
5. Repeat under the same cold or warm condition.
6. Submit the result with the [benchmark issue form](https://github.com/cloudguo123/atomlane/issues/new?template=benchmark.yml).

Negative results are welcome. A safe refusal, a 1.00× result, a resource regression, or an incorrect scenario classification is actionable evidence.

## Maintainer acceptance

External results are listed only when the method is reproducible, the comparison is honestly labeled, and correctness evidence is present. Maintainers may request a smaller public fixture when the original project cannot be shared.
