# AtomLane 0.16

AtomLane compiles structured local work into an immutable,
semantics-preserving atomic plan and executes that exact verified plan against
the current platform realm and resource envelope. It combines conservative
effect analysis, typed dependencies, native concurrency delegation,
platform-specific supervision, bounded scheduling, live progress, and savings
accounting.

Version 0.16 adds a proof-carrying pytest test-suite frontend. A suite such as
100 independent cases remains one native pytest-xdist invocation: AtomLane
owns the exact immutable plan, declared effects, CPU/memory budget, containment,
live visibility, fresh JUnit validation, and savings accounting, while pytest
owns collection, fixtures, case scheduling, and worker lifecycle.

Version 0.15 adds the plugin-bundled, read-only task-assessment hook and keeps
its lexical routing result separate from project inspection, safety proof, and
execution authorization.

Version 0.14 moves current project-owned source to MPL-2.0 while preserving
all historical MIT grants through 0.13.0. The community release remains free
for personal, research, educational, and commercial use; licensing and
trademark boundaries are explicit and do not change the runtime contract.

Version 0.13 makes AtomLane the canonical plugin, marketplace, MCP, event,
environment, package, and storage identity. It also resolves scenario routes
against the current execution realm: portable resource advice uses the host
planner, while Apple-only accelerator goals stay advisory outside native
macOS instead of naming an unavailable executor.

Version 0.12 adds the native Windows Preview platform adapter while retaining a
single compiler and scheduler core. Windows plans are bound to their realm,
use NT path semantics, and run through staged Job Object supervision with
separate pipe capture or optional ConPTY. The user-visible live surface reports
lifecycle counts and savings; task stdout/stderr is returned at completion.
The Python Candidate IR now proves a portable, explicit `spawn` contract rather
than a macOS-only assumption.

Version 0.11 adds a separate, non-executing Python Candidate IR. It identifies
small program-level parallelism candidates and emits source-hash-bound review
previews, but it never applies those previews or treats static advice as an
executable Atom plan.

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
- Match concurrency to the current native host or Docker VM, nested worker
  demand, memory pressure, power mode, load, and thermal conditions.
- Keep long execution visibly live through lifecycle counts and savings, while
  returning bounded task stdout/stderr with the completed result.
- Find high-confidence Python ordered-map candidates without importing or
  executing the target, and make every missing proof or modeled assumption
  explicit before a source refactor is reviewed.
- Make the per-prompt acceleration preflight visible while keeping it advisory,
  deterministic, read-only, and separate from the proof-carrying planner.
- Accelerate large decoupled pytest suites through a resource-bounded native
  pytest-xdist pool without converting every testcase into an external atom.

## Non-goals

- Parallelism does not grant permission for new commands or mutations.
- The planner does not claim that arbitrary code is pure, idempotent,
  deterministic, retryable, or GPU-enabled.
- The plugin does not proxy arbitrary remote-provider tools. Codex should batch
  independent connector calls through its native orchestration.
- Docker cpusets are not presented as stable Apple performance-core or
  efficiency-core affinity, and ordinary Docker Desktop containers are not
  claimed to receive Metal, GPU, ANE, or media-engine access.
- The lifecycle hook is not an auto-executor, policy bypass, project scanner, or
  proof of independence. It classifies only the submitted prompt and cannot
  authorize, block, rewrite, or run project work. Task-internal compilation
  still occurs at meaningful execution boundaries under skill guidance.
- The Python advisor is not a general auto-parallelizing compiler. It does not
  rewrite arbitrary loops, infer purity from confidence scores, apply patches,
  execute profilers, or claim modeled projections as measured speedups.
- The pytest frontend does not install pytest-xdist, run hidden collection,
  infer complete test effects, or promise CPU affinity. Configured native
  workers are not presented as observed worker concurrency.

## Architecture

```text
User task
   |
   v
Read-only UserPromptSubmit hook
   |
   +---------------- direct / inspect / likely candidate (visible advice only)
   |
   v
Skill execution-boundary preflight
   |
   +---------------- scenario_plan / accelerator / container advice
   |
   +---------------- python_parallel_advisor
   |                    |
   |                    +-- bounded AST + local call graph
   |                    +-- effects / GIL / spawn / nesting gates
   |                    +-- source-hash-bound review preview
   |
   v
atomic_task_plan
   |
   +-- entrypoint and bounded project discovery / test_suite_plan frontend
   +-- frontend expansion and native-owner recognition
   +-- typed control, data, event, and resource edges
   +-- effect and lifecycle classification
   +-- conflict, policy, and nested-parallelism analysis
   +-- realm-bound host resource plan and benefit forecast
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
   +-- process-group or Job Object timeout and failure propagation
   +-- exact process-atom or supported native-delegate execution
   +-- live scheduler events and bounded final output capture
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

## Program-level Python advisor

The Python advisor is intentionally separate from the command-level Atom IR.
It accepts an absolute project boundary, optional project-relative `.py`
paths, optional measured serial hotspot observations, and resource ceilings.
It reads regular UTF-8 files under strict file, byte, AST-node, candidate, and
diagnostic limits. Symlink escapes, oversized inputs, malformed source, and
paths outside the project boundary are rejected or diagnosed. Source modules
are parsed but never imported or executed.

The Python Candidate IR currently recognizes only ordered one-argument maps:

- `results = [worker(item) for item in items]`;
- `return [worker(item) for item in items]`;
- an empty-list initialization followed by
  `for item in items: results.append(worker(item))`.

Workers must resolve to a same-module top-level function. A conservative local
call graph propagates global/nonlocal writes, attribute and subscript writes,
I/O, network, subprocess, database, output, nondeterminism, environment reads,
dynamic execution, unknown calls, generators, async control, and nested
functions. Unknown is a hard effect—not an invitation to guess. Import-time
work, unstable or late worker/helper bindings, reflective namespace access,
effectful iterable evaluation, complex enclosing control, repeated pool
creation, loop-target live-out, fewer than two known items, and uncoordinated
outer/native pools also block a CPU rewrite. The supported portable spawn path
must be a statically linked top-level `__main__` guard. Review previews
must own complete physical lines, preserve comments and final-newline state,
and avoid binding collisions.

Classification and executor selection are distinct:

- pure Python CPU work may become `reviewable_rewrite` with an ordered
  `ProcessPoolExecutor.map` preview, but only with a statically linked
  `if __name__ == "__main__"` path suitable for explicit spawn;
- blocking reads receive thread-pool advice but no patch;
- network and subprocess batches remain advisory because rate limits,
  idempotence, external effects, and cancellation are not statically proven;
- NumPy, SciPy, PyTorch, JAX, Polars, and similar calls prefer native ownership;
- existing pools are coordinated rather than nested;
- every other unresolved case remains `blocked`.

A review preview is a syntax-compiled unified diff bound to the source SHA-256,
candidate location, and recognized pattern. It is invalid after any source
change and is never applied automatically. Runtime picklability, exception
equivalence, result order, produced files, peak memory, and performance remain
validation obligations. Blocked, advisory, and native-owned candidates never
receive a process-pool speedup projection. Without runtime evidence no speedup
is estimated. A provided serial hotspot on a rewrite-eligible CPU candidate may
yield a modeled parallel projection, labeled
`measured_serial_modeled_parallel` and explicitly not a benchmark.

The full contract is maintained in
`skills/optimize-python-parallelism/references/python-program-ir.md`.

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

### Pytest native-worker contract

The 0.16 pytest frontend is deliberately a native-owner adapter, not an
external testcase sharder. `test_suite_plan` lowers one explicitly selected
pytest runner and its selectors into the standard immutable Atom plan, and
`atomic_exec` executes that exact plan. One suite is one outer atom even when
pytest-xdist schedules 100 or more independent cases across several workers.

Planning performs no `pytest --collect-only` pass and imports no test module.
The runner must be an exact Python `-m pytest` module invocation; direct
`pytest`/`py.test` console scripts are refused. The resolved interpreter is
content-hash attested and revalidated immediately before execution while its
virtual-environment invocation path is preserved. `PYTHONPATH`, `PYTHONHOME`,
and `PYTHONOPTIMIZE` are forced empty, and project/config-pythonpath candidates
that could shadow the trusted `pytest` or `xdist` modules are rejected.
Clearing `PYTHONOPTIMIZE` also prevents inherited `-O` semantics from removing
ordinary assertions outside pytest's assertion-rewriting boundary. This binds
the runner selected by the caller; it does not turn the selected Python
environment or installed packages into an AtomLane-managed trust root.
It resolves the effective project-local pytest configuration, binds its exact
path with `-c`, and snapshots it with caller-selected source files. Valid
configuration `addopts` and `PYTEST_ADDOPTS` remain active and their exact token
lists are included in the selection fingerprint after conflicting controls are
rejected. A plain `pyproject.toml` selected only by pytest 8.4's root-directory
fallback is represented by the distinct, hash-bound `fallback_pyproject`
selection kind; runtime validation accepts it only while it still contains no
pytest configuration. Ambiguous discovery requires an explicit `config_path`; Python 3.10
needs importable `tomli` to parse `pyproject.toml`. Test reads, writes,
databases, ports, services, environment mutations, and other non-file effects
must still be declared completely. An incomplete effect model blocks execution.
A multi-worker plan separately requires
`independence_declared=true`; neither a case-count hint nor a complete
suite-level effect list is silently treated as proof of fixture/order
independence. The frontend also refuses native-worker, distribution, base-temp,
JUnit, non-executing, and conflicting plugin controls supplied through runner
arguments, configuration, or `PYTEST_ADDOPTS`; this prevents two authorities
from silently disagreeing. Positional selectors and config `testpaths` or
`pythonpath` values must resolve to existing project-local paths without a
symbolic-link/reparse alias and are revalidated before process launch. Explicit
source snapshots use the same lexical-to-final identity rule. A hash-bound
`--confcutdir=<project_root>` prevents pytest from loading parent-directory
`conftest.py` files beyond the declared project. AtomLane explicitly loads the xdist plugin
so `PYTEST_DISABLE_PLUGIN_AUTOLOAD` remains usable. It disables the shared
pytest cache provider and rejects cache-dependent selection flags. JUnit and
base-temp output paths may not overlap snapshotted inputs, the bound config,
the runner executable, or each other; explicit JUnit outputs must also remain
outside every collection directory in every suite in the plan. Compilation and
runtime use the same case-folded, Unicode-NFC path identity in both cross-suite
directions and also compare physical ancestor/file identities, covering macOS
firmlinks and mount aliases. Windows report paths additionally reject
Win32-lossy spellings: trailing spaces/dots, alternate streams, device names,
root-relative/drive-relative forms, and device namespaces. Unknown third-party pytest options that
consume a value must use `--option=value`; otherwise the frontend cannot safely
distinguish that value from a positional selector. The
selected environment must already contain pytest-xdist for both serial-baseline
and multi-worker execution; AtomLane never installs it. Independent cases use
`worksteal` by default; file/scope/group affinity schedulers are explicit
opt-ins.
The 0.16 release gate covers `macos-14` and `windows-2025`, CPython 3.10–3.13,
pytest 8.4.2, and pytest-xdist 3.8.0; other dependency versions and host images
are outside this version's verified compatibility claim.

Each JUnit and base-temp path is also a runtime lease resource. AtomLane
normalizes and globally sorts those resources, acquires non-blocking
cross-process locks, then repeats semantic/source/path validation while holding
them. Locks remain held through stable JUnit parsing and evidence generation.
Any collision fails fast; it is not queued as capacity work because waiting
would contaminate the run's elapsed-time comparison. A lease set contains the
normalized path key, the physical parent identity plus normalized basename, and
the existing target identity when present; the full set is recomputed after
acquisition. This keeps a key stable when a previously absent output is created
and collapses firmlink/bind-mount aliases. Native Windows discovers the private
lease root through the current token's LocalAppData Known Folder, not
`USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, or `LOCALAPPDATA`.

The compiled worker count consumes the shared CPU capacity budget, and an
optional per-worker memory estimate is multiplied into the suite claim.
`worker_count=auto` is capped by the current host resource plan and any supplied
case-count hint; an explicit count is still a ceiling subject to plan validation. These are scheduling
budgets, not core placement. AtomLane neither requests nor claims CPU affinity;
pytest-xdist and the operating system schedule the native worker processes.

The report distinguishes three facts:

- `native_workers_configured`: the hash-bound pool size requested from xdist;
- `outer_peak_concurrency`: the observed number of AtomLane outer atoms; and
- `native_workers_observed`: unavailable unless a compatible runtime observer
  proves inner process activity.

Configured worker capacity is not observed worker activity, so native-pool
parallel efficiency also remains unavailable without that observer.

A measured comparison is a two-plan protocol. AtomLane first executes the same
selection with `worker_count=1`; a fresh, non-empty, counter-consistent,
all-passing JUnit run with no skipped cases may return session-attested
`serial_baseline_evidence`. Every suite must set
`baseline_source_closure_declared=true`; its `snapshot_paths`, together with the
automatically bound effective-config snapshot, must cover every semantically
relevant selected test, source, helper, project-local plugin, `conftest`, and
configuration.
AtomLane performs bounded static checks over direct selectors, configured
`testpaths`/`pythonpath`, directory roots, and discoverable `conftest` files.
Any symbolic link or reparse point discovered in the collection tree makes the
run ineligible to issue baseline evidence; otherwise a target could change while
the lexical selector remained constant. The check cannot establish closure over arbitrary dynamic imports or plugin
loading, so the boolean is a caller assertion and the resulting attestation is
not proof of complete semantic closure. The multi-worker plan must have the same
selection fingerprint and produce a fresh passing JUnit with the same testcase
identities and outcomes. A bare `serial_baseline_seconds` number is rejected for
a native pytest pool.
Installed pytest/xdist distributions and plugins outside the project are not
content-attested; compatibility therefore also assumes the caller keeps that
trusted environment unchanged between the two runs.

Without compatible attested evidence, the sum of complete, runtime-plausible
testcase timings in the fresh counter-consistent JUnit may provide an explicitly
estimated comparison without rerunning the tests. If neither comparison is
available, per-run savings remain pending and the cumulative ledger is not
credited. Estimated comparisons remain visible and enter only the estimated
ledger bucket; they never enter the primary credited total.

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
only output caps and ordinary-task serial baselines are outside the semantic
plan and do not permit changing it. Native pytest pools accept only the
session-attested baseline protocol described above, not caller-supplied seconds.

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

Every invocation observes the current host's logical and physical CPU count,
memory, load, power state, architecture, and execution realm. macOS additionally
reports the Mac model, performance/efficiency topology, GPU count, thermal
state, and Low Power Mode. Native Windows uses kernel APIs for memory, CPU load,
power, physical topology, and capability probes. Interactive mode is the
default and retains CPU and memory headroom. Balanced and throughput modes
expose progressively more capacity while preserving a reserve. A caller's
maximum concurrency is a ceiling, not an override of adaptive safety limits.

Container budgets derive from the Docker daemon's own envelope. Native Windows,
WSL Linux, and Docker's Linux VM are different realms and never share one
capacity claim or immutable plan. CPU quota,
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

## Windows Preview platform adapter

The portable core never emulates POSIX behavior on native Windows. Each plan
records the operating system, architecture, path flavor, argv transport,
process-tree mechanism, execution realm, and required terminal capability in
its immutable envelope. Execution rejects a plan compiled for another realm.

Windows path conflict analysis canonicalizes drive, UNC, slash, case, and
extended-prefix aliases before ancestry checks. Drive-relative paths, alternate
data streams, and reserved device names fail closed. Exact argv is transported
without a shell. `.cmd` and `.bat` are not accepted as exact argv because
CreateProcess routes them through `cmd.exe` semantics.

Native execution starts an isolated trusted waiting supervisor from a trusted
directory and sanitized environment. The parent opens that supervisor by PID,
assigns it to a Job Object, and only then sends the target specification and
task environment; the supervisor immediately creates the target and normally
created Windows descendants inherit the Job. This is a staged sequence, not an
atomic target-creation guarantee: the current Preview does not use
`CREATE_SUSPENDED` or `PROC_THREAD_ATTRIBUTE_JOB_LIST` for the target.

The Job can enforce kill-on-close, CPU rate, memory, and active-process limits.
CPU and memory limits are Job-wide and therefore include the supervisor plus
the normally inherited target tree; the minimum accepted memory limit is
128 MiB. In pipe mode, `max_processes` is the exact Job-wide active-member
ceiling and includes the supervisor; it is never inflated into an unprovable
target-tree allowance. The minimum is two so the supervisor can create one
target. ConPTY with `max_processes` fails before target code starts because the
console host's Job membership is not proven; CPU and memory controls remain
available. Reported containment and resource scope remain the supervisor and
normally inherited target tree. Separate byte pipes are the
default concurrently drained capture transport; retained bytes are decoded as
UTF-8 with an explicit replacement flag. The live surface reports scheduler
lifecycle counts and savings while the task runs, and returns captured
stdout/stderr at completion. ConPTY is opt-in for programs needing
terminal-shaped output and intentionally reports a combined VT stream. Explicit
ConPTY stdin fails before target creation because a verified terminal-input and
EOF contract is not implemented; bounded stdin remains available through pipes.

The Job boundary does not contain work created by another authority. WSL,
Docker/container daemons, WMI, services, scheduled tasks, and remote-execution
clients are marked as brokers; Job resource limits are rejected for their
brokered workloads and the result reports client-only containment. Querying an
empty Job after termination proves only that no process remains in that Job,
not that an external broker stopped its work.

The PowerShell frontend accepts an existing `.ps1` file only through `pwsh`
with `-NoLogo -NoProfile -NonInteractive -File`. It never splits statements or
claims command-level independence inside the script; the whole file remains an
opaque atom whose effects must be completely declared. POSIX shell, package,
Make, and Compose lowering remain unavailable on native Windows Preview. WSL
uses the Linux contract and Docker uses the daemon/VM contract.

## Tool surfaces

- `test_suite_plan`: compile one explicitly declared pytest suite into the
  standard immutable plan, delegating inner scheduling to a bounded
  pytest-xdist worker pool without executing collection during planning.
- `atomic_task_plan`: compile and validate an immutable atomic execution plan,
  emit blockers/diagnostics, native delegates, resource decisions, forecasts,
  and `plan_hash` without executing project commands.
- `atomic_exec`: verify and execute the exact compiled plan and hash.
- `scenario_plan`: match bounded project/trace evidence to preset optimization
  goals and guardrails; advisory only.
- `python_parallel_advisor`: parse bounded project-local Python without imports,
  execution, or writes; return candidate classifications, proof obligations,
  GIL-aware executor advice, benefit labels, and optional hash-bound previews.
- `task_parallel_scan`: compatibility advisory scan for caller-declared coarse
  units.
- `host_resource_plan`: return observed native host resources, realm, platform
  capabilities, and a concurrency plan.
- `mac_resource_plan`: compatibility alias for `host_resource_plan`; on macOS
  it also returns Apple-specific topology and accelerator facts.
- `mac_accelerator_plan`: recommend an implemented Apple-silicon backend.
- `container_resource_plan`: recommend Docker VM and per-service budgets without
  creating containers or changing Docker Desktop settings.
- `parallel_exec`, `parallel_map`, and `parallel_dag`: lower-level compatibility
  executors for callers that already possess a safe explicit plan. The 0.9 skill
  does not translate a compiled atomic plan back into these payloads.

Commands are argv arrays and run without a shell unless the compiled operation
explicitly requires one. Task count, argv, stdin, concurrency, timeout, and
captured output are bounded. Timeouts terminate the contained POSIX process
group or Windows Job Object; the Windows claim ends at that Job boundary.

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
schema, and cumulative savings store. This live channel does not stream each
task's stdout/stderr; bounded captured output is returned in the completion
result. ConPTY remains an optional target terminal transport, not a UI stream.

Completion reports:

- parallel or serial indicator and observed peak concurrency;
- elapsed time and atom status;
- speedup multiplier and whether it is measured or estimated;
- parallel efficiency;
- `time_saved_seconds` for an eligible successful invocation, clamped at zero;
- provenance-specific `measured_time_saved_seconds` or
  `estimated_time_saved_seconds`;
- `ledger_credit_eligible`, `ledger_credit_recorded`, and
  `credited_time_saved_seconds` so comparison validity is not confused with a
  successful ledger write;
- `overhead_seconds` when an eligible invocation is slower than its comparison;
- `savings_eligible` plus an explicit reason when a failed, timed-out, or empty
  invocation is excluded; and
- primary `cumulative_saved_seconds` for measured credits plus retained legacy
  unclassified values, and a separate `cumulative_estimated_saved_seconds`.

A supplied `serial_baseline_seconds` supports a measured comparison for
ordinary work. Otherwise ordinary multi-atom work uses observed successful atom
durations and never reruns commands merely to benchmark them. A native pytest
pool rejects bare seconds: a measured comparison requires closure-declared,
statically checked, session-attested evidence from a matching `worker_count=1`
run; otherwise only fresh, counter-consistent, complete, runtime-plausible JUnit
testcase timings can support an explicitly estimated comparison. Without one,
savings remain pending and the run does not change the ledger.
Failed, timed-out, and all-skipped invocations likewise do not change the
ledger; successful negative deltas are reported as overhead and credit zero
savings. Ledger v2 records measured and estimated observations in separate
buckets. Only a measured comparison is eligible for the primary cumulative
credit; an estimate remains visible in the estimated bucket. A v1 ledger is
migrated without rewriting history: its existing count and total become
`legacy_unclassified`, and the compatible primary total remains legacy plus new
measured credits. Updates use one locked atomic replacement; an unreadable or
invalid existing file is never silently reset. macOS keeps the legacy-compatible path
`~/Library/Application Support/Codex/AtomLane/stats.json`;
Windows uses `%LOCALAPPDATA%\AtomLane\stats.json`. Tests override this path.

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

The plugin has no third-party runtime dependency and uses Python 3.10+ as
`python3` on supported macOS and Windows hosts.

```bash
python3 scripts/self_test.py
python3 "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" \
  skills/accelerate-local-work
python3 "$HOME/.codex/skills/.system/skill-creator/scripts/quick_validate.py" \
  skills/optimize-python-parallelism
```

Version 0.9.0 adds the immutable Atom IR, typed effect/control planning,
plan-hash execution handshake, native-owner delegation, unknown-effect
fail-closed behavior, and atomic live-run path. Open a new Codex task after
installation so the updated skill metadata and instructions are loaded.

Version 0.9.1 added the reproducible GitHub Pages verification dashboard and
the independent five-minute benchmark with observed per-run and cumulative
time-savings evidence.

Version 0.9.2 canonicalizes integral JSON numbers before semantic and envelope
hashing so immutable plans survive Python/JavaScript MCP round trips without
weakening tamper detection. It also adds reproducible launch assets, aggregate
growth evidence, and a real-project benchmark contribution protocol.

Version 0.11.0 adds the non-executing Python Candidate IR, effect and spawn
proof gates, GIL-aware routing, source-hash-bound review previews, modeled-vs-
measured benefit labels, a dedicated skill, and public safety-fixture evidence.

Version 0.12.0 adds native Windows Preview resource probes, NT path semantics,
staged Job Object supervision, optional ConPTY, `pwsh` file atoms, realm-bound
plans, Windows CI, and portable explicit-spawn Python previews. The CI evidence
comes from `windows-2025` and is not evidence of Windows 11 Desktop UI support.

Version 0.16.0 adds `test_suite_plan` and the pytest native-worker contract:
resource-bounded pytest-xdist delegation, no hidden collection or dependency
installation, immutable effect/configuration evidence, preserved and hash-bound
addopts, collision-free outputs, live configured-worker visibility, attested
serial baselines, fresh validated JUnit summaries, and savings that remain
pending until a compatible measured or plausible JUnit comparison exists.
