# OpenAI Plugins Directory submission packet

Official guidance: https://developers.openai.com/plugins/deploy/submission

## Current submission boundary

OpenAI accepts skills-only, MCP-only, and combined plugins. Public submission
requires a verified developer or business identity and `Apps Management: Write`
for the publishing organization. The current MCP server is intentionally local
stdio and therefore is not a public HTTPS MCP endpoint. Do not describe it as a
remote MCP submission.

The safe next portal step is to confirm with the publisher whether OpenAI will
review this project as a skills-only local Codex workflow. Do not submit a
skills-only snapshot until its bundled local scripts and fallback execution
path have been tested independently of the plugin's MCP registration.

The root `plugin.json` and `mcp.json` separately implement the vendor-neutral
Agent Plugins 1.0.0 layout, which permits local stdio MCP. That portable
compatibility does not override the OpenAI Platform portal's public-MCP review
requirements and must not be used to imply remote hosting.

## Listing information

- Name: AtomLane
- Category: Productivity
- Short description: Parallelize only what is proven safe—with live progress and measured savings.
- Website: https://cloudguo123.github.io/atomlane/
- Source: https://github.com/cloudguo123/atomlane
- Support: https://github.com/cloudguo123/atomlane/discussions
- Privacy: https://github.com/cloudguo123/atomlane/blob/main/PRIVACY.md
- Terms: https://github.com/cloudguo123/atomlane/blob/main/TERMS.md
- Logo: https://raw.githubusercontent.com/cloudguo123/atomlane/main/assets/growth/listing-logo.png
- Screenshot: https://raw.githubusercontent.com/cloudguo123/atomlane/main/assets/growth/social-preview.png
- Developer identity: `[verified publisher selection required]`
- Availability: `[publisher decision required]`

## Long description

AtomLane helps Codex find safe program-level Python refactors and execute safe
concurrency in local macOS builds, tests, Docker/Compose graphs, Make targets,
paper workflows, and batch jobs. Its Python advisor performs bounded AST and
effect analysis without importing, executing, or modifying target code, and
binds any review preview to the exact source hash. It compiles supported entrypoints into a typed Atom IR that keeps
control flow, artifacts, effects, lifecycle events, source snapshots, and
resource capacity explicit. Unknown effects and unordered shared writes fail
closed instead of being guessed safe.

The executor consumes the exact immutable plan and matching hash. It releases
ready successors without wave barriers, preserves failure propagation, adapts
to the current Mac, and delegates concurrency to native owners when safer. Long
runs show live running, ready, completed, and failed counts together with
per-run and cumulative estimated time saved. The project is MIT licensed,
local-first, and backed by public tests and reproducible benchmark evidence.

## Starter prompts

1. Use AtomLane to inspect this project's build and test entrypoints, compile a safe atomic plan, and execute only verified concurrency with live progress.
2. Analyze this Docker Compose application, preserve health and completion dependencies, and recommend CPU and memory budgets for each service before running it.
3. Inspect this Make target for hidden file dependencies and parallelize it only if the dataflow and shared outputs are safe.
4. Profile this research or paper workflow, identify timing-sensitive fences, and separate correctness replays from benchmark-eligible runs.
5. Explain why this workload was kept serial or delegated to its native runner, including every blocker and resource conflict.
6. Use the Python advisor on this long-running entrypoint, distinguish CPU,
   blocking I/O, native-library, and unsafe-effect candidates, and show only
   source-hash-bound review previews with explicit validation requirements.

## Positive review cases

### 1. Preserve a shell quality gate

- Prompt: Run `lint && typecheck && build && test` as fast as safely possible.
- Fixture: A repository where `typecheck` reads generated build types and all commands are deterministic.
- Expected behavior: Preserve every success edge; infer or report the typecheck/build artifact conflict; execute only a legal ordering or keep the compound entrypoint serial.
- Expected result: Plan hash, typed edges, execution eligibility, atom statuses, elapsed time, and clearly labeled measured or estimated savings.

### 2. Respect a Compose lifecycle graph

- Prompt: Start the app stack and run migrations and smoke tests with appropriate CPU and memory budgets.
- Fixture: Compose services with `service_healthy`, `service_completed_successfully`, profiles, ports, and named volumes.
- Expected behavior: Preserve readiness, health, and completion semantics; budget within the Docker Desktop VM; avoid unrelated `compose up` fan-out.
- Expected result: Native-delegate decision or exact blocker, service/resource explanation, and live progress for any long run.

### 3. Recover a hidden Make data chain

- Prompt: Speed up the `finite-pilot-cost` target.
- Fixture: Sibling prerequisites whose scripts actually produce calibrate → validate → report artifacts.
- Expected behavior: Infer the data edges from declared accesses or fail closed; never trust sibling prerequisite shape alone.
- Expected result: Corrected typed graph, diagnostics, and no overlapping producer/consumer execution.

### 4. Delegate native test parallelism

- Prompt: Run the Vitest suite faster in CI.
- Fixture: Vitest already uses internal workers and every invocation writes the same JUnit path.
- Expected behavior: Keep one native runner or isolate and deterministically merge outputs; do not create per-file processes sharing the report.
- Expected result: Native delegate or isolation plan, aggregate exit status, failed tests, and output paths.

### 5. Show a genuinely live long run

- Prompt: Run four independent 15-second fixtures and show progress throughout.
- Fixture: Four isolated commands with declared read-only inputs and distinct outputs.
- Expected behavior: Compile once, execute the exact plan through the PTY live runner, and update elapsed/running/ready/completed/failed values during execution.
- Expected result: Final atom results, observed peak concurrency, per-run time saved, and cumulative time saved.

### 6. Review a Python ordered CPU map

- Prompt: Inspect a long-running pure Python list-comprehension map and suggest a safe process-pool refactor without running it.
- Fixture: A module-level pure worker called through a guarded `main`, plus measured serial hotspot metadata.
- Expected behavior: Parse but never import or execute the target; propagate local effects; require the macOS spawn guard; distinguish the measured serial observation from the modeled parallel projection.
- Expected result: `reviewable_rewrite`, complete proof obligations, resource ceiling, source hash, and syntax-checked unified diff that is not applied automatically.

## Negative review cases

### 1. Unknown effect

- Prompt: Parallelize a dynamically generated shell command whose output paths and external effects are unknown.
- Expected behavior: Refuse to infer independence; preserve a proven opaque serial boundary only when legal, otherwise stop for evidence.
- Why: Unknown effects are not equivalent to no effects.

### 2. Unordered shared write

- Prompt: Run two code generators concurrently even though both overwrite `.next/types`.
- Expected behavior: Reject the unordered alias or require explicit isolation/versioning and a deterministic merge.
- Why: Silent mutex insertion or optimistic fan-out would hide an invalid source plan.

### 3. Stale or modified plan

- Prompt: Execute a compiled plan after editing its argv or changing a source snapshot while reusing the old hash.
- Expected behavior: Reject before launching any atom and require recompilation.
- Why: The execution contract must match both the immutable plan and current source evidence.

## Current release notes

Version 0.11 adds a non-executing Python Candidate IR, fail-closed effect and
spawn gates, GIL-aware executor routing, source-hash-bound review previews, and
public safety-fixture evidence to the existing typed atomic planner, exact
plan-hash execution, Apple-silicon resource awareness, native delegation, live
progress, and honest savings reporting. No test credentials or network account
are required for local use.
