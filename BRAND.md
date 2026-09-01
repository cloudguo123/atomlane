# AtomLane brand guide

## Core identity

- **Name:** AtomLane
- **Category:** safe parallel execution for coding agents
- **Primary line:** Parallelize only what is proven safe.
- **Chinese line:** 只并行已证明安全的任务。
- **Positioning line:** Universal safety core. Platform-native execution.
  Workload-tailored acceleration.
- **Chinese positioning:** 一套通用安全内核，按平台原生执行，按工作负载定制加速。
- **One sentence:** AtomLane compiles local work into conflict-checked atomic
  plans, then tailors safe execution to the active platform, workload, and
  resource envelope.

Use `AtomLane` with a capital `A` and `L`. The canonical plugin ID, marketplace
name, MCP server key, event namespace, storage directory, and repository slug
are all `atomlane` or `AtomLane`, according to the field's naming convention.
Previous identifiers belong only to immutable releases, tags, and Git history;
do not repeat them in current product copy or installation instructions.

## Message hierarchy

1. Lead with the safety promise: **Parallelize only what is proven safe.**
2. Establish the design: **one universal safety core, platform-native
   execution, and workload-tailored acceleration.**
3. Explain the mechanism: typed Atom IR, exact hashed plans, conflict and
   resource checks, fail-closed execution, and realm-specific containment.
4. Prove the result: live progress, per-run and cumulative savings, verified
   tests, and reproducible benchmarks.
5. Name the supported situations: builds, tests, Docker/Compose, research,
   batch data/media, Python refactors, and native toolchains.

Avoid broad claims such as “make every task 4× faster.” Use “4.00× observed in
the controlled benchmark” and preserve the serial-equivalent methodology note.
Do not say “Windows fully supported” or imply platform parity. Say **macOS
Stable** and **native Windows Preview** until the Preview limitations and
desktop integration gates are closed.

## Universal and tailored: exact meanings

| Term | What it means | What it does not mean |
| --- | --- | --- |
| Universal safety core | Shared Atom IR, hashes, effect/conflict/resource checks, authorization boundaries, live telemetry, and savings accounting | Every command, language, platform, or task is supported |
| Platform-native execution | Plans are bound to their real execution realm and use that platform's path, process, terminal, and resource controls | A plan or resource budget can be replayed across macOS, Windows, WSL, or Docker |
| Workload-tailored acceleration | Frontends, native delegates, scenario routing, and worker budgets change for builds, tests, containers, research, media/data/ML, and Python | Scenario similarity is proof of independence or permission to execute |

Platform status must always be stated together:

| Platform | Public status | Current promise |
| --- | --- | --- |
| macOS native | Stable | Supported shell, package, Make, Compose, test/build, research, batch, Python-advisor, and Apple-silicon paths within documented gates |
| Native Windows | Preview | Exact argv, declared whole-file PowerShell, Job Object containment, UTF-8 pipes, optional output-only ConPTY, static Python advice, and realm-safe resource planning |
| WSL / Docker | Separate realms | Explicitly detected and budgeted; never silently presented as native Windows or macOS containment |

## Visual system

- Background: `#07100E`
- Surface: `#0B1714`
- Primary mint: `#65E6B4`
- Parallel blue: `#80B7FF`
- Evidence purple: `#D7A6FF`
- Primary text: `#ECF8F3`
- Muted text: `#8EA79E`

The lane mark represents atomic tasks entering separate verified paths. Use
the square logo for plugin listings and avatars, the 1280×640 social preview
for repository cards, and the live-execution GIF for demonstrations. Source
SVGs are canonical; PNG and GIF derivatives are generated artifacts.

## Canonical naming

```text
Brand:          AtomLane
Skill:          $accelerate-local-work
Plugin ID:      atomlane
Marketplace:    atomlane
Repository:     cloudguo123/atomlane
MCP server key: atomlane
Event namespace: atomlane:*
Environment:    ATOMLANE_*
```
