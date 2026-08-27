# AtomLane brand guide

## Core identity

- **Name:** AtomLane
- **Category:** safe parallel execution for coding agents
- **Primary line:** Parallelize only what is proven safe.
- **Chinese line:** 只并行已证明安全的任务。
- **One sentence:** AtomLane compiles local work into conflict-checked atomic
  plans, then executes only the concurrency that preserves control flow,
  effects, resources, and authorization boundaries.

Use `AtomLane` with a capital `A` and `L`. Do not abbreviate the public brand
to MPA. The repository slug, plugin ID, MCP server key, environment variables,
event names, and legacy statistics directory may continue to use
`mac-parallel-accelerator` during the compatibility window.

## Message hierarchy

1. Lead with the safety promise: **Parallelize only what is proven safe.**
2. Explain the mechanism: typed Atom IR, exact hashed plans, conflict and
   resource checks, and fail-closed execution.
3. Prove the result: live progress, per-run and cumulative savings, verified
   tests, and reproducible benchmarks.
4. Name the supported situations: builds, tests, Docker/Compose, research,
   batch data/media, and native toolchains.

Avoid broad claims such as “make every task 4× faster.” Use “4.00× observed in
the controlled benchmark” and preserve the serial-equivalent methodology note.

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

## Compatibility naming

The v0.10 brand release changes user-visible naming without breaking existing
installation routes:

```text
Brand:          AtomLane
Skill:          $accelerate-local-work
Plugin ID:      mac-parallel-accelerator
Repository:     cloudguo123/mac-parallel-accelerator
MCP server key: mac-parallel-accelerator
```

Technical identifiers should change only in a separately announced migration
with tested aliases or redirects. Public copy may say “AtomLane, formerly Mac
Parallel Accelerator” when continuity matters.
