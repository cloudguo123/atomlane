# Benchmarking and external results

AtomLane reports two different kinds of evidence. Keep them separate:

- **Controlled scheduler benchmark:** isolated workloads with known independence, useful for measuring scheduler overhead, live reporting, and savings arithmetic.
- **Project benchmark:** a real project command or workflow, useful for measuring practical benefit and discovering safety blockers.

Never label an estimate as measured. Never rerun a stateful or destructive task solely to obtain a baseline.

## Minimum evidence

A publishable project result includes:

1. Plugin version and commit.
2. Mac model, macOS version, power source, Low Power Mode, and logical CPU count.
3. Project category and a sanitized description of the entrypoint.
4. Whether the run was cold or warm; cache and dependency state must be explicit.
5. Exact success criteria and output hashes or other correctness checks.
6. Parallel wall time and either a separately measured safe serial baseline or a clearly labeled serial equivalent.
7. Per-run savings, speedup, peak concurrency, failures, skips, and planner blockers.
8. At least three repetitions for stable work; report median (p50) and slowest observation (p90 when enough samples exist).

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
6. Submit the result with the [benchmark issue form](https://github.com/cloudguo123/mac-parallel-accelerator/issues/new?template=benchmark.yml).

Negative results are welcome. A safe refusal, a 1.00× result, a resource regression, or an incorrect scenario classification is actionable evidence.

## Maintainer acceptance

External results are listed only when the method is reproducible, the comparison is honestly labeled, and correctness evidence is present. Maintainers may request a smaller public fixture when the original project cannot be shared.
