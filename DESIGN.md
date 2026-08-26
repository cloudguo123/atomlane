# Mac Parallel Accelerator 0.9

Mac Parallel Accelerator compiles structured local work into an immutable,
semantics-preserving atomic plan and executes that exact verified plan against
the current Mac's resource envelope. It combines conservative effect analysis,
typed dependencies, native concurrency delegation, Apple-silicon routing,
bounded scheduling, live progress, and savings accounting.

The central rule is:

```text
atomic_task_plan -> compiled_plan + plan_hash -> atomic_exec
```

Planning and execution are deliberately separated. The planner owns semantic
interpretation and scheduling decisions. The executor verifies plan identity
and consumes the compiled artifact without rediscovering, weakening, or
repairing it.

## Goals

- Discover useful concurrency below a user-visible task without treating a
  shell command, package script, Make target, Compose project, or test runner
  as an indivisible black box.
- Preserve exact control flow, failure behavior, effects, lifecycle, resource
  leases, and authorization boundaries.
- Fail closed when independence cannot be established.
- Prefer a semantic owner's native worker pool or jobserver when it provides a
  safer execution boundary than external process fan-out.
- Match concurrency to the current Mac, Docker VM, nested worker demand, memory
  pressure, power mode, load, and thermal conditions.
- Keep long execution visibly live and report per-run and cumulative savings.

## Non-goals

- Parallelism does not grant permission for new commands or mutations.
- The planner does not claim that arbitrary code is pure, idempotent,
  deterministic, retryable, or GPU-enabled.
- The plugin does not proxy arbitrary remote-provider tools. Codex should batch
  independent connector calls through its native orchestration.
- Docker cpusets are not presented as stable Apple performance-core or
  efficiency-core affinity, and ordinary Docker Desktop containers are not
  claimed to receive Metal, GPU, ANE, or media-engine access.
- The plugin does not enable a global lifecycle hook. Task-internal compilation
  occurs at meaningful execution boundaries under skill guidance.

## Architecture

```text
User task
   |
   v
Cheap skill preflight
   |
   +---------------- scenario_plan / accelerator / container advice
   |
   v
atomic_task_plan
   |
   +-- entrypoint and bounded project discovery
   +-- frontend expansion and native-owner recognition
   +-- typed control, data, event, and resource edges
   +-- effect and lifecycle classification
   +-- conflict, policy, and nested-parallelism analysis
   +-- Mac resource plan and benefit forecast
   +-- deterministic validation and canonical plan hash
   |
   v
immutable compiled_plan + plan_hash
   |
   v
atomic_exec
   |
   +-- version/hash/precondition verification
   +-- dynamic ready-node scheduling
   +-- capacity and exclusive lease enforcement
   +-- process-group timeout and failure propagation
   +-- exact process-atom or supported native-delegate execution
   +-- live events and bounded output capture
   |
   v
atom results + indicator + elapsed/speedup/savings
```

The existing `task_parallel_scan` remains a compatibility and advisory surface
for callers that already have coarse candidate units. It is not the 0.9
execution contract. New skill-driven local runs use `atomic_task_plan` followed
by `atomic_exec` with the exact compiled result.

## Compiler model

An atom is the smallest operation whose execution contract is complete enough
to schedule safely. It is not necessarily one subprocess or source line. A
native test runner, Make invocation, Compose service group, transaction-like
generator, or supervised daemon may remain one compound atom.

The compiled representation separates:

- operation identity: argv, cwd, environment delta, stdin, and kind;
- typed control: hard, success, failure, order, data, stream, readiness,
  health, service completion, and finally relationships;
- artifact accesses: read, snapshot, create, append, overwrite, delete, and
  transaction, plus typed non-filesystem effects and independent assurance;
- logical resources: Git state, database scope, ports, containers, volumes,
  external accounts, devices, accelerator slots, worker pools, and timing
  provenance;
- lifecycle: bounded, one-shot initializer, daemon, supervisor, or native
  compound executor;
- semantic properties: determinism, idempotence, retryability, cacheability,
  ordering sensitivity, and failure behavior;
- policy fences: authorization, formal evidence, timing validity,
  post-candidate constraints, and related project rules;
- cost: duration, memory, startup cost, resource profile, and nested worker
  demand when known.

The maintained normative details and forward-test cases live in
`skills/accelerate-local-work/references/atom-ir.md`.

### Exact control flow

The default optimization mode preserves observable behavior. `a && b` is a
`success` edge, `a || b` is a `failure` edge, sequential shell or Make recipe
lines retain `order`, and cleanup may use `finally`. A daemon exposes an
`after_ready` or `after_healthy` event while retaining its resource leases; its
dependents do not wait for process exit.

No speculative execution crosses a control edge in exact mode. A future
run-everything diagnostic mode would be explicitly non-equivalent and must be
requested and represented in the compiled plan.

### Unknown effects

Unknown is not equivalent to empty or read-only. An unresolved effect blocks
parallel fan-out, fusion, caching, automatic retry, and reordering. The planner
may retain an authorized source entrypoint as one opaque serial compound atom
only when its original control and isolation boundary remains intact. If that
boundary is also unknown, planning returns a blocker and execution stops.

### Native delegates

Concurrency stays with the semantic owner when appropriate:

- Make may own a sound dependency graph through its jobserver. Prerequisites
  are unordered while recipe lines remain ordered; inferred artifact edges may
  conservatively repair an incomplete graph for the compiled plan. For literal
  Python CLI recipes, a bounded, non-importing AST pass can connect argparse
  path defaults to selected read/write sinks; unsupported code contributes no
  fact and never becomes permission to execute in parallel.
- Docker Compose owns profile expansion, service closure, started/healthy/
  completed conditions, one-shot jobs, restarts, and long-lived services.
- Vitest, Jest, pytest, compiler drivers, BuildKit, BLAS libraries, and similar
  tools may own inner worker pools.

The resource model budgets native inner workers and outer atoms together to
avoid multiplicative oversubscription. External sharding requires disjoint
outputs, reports, caches, databases, and temporary paths plus a deterministic
merge. A native delegate is an execution contract, not a suggestion to explode
the work into legacy subprocesses. If the installed executor does not support
that delegate or its lifecycle events exactly, the plan is advisory and fails
closed at execution.

## Plan identity and TOCTOU boundary

`atomic_task_plan` returns the complete compiled object with `plan_hash`.
Callers pass that entire object as `compiled_plan` and repeat its hash in the
`atomic_exec` request:

```text
compiled = atomic_task_plan(...)
atomic_exec({
  "compiled_plan": compiled,
  "plan_hash": compiled["plan_hash"]
})
```

The caller must not reconstruct or edit the object. A semantic change requires
replanning. `atomic_exec` rejects missing or mismatched hashes, unsupported plan
versions, invalid structure, and failed execution preconditions. Execution-
only output caps and a caller-supplied serial baseline are outside the semantic
plan and do not permit changing it.

The public `plan_hash` covers the complete returned envelope, including its
schedule, resource policy, diagnostics, and native-delegate decisions. A
separate `semantic_hash` covers the normalized Atom/effect/capacity/snapshot
core and is independently recompiled at execution. This catches both envelope
mutation and a planner/executor semantic mismatch before any process starts.

## Scheduling and resource adaptation

The scheduler releases each atom as soon as its own typed predecessors and
leases allow. It does not impose an unrelated stage-wide barrier. Failed
dependencies block their descendants; independent branches may continue unless
the compiled failure policy says otherwise.

Every invocation observes the current chip and Mac model, active logical and
physical CPU count, performance/efficiency topology, GPU count, load average,
memory pressure, thermal state, power source, and Low Power Mode. Interactive
mode is the default and retains CPU and memory headroom. Balanced and throughput
modes expose progressively more capacity while preserving a reserve. A caller's
maximum concurrency is a ceiling, not an override of adaptive safety limits.

Container budgets derive from the Docker Desktop Linux VM envelope. CPU quota,
shares, optional VM-vCPU cpuset, memory, PID, and BuildKit budgets may differ by
service, but the planner retains fixed ports, container names, volumes,
migration authority, and Compose project lifecycle as semantic resources.

## Apple silicon operator routing

`mac_accelerator_plan` detects applicable implementation backends without
silently rewriting arbitrary programs:

- Accelerate/BNNS for vector math, BLAS/LAPACK, DSP, image operations, and
  suitable CPU neural networks;
- Metal/MPSGraph for compatible GPU graphs and custom data-parallel kernels;
- Core ML with all compute units for compatible inference;
- MLX or PyTorch MPS when installed and appropriate;
- VideoToolbox-backed codecs for supported media encode/decode;
- Apple Compression/Apple Archive for compatible compression workflows.

The invoked implementation must expose the backend. Accelerator-driving atoms
normally use low concurrency because GPU, ANE, media engines, unified memory,
and memory bandwidth are shared.

## Tool surfaces

- `atomic_task_plan`: compile and validate an immutable atomic execution plan,
  emit blockers/diagnostics, native delegates, resource decisions, forecasts,
  and `plan_hash` without executing project commands.
- `atomic_exec`: verify and execute the exact compiled plan and hash.
- `scenario_plan`: match bounded project/trace evidence to preset optimization
  goals and guardrails; advisory only.
- `task_parallel_scan`: compatibility advisory scan for caller-declared coarse
  units.
- `mac_resource_plan`: return observed Mac resources and a concurrency plan.
- `mac_accelerator_plan`: recommend an implemented Apple-silicon backend.
- `container_resource_plan`: recommend Docker VM and per-service budgets without
  creating containers or changing Docker Desktop settings.
- `parallel_exec`, `parallel_map`, and `parallel_dag`: lower-level compatibility
  executors for callers that already possess a safe explicit plan. The 0.9 skill
  does not translate a compiled atomic plan back into these payloads.

Commands are argv arrays and run without a shell unless the compiled operation
explicitly requires one. Task count, argv, stdin, concurrency, timeout, and
captured output are bounded. Timeouts terminate the whole process group.

## Live execution and user-visible progress

Codex Desktop may buffer MCP progress while a tool call is active. For plans
expected to exceed ten seconds, the skill writes the exact `atomic_exec`
arguments to workspace JSON and starts:

```bash
python3 scripts/live_runner.py --mode atomic --input /absolute/path/to/input.json
```

in a PTY. It polls about every five seconds and surfaces elapsed time,
running/ready/completed/failed counts, and current estimated savings. The live
runner and direct `atomic_exec` share the same plan verifier, scheduler, result
schema, and cumulative savings store.

Completion reports:

- parallel or serial indicator and observed peak concurrency;
- elapsed time and atom status;
- speedup multiplier and whether it is measured or estimated;
- parallel efficiency;
- `time_saved_seconds` for the invocation;
- `cumulative_saved_seconds` across completed invocations.

A supplied `serial_baseline_seconds` supports a measured comparison. Otherwise
the estimate uses observed non-skipped atom durations and never reruns commands
merely to benchmark them. Completed invocations atomically update
`~/Library/Application Support/Codex/Mac Parallel Accelerator/stats.json`;
tests override this path.

## Scenario catalog

`catalog/scenarios.json` remains an advisory routing layer for recurring
research, software, data, document, media, ML, container, hardware, release,
and operations workloads. It contains execution modes, isolation rules,
optimization goals, and serial guardrails. Project inventory and optional trace
sampling are bounded and privacy-preserving; trace evidence contains aggregate
signatures and task names, not prompts, reasoning, command bodies, or outputs.

Scenario similarity never proves atom independence. The atomic compiler remains
the execution authority.

## Safety invariants

- Plan and execution authorization are separate; neither expands user scope.
- The exact compiled plan and hash cross the planning/execution boundary.
- Unknown effects fail closed.
- Shared writes, fixed sidecars, database mutations, ports, volumes, devices,
  and external authorities remain ordered or isolated.
- Formal timing and provenance fences override apparent file independence.
- Automatic retries require explicit idempotent and retryable semantics.
- A successful scheduler transport does not imply every atom passed; atom
  return codes, stderr, skips, timeouts, and truncation are inspected.

## Validation and installation

The plugin has no third-party runtime dependency and uses the macOS system
`python3`.

```bash
python3 scripts/self_test.py
python3 "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" \
  skills/accelerate-local-work
```

Version 0.9.0 adds the immutable Atom IR, typed effect/control planning,
plan-hash execution handshake, native-owner delegation, unknown-effect
fail-closed behavior, and atomic live-run path. Open a new Codex task after
installation so the updated skill metadata and instructions are loaded.
