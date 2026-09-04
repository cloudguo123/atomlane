# Changelog

All notable changes to this project are documented here.

## 0.16.0 - 2026-09-04

- Added `test_suite_plan`, a proof-carrying pytest frontend that compiles one
  suite into the standard immutable Atom plan and delegates independent case
  scheduling, fixtures, collection, and worker lifecycle to pytest-xdist.
  The release matrix pins pytest 8.4.2 and pytest-xdist 3.8.0.
- Added shared CPU and optional per-worker memory budgeting for native pytest
  pools, per-run base-temp and hash-bound JUnit effects, effective configuration
  and explicit source snapshots, suite-level timeouts, and refusal of
  conflicting xdist options or incomplete declared effects. Valid config
  `addopts` and `PYTEST_ADDOPTS` are preserved and hash-bound after unsafe
  controls are rejected; output paths may not overlap snapshotted/config/runner
  inputs. Multi-worker execution also requires an explicit case-independence
  declaration; a case-count hint is never treated as proof.
- Bound positional selectors plus config `testpaths`/`pythonpath` to existing
  project-local, non-link identities; explicit source snapshots follow the same
  lexical-to-final identity rule and are revalidated immediately before execution.
  Any symbolic link/reparse point in the audited collection tree disables
  serial-baseline issuance, preventing link retargeting from reusing stale
  timing evidence. Native
  pools explicitly load AtomLane's xdist plugin even when pytest plugin
  autoloading is disabled; pytest's shared cache provider is disabled and its
  cache-dependent selection flags are rejected. Unknown third-party options
  that take values must use the unambiguous `--option=value` form.
- Preserved pytest 8.4's plain-`pyproject.toml` root fallback as an explicit,
  hash-bound configuration-selection kind, and revalidated that it remains a
  fallback rather than silently becoming active pytest configuration.
- Bound `--confcutdir` to the declared project root so pytest cannot execute an
  unsnapshotted parent-directory `conftest.py`; recursively discoverable nested
  conftests are required in the declared baseline source closure.
- Added sorted non-blocking cross-process leases for every pytest JUnit and
  base-temp path, held across final revalidation, execution, stable report
  parsing, and evidence generation so concurrent runs cannot consume or
  overwrite one another's evidence. Lease keys combine normalized paths,
  physical parent-plus-basename identities, and existing target identities,
  closing firmlink/bind-mount aliases and the absent-to-created output race.
  Native Windows resolves the private lease root through the current token's
  LocalAppData Known Folder instead of mutable profile environment variables.
- Applied case-folded, Unicode-normalized collection/output overlap checks in
  both compilation and runtime validation, augmented by physical ancestor/file
  identities and applied in both directions across multiple suites, so
  filesystem aliases cannot turn JUnit or base-temp output into test-source
  overwrite.
- Rejected ambiguous Windows JUnit spellings before filesystem resolution,
  including trailing spaces/dots, alternate data streams, reserved device
  components, drive/root-relative forms, and device namespaces.
- Kept planning non-executing: AtomLane does not run a hidden collect pass,
  import project tests, or install pytest-xdist. Worker budgets are not CPU
  affinity and remain scheduled by pytest-xdist and the operating system.
- Restricted runners to exact Python `-m pytest` module invocations, hash-bound
  and revalidated the selected interpreter, cleared
  `PYTHONPATH`/`PYTHONHOME`/`PYTHONOPTIMIZE`,
  and rejected project/config-pythonpath candidates that could shadow the
  trusted pytest or xdist modules. Direct console-script runners fail closed.
- Added live native-pool context and honest concurrency labels that distinguish
  hash-bound configured workers from observed AtomLane outer concurrency;
  native worker count and parallel efficiency remain unavailable without
  compatible inner-worker instrumentation.
- Added fresh bounded JUnit parsing with declared-counter checks, testcase
  identity, and timing plausibility. Native pools reject bare baseline seconds:
  a successful matching `worker_count=1` run can issue session-attested baseline
  evidence only when every suite sets
  `baseline_source_closure_declared=true`, passes bounded static source-coverage
  checks, and snapshots its caller-declared semantic source closure. The checks
  cover statically discoverable selectors, config paths, and `conftest` files;
  dynamic imports and plugins remain part of the caller assertion, so this is
  not described as proof of complete semantic closure.
  Installed pytest/xdist distributions and external plugins remain a documented
  caller-trusted environment that must not change between comparison runs.
  Compatible evidence produces measured comparisons; complete plausible JUnit
  timings produce explicitly estimated comparisons; otherwise per-run savings
  remain pending. Added the v2 savings ledger: measured credits and estimates
  have separate counters/totals, only measured evidence enters the primary
  cumulative total, v1 totals are retained as `legacy_unclassified`, and an
  invalid existing ledger is never silently overwritten. Result/UI fields
  distinguish comparison eligibility, credit eligibility, and a successful
  ledger write.

## 0.15.0 - 2026-09-02

- Added a real plugin-bundled `UserPromptSubmit` hook so AtomLane appears in
  Codex Hooks and performs a fast, visible, advisory assessment on every task.
  The hook never executes project commands, blocks a prompt, or treats a
  lexical candidate as proof that parallelism is safe. Its Python interpreter
  starts in isolated, no-site mode on macOS and Windows.
- Kept selection workload-aware: direct requests stay direct, ambiguous local
  work is inspected at the first execution boundary, and likely parallel work
  is routed through the existing atomic planner and live progress contract.
- Made the MCP App indicator backward-compatible with versioned resource URIs
  cached by older tasks, including `0.13.0`, while continuing to reject
  unrelated or malformed resources.
- Corrected invalid `resources/read` failures to use a JSON-RPC error instead
  of a tool-result payload, preventing the host-side `Unexpected response
  type` failure mode even for malformed parameters or internal resource errors.
- Removed the root Agent Plugins manifest because current Codex releases
  classify that package format separately and suppress its bundled lifecycle
  hooks. The Codex-native manifest, skills, MCP configuration, and hook remain
  packaged together; root `mcp.json` remains available as an optional portable
  local-stdio configuration.

## 0.14.0 - 2026-09-02

- Moved current project-owned source to the OSI-approved Mozilla Public
  License 2.0 while keeping personal, research, educational, and commercial
  use open and free.
- Preserved `0.13.0` and all earlier grants under their original MIT terms,
  with an explicit immutable commit boundary and retained license text.
- Added clear licensing, trademark, contribution, and third-party boundaries
  so future optional commercial terms or separately licensed capabilities do
  not weaken rights already granted to the community.
- Updated plugin manifests, skills, public reports, and launch material to use
  one consistent MPL-2.0 identity. No existing community capability was moved
  behind an account or paid tier.

## 0.13.0 - 2026-09-02

- Made `atomlane` the canonical plugin, marketplace, MCP, event, environment,
  package, and storage identity; the public install selector is now
  `atomlane@atomlane`.
- Unified public positioning around one universal safety core with
  platform-native, workload-tailored execution, while retaining the exact
  support boundary: macOS Stable and native Windows Preview.
- Added execution-realm-aware scenario routing. Portable resource scenarios
  now use `host_resource_plan`; Apple-only accelerator goals remain explicit
  advice outside native macOS and never enter executable high-value targets.
- Renamed the optional traffic secret to `ATOMLANE_TRAFFIC_TOKEN`, refreshed
  plugin metadata and launch copy, and updated the repository social card to
  show both platform statuses.
- Preserved immutable releases, tags, Git history, and their source-bound
  evidence rather than rewriting historical provenance.

## 0.12.0 - 2026-09-01

- Added native Windows Preview behind the same immutable Atom IR and scheduler,
  with realm-bound platform contracts that prevent replay across native
  Windows, WSL, Docker, macOS, or another architecture.
- Added Windows-native CPU, memory, power, topology, and capability probes;
  portable locked cumulative statistics; and Docker daemon/VM boundary facts.
- Added staged Job Object supervision: a waiting supervisor is assigned by PID
  before it receives the target launch record. This is not atomic target
  creation. Job-wide CPU and memory limits include the supervisor and normally
  inherited target tree, use a 128 MiB memory minimum, and exclude brokered work.
- Added concurrently drained UTF-8-decoded pipe capture and optional ConPTY with
  an explicit combined-output contract. Explicit ConPTY stdin fails closed because
  a verified terminal-input and EOF contract is not implemented. Live UI updates cover lifecycle
  counts and savings; captured task stdout/stderr is returned at completion.
- Added Windows path alias, case, UNC, extended-prefix, reserved-device, and
  alternate-data-stream safety rules plus conservative whole-file `pwsh` atoms.
- Upgraded Python rewrite previews to use an explicit `spawn` context and a
  platform-aware Windows worker ceiling.
- Added a macOS/Windows and Python 3.10/3.11/3.12/3.13 CI matrix, native
  Windows runtime canaries, public evidence artifacts, bilingual documentation,
  and Windows Preview report coverage. The `windows-2025` runner is not
  presented as proof of Windows 11 Desktop UI integration.
- Made browser-bundle dependency versions explicit, retained exact upstream
  third-party license payloads in release archives, and added drift gates for
  bundle provenance and dynamic-code-evaluation removal.

## 0.11.0 - 2026-09-01

- Added `python_parallel_advisor`, a bounded AST and local-call-graph analyzer
  that never imports, executes, or modifies target Python code.
- Added conservative effect, import-time, loop-control, macOS spawn, GIL/native
  ownership, nested-worker, path-containment, source-size, and resource gates.
- Added deterministic classifications and syntax-checked, source-hash-bound
  rewrite previews for a narrow ordered-map subset; previews are review-only
  and are never applied automatically.
- Separated measured serial observations from modeled parallel projections and
  exposed explicit proof obligations and validation requirements.
- Added the `$optimize-python-parallelism` skill, Python Candidate IR reference,
  scenario routing, bilingual documentation, public safety fixtures, and a
  dedicated visual-report section.
- Added 66 Python-advisor regression cases, bringing the full release suite to
  113 passing tests, including a pre-parse memory budget and real macOS-compatible
  `spawn` output equivalence, non-execution, deterministic hashing, effect
  refusals, path escapes, malformed inputs, and MCP contract coverage.

## 0.10.1 - 2026-08-27

- Renamed the public GitHub repository to `atomlane` and migrated source,
  Pages, installation, badge, support, launch, citation, and generated-report
  URLs to the new canonical location.
- Preserved compatibility aliases for existing installations during the
  migration, without publishing them as the current product identity.

## 0.10.0 - 2026-08-27

- Rebranded the public product as AtomLane with the promise “Parallelize only
  what is proven safe,” while preserving the existing plugin, MCP, repository,
  event, environment-variable, and statistics identifiers for compatibility.
- Redesigned the listing logo, social card, live demo, report metadata, and
  bilingual launch copy around the new identity.
- Updated Codex, Agent Plugins, marketplace, MCP App, skill UI, citation,
  privacy, terms, benchmark, and scenario metadata to use the AtomLane brand.
- Added a maintained brand and compatibility guide for future releases and
  directory submissions.

## 0.9.4 - 2026-08-26

- Added portable Agent Plugins 1.0.0 manifests while retaining the native
  Codex plugin and local stdio MCP configuration.
- Added a privacy-reviewed first-run feedback form and aggregate conversion
  counters to the weekly public metrics and GitHub Pages report.
- Added official directory submission materials, square listing artwork,
  terms, and new standards-based discovery paths.
- Expanded the verified regression suite to 44 tests and kept all release,
  security-scanner, and Pages gates green.

## 0.9.3 - 2026-08-26

- Removed the bundled Zod dynamic-evaluation capability probe and added a
  build-time plus regression-test gate that rejects `eval()` and
  `new Function()` in the published MCP App bundle.
- Added the SHA-pinned HOL marketplace scanner workflow, privacy metadata,
  `.codexignore`, manifest discovery links, and explicit skill licensing.
- Hardened scheduled growth metrics by retaining the last authorized traffic
  sample when GitHub's default Actions token cannot read traffic endpoints.
- Submitted the plugin and its skill to independent free Codex ecosystem
  directories.

## 0.9.2 - 2026-08-26

- Fixed immutable plan verification across MCP clients that serialize integral
  JSON numbers such as `1.0` as `1`; semantic and envelope hashes now use one
  cross-language numeric representation.
- Added an outcome-first bilingual README, exact-text social preview, 20-second
  live-progress demo, reproducible sharing assets, and community templates.
- Added privacy-safe weekly GitHub growth snapshots, a public measurement plan,
  real-project benchmark protocol, roadmap, contribution guide, and launch kit.
- Expanded the public verification dashboard with growth evidence, shareable
  media, and regression coverage for the new publication tooling.

## 0.9.1 - 2026-08-26

- Added a reproducible visual test dashboard with per-test timing, subsystem
  coverage, release gates, environment metadata, and build provenance.
- Added automatic GitHub Pages generation and deployment from the verified
  `main` branch.
- Added a weekly and manually dispatchable five-minute benchmark with four
  independent low-load workloads, live progress samples, observed serial
  equivalent, per-run savings, cumulative savings, speedup, and efficiency.

## 0.9.0 - 2026-08-23

- Added the typed Atom IR and fail-closed frontend compiler for shell, package
  scripts, Make, Compose, and explicit atoms.
- Added immutable plan-envelope and semantic hashes plus execution-time source
  snapshot validation.
- Added event-driven multidimensional resource scheduling without unrelated
  wave barriers.
- Added native concurrency ownership, nested-worker budgeting, split/fusion
  suggestions, and formal timing/provenance fences.
- Added bounded Python CLI AST artifact inference to repair reliable missing
  Make dataflow edges without importing or executing project code.
- Added live running/ready/completed/failed progress and per-run plus cumulative
  savings reporting.
- Added regression coverage for failure propagation, output bounds, timeout
  semantics, filesystem aliases, lifecycle execution gates, and live scheduler
  progress with bounded completion output.
