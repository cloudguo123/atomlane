# AtomLane Windows Preview

Windows Preview is a deliberately narrow native-Windows execution target. It
uses the same Atom IR, conflict checker, resource-aware scheduler, immutable
plan hash, live progress, and savings accounting as the macOS implementation.
It is a platform adapter, not a second planner or a compatibility fork.

Preview means that the supported boundary below is release-gated, while wider
Windows shell and filesystem semantics still fail closed. The current CI gate
runs on GitHub's `windows-2025` image with CPython 3.10, 3.11, 3.12, and 3.13.

## Supported boundary

| Area | Windows Preview contract |
| --- | --- |
| Host | AtomLane itself runs under native Windows as the `windows_native` realm. |
| Plans | A plan is bound to OS family, realm, architecture, NT path flavor, argv transport, process-tree backend, and required terminal modes. Recompile after moving it to another realm. |
| Commands | Direct executable argv with `shell=False`; direct `.cmd` and `.bat` argv are rejected. |
| PowerShell | One existing `.ps1` file is one snapshotted atom, launched by `pwsh -NoLogo -NoProfile -NonInteractive -File`. |
| Process scope | A waiting supervisor is opened by PID and placed in a kill-on-close Windows Job Object before it receives the launch record and creates the target. Normal inherited target descendants share the Job; broker-created WSL, Docker, WMI, service, scheduled-task, or remote work does not. This is staged supervision, not atomic target creation. |
| Resources | Optional Job-wide CPU hard-cap percentage and memory limit cover the supervisor plus the normally inherited target tree. Memory is at least 128 MiB. In pipe mode, `max_processes` is an exact Job-wide active-member ceiling of 2–4096; the verified supervisor consumes one slot while alive. ConPTY with `max_processes` fails before target code starts because console-host Job membership is not yet proven; CPU and memory limits remain available. |
| Output | Separate byte pipes, decoded as UTF-8 with replacement, by default; ConPTY is opt-in for programs that need output-side terminal semantics. Pipes are drained concurrently and capture remains bounded; user-visible counters and savings are emitted while work is running. Captured task output is returned in the final result. |
| Python advice | The static advisor can target Windows and emits an explicit `multiprocessing.get_context("spawn")` process-pool preview for eligible CPU maps. |
| Pytest pools | `test_suite_plan` uses an exact Python `-m pytest` runner and one bounded pytest-xdist pool, binds `--confcutdir` to the project, preserves pytest 8.4's hash-bound plain-`pyproject.toml` root fallback, rejects link/path aliases and same- or cross-suite JUnit/collection overlap with lexical plus physical identities, and leases each JUnit/base-temp path across revalidation, execution, and report parsing. Windows derives that private lease root from the profile directory bound to the current process token rather than mutable profile environment variables. The 0.16 release matrix exercises pytest 8.4.2 and pytest-xdist 3.8.0 on native Windows with CPython 3.10–3.13; other dependency versions are not release-gated by this version. |
| Docker | Resource advice can target an identified Linux Docker daemon, including Docker Desktop. Windows containers are advisory-only. |

The minimum dependable workflow is therefore a local native-Windows project
using canonical absolute paths, direct executable argv or whole-file
PowerShell (`pwsh`) scripts, and effects that AtomLane can prove or that the
caller has declared completely. The plugin manifest launches `python3`, so an
actual Python 3.10+ interpreter must be available under that command on `PATH`.
The current Python Install Manager supplies this compatibility alias; a disabled
or redirecting Store alias without an installed runtime is not sufficient.

## Native process and terminal model

AtomLane does not build one command string and ask a shell to reinterpret it.
For pipe mode, argv is transported as JSON to the waiting Windows supervisor
and the target is created with `shell=False`. This preserves argument
boundaries and avoids accidental `cmd.exe` expansion of `%VAR%`, `&`, `|`,
redirection, or caret escapes.

AtomLane starts the waiting supervisor with an isolated interpreter, trusted
working directory, and sanitized environment. The parent opens that supervisor
by PID, assigns it to the Job Object, and only then sends the launch record and
target environment; the supervisor immediately creates the target. Normally
created Windows descendants inherit the Job. This sequence prevents target-
controlled code from running before the supervisor assignment, but it is not
an atomic target-creation contract: the Preview does not create the target with
`CREATE_SUSPENDED` or attach `PROC_THREAD_ATTRIBUTE_JOB_LIST`. Job creation,
assignment, or resource-limit failures are execution failures; AtomLane does
not silently fall back to uncontained execution.

CPU and memory limits are Job-wide, so the supervisor's own usage consumes part
of the same budget as the target and its normally inherited descendants. The
minimum accepted memory limit is 128 MiB. `max_processes` is the exact ceiling
for all active Job members, not a separate target-tree promise. It is accepted
from 2 through 4096 in pipe mode; the supervisor consumes one member slot while
alive, so the target has at most `max_processes - 1` slots at launch. After the
supervisor exits, the kernel still enforces the same Job-wide total and no
capacity is added beyond the request. ConPTY may involve a console host whose
Job membership is not documented strongly enough to make this control
predictable, so ConPTY with `max_processes` fails before target code starts.
CPU and memory limits remain available with ConPTY. On timeout or
cancellation AtomLane terminates the Job and queries it until it reports zero
active processes. That proves only that the Job is empty, not that work
delegated through an external broker stopped.

Use `terminal_mode: "pipes"` unless the program changes output behavior when attached
to a console. `terminal_mode: "conpty"` uses the native ConPTY API and returns
one combined VT output stream, so stderr is not independently attributable.
ConPTY requires Windows 10 version 1809 or later and must be present when the
plan is compiled and executed. It provides output-side terminal semantics and
concurrent bounded draining, not a live-output event feed or an interactive
keyboard session. Explicit ConPTY `stdin`, including an empty string, fails
before target creation because this Preview has no verified terminal-input and
EOF contract. Use pipes for bounded stdin. The live progress
surface reports scheduler counters and savings during execution, while captured
stdout/stderr is returned at completion.

## PowerShell is a whole-file atom

The `powershell_file` frontend preserves a script as one indivisible operation.
AtomLane snapshots the `.ps1` source, keeps its arguments as separate argv
items, and verifies the immutable plan and source snapshot before execution. It
does **not** split statements, pipelines, script blocks, functions, jobs, or
PowerShell control flow into speculative parallel atoms.

The adapter requires `pwsh` on `PATH`. Windows PowerShell 5.1
(`powershell.exe`) is not substituted automatically. Because script contents
are not effect-inferred in this Preview, execution remains blocked unless the
effect model is explicitly complete; set `effects_declared_complete: true`
only after declaring every file and non-file effect. A missing `pwsh` produces
a plan blocker rather than a shell fallback.

Native Windows currently rejects the automatic `shell`, `package_script`,
`make_target`, and `compose_services` frontends because those frontends have
POSIX parsing assumptions. In particular, npm scripts normally run through
`cmd.exe` on Windows and may select another shell through `script-shell`.
AtomLane therefore does not apply POSIX tokenization to npm operators, batch
syntax, or an unknown package shell. If such work must run in Preview, keep the
native tool invocation outside the automatic frontend as one semantically
opaque, serial operation. Model it as an explicit atom only when its exact
invocation and complete effect boundary can be declared; do not classify
commands inside it as independent.

## Windows, WSL, and Docker are separate realms

AtomLane never treats these as one execution or resource envelope:

- **Native Windows** is `windows_native`, uses NT paths, the Windows supervisor,
  Job Objects, and optionally ConPTY.
- **WSL** is detected as `wsl_linux`. When AtomLane itself runs inside one WSL
  distribution, it follows Linux/POSIX process semantics; it is not a native
  Windows run and does not use Job Objects or ConPTY. Calling `wsl.exe` from a
  native plan does not make the commands inside the distribution analyzable.
- **Docker** is identified from the active Docker context plus daemon identity
  and daemon OS. Its reported CPU and memory are the daemon/VM envelope, not
  the Windows host envelope. Docker Desktop CPU-set IDs are VM vCPUs, not
  stable physical-core identities.

Job inheritance covers processes created normally by the contained Windows
client; it is not a claim about every process the task can cause another
authority to create. It does not prove containment of work created by an
external broker.

AtomLane marks `wsl.exe`, Docker/container CLIs, WMI, service control, scheduled
tasks, SSH/WinRM, and similar clients as broker boundaries. Job CPU, memory, and
process limits are rejected for those targets because they would constrain only
the client, not the Linux distribution, daemon, service, remote host, or other
authority that performs the actual work.

A compiled plan cannot move between native Windows and WSL or to a different
OS/architecture contract without recompilation. Moving it to another Windows
host is not a portability promise either: its absolute source snapshots and
all platform fields still have to match, and local recompilation is the safe
default. Cross-realm file paths, dependencies, locks, ports, and completion
signals are not inferred. Model a realm bridge explicitly or keep the combined
operation opaque and serial.

Docker resource output is apply-safe only when the probed daemon is available
and reports `OSType=linux`. Host-derived fallback capacity is a warning to
verify Docker Desktop settings, not proof of the VM budget. The Preview does
not change Docker Desktop's VM-wide settings, promise host-core affinity, or
claim support for Windows containers. Native-Windows Compose frontend lowering
also remains unavailable even though Linux-daemon resource advice is exposed.

## NT path and conflict semantics

Windows file resources are normalized conservatively before conflict checks:

- drive-letter, slash direction, Unicode normalization, and case differences
  cannot bypass an overlap;
- UNC paths and `\\?\UNC\...` spellings are normalized into one UNC form;
- an extended drive prefix (`\\?\C:\...`) is compared with its ordinary drive
  spelling;
- a file and its alternate data stream are considered overlapping;
- trailing spaces and dots are collapsed for identity checks; and
- drive-relative paths such as `C:output\result.json`, device-namespace paths,
  and reserved DOS device names are rejected.

Case folding is intentionally conservative even on a directory with NTFS
per-directory case sensitivity: it may serialize safe work, but it should not
create a missed conflict merely because case behavior differs by directory.
Windows environment variable names are likewise compared case-insensitively,
so declarations such as `PATH` and `Path` in one task are rejected.

This is a static namespace model, not a proof of final NTFS file identity.
Junctions, symlinks, hard links, reparse points, `SUBST` drives, mapped shares,
and two names for the same remote object can still alias after normalization.
For Preview, use one canonical path spelling for each resource and do not admit
automatic concurrency across such aliases. Declare a shared logical effect or
keep the affected atoms ordered when final identity is not proven. AtomLane
does not lock arbitrary target files to discover conflicts at runtime; a
sharing violation is an execution error, not a substitute for the access
model. Windows long-path policy and remote-share behavior also remain host
configuration concerns.

Pytest outputs are a deliberate exception to the “no arbitrary target-file
locking” rule: AtomLane gives every JUnit and base-temp path a sorted,
non-blocking cross-process lease and revalidates its canonical parent while the
lease is held. Explicit JUnit spellings with Win32 trailing-space/dot aliases,
alternate data streams, reserved device components, drive/root-relative forms,
or device namespaces fail before launch. Case, slash, and Unicode-equivalent
spellings share the same conservative lease identity.

## Python `spawn` contract

The Python advisor is static: it parses source without importing or executing
the target and never applies its preview automatically. For an eligible pure
Python CPU map on a Windows target, the preview uses
`ProcessPoolExecutor(..., mp_context=multiprocessing.get_context("spawn"))`.
The same explicit start method is required during differential validation on
every platform so Linux or macOS testing cannot hide Windows import behavior.

Before accepting a preview, prove all of the following:

- the worker is a module-level importable callable;
- arguments and results are pickleable under `spawn`;
- process creation is reachable only behind a safe
  `if __name__ == "__main__":` entry boundary;
- importing the main module does not repeat top-level work or side effects;
- ordering, exceptions, output, cancellation, and memory use remain equivalent;
  and
- one outer/inner resource budget prevents nested pool multiplication.

Windows `ProcessPoolExecutor` has a 61-worker ceiling. AtomLane caps Windows
advice at 61, but that is an API limit, not a useful default. REPL and notebook
workers, dynamically defined callables, and frozen executables are outside the
automatic proof boundary. A frozen application needs its own reviewed
`multiprocessing.freeze_support()` startup design.

## Known Preview limitations

- Release evidence currently covers GitHub `windows-2025` with CPython 3.10,
  3.11, 3.12, and 3.13; other Windows versions, Python versions, architectures, enterprise
  policies, nested-Job configurations, and Windows Desktop UI integration are
  not release-gated. In particular, this is not Windows 11 Desktop UI proof.
- Target creation is staged after supervisor assignment, not atomic; the
  Preview does not use `CREATE_SUSPENDED` or
  `PROC_THREAD_ATTRIBUTE_JOB_LIST` for the target.
- PowerShell source is not yet parsed into an AST and is never decomposed.
- `cmd.exe`, `.cmd`, `.bat`, npm/package scripts, Make recipes, and Compose
  entrypoints have no native automatic lowering.
- ConPTY combines stdout and stderr and is unsuitable when their separation is
  part of the task contract.
- ConPTY launch stdin is rejected before target creation because this Preview
  has no verified terminal-input and EOF contract. Use `terminal_mode: "pipes"`
  for bounded stdin and observable EOF semantics.
- Interactive prompts, UAC elevation, GUI automation, Windows services, and
  detached processes that require escape from the Job Object are not covered.
- The 128 MiB minimum is a Job-wide floor, not 128 MiB exclusively available
  to the target; supervisor overhead is part of the same memory budget.
- NTFS/reparse/remote-share alias identity is not fully proven; ambiguous
  work stays serial or requires an explicit shared-effect declaration.
- WSL distributions, native Windows, Docker daemons, and Windows containers are
  never auto-merged into one schedule or capacity budget.
- Python previews remain hash-bound review artifacts until correctness and
  performance validation succeeds.

## Quick verification on native Windows

Run these commands from the repository root in PowerShell:

```powershell
python3 --version
pwsh --version
python3 -m compileall -q scripts
python3 -m unittest discover -s scripts -p "test_platform_adapter.py" -v
python3 -m unittest discover -s scripts -p "test_windows_runtime.py" -v
python3 -m unittest scripts.test_pytest_test_plan.RealPytestXdistIntegrationTests -v
python3 scripts/self_test.py
```

`test_windows_runtime.py` is intentionally a native release gate: on Windows it
checks staged supervisor assignment, queried Job limits, in-Job child
termination, ConPTY combined capture, UTF-8 MCP transport, scheduler-counter
progress before completion, startup isolation, bounded descendant-pipe cleanup,
argv/environment hazards, and whole-file PowerShell planning. The critical
Windows test class does not convert missing runtime capabilities into skipped
passes.

The 0.16 release gate runs the pytest integration command with pytest 8.4.2 and
pytest-xdist 3.8.0 in the selected Python environment. It executes a real 100-case suite through the
native worker pool and verifies fresh JUnit evidence, measured serial-baseline
comparison, and live/result accounting rather than only parsing a Windows plan.

For the full local regression suite, run:

```powershell
python3 -m unittest discover -s scripts -p "test*.py" -v
```

If Docker Desktop is part of the intended workflow, also record the active
daemon boundary:

```powershell
docker context show
docker info --format '{{json .}}'
docker compose version
```

Confirm that `docker info` reports a Linux daemon before treating AtomLane's
container resource plan as apply-safe. A passing native test run does not make
a plan portable to WSL or to a different Docker context; compile and validate
again in the realm where execution will occur.
