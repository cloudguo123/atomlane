# OpenAI Showcase submission

Official form: https://openai.com/form/showcase-submission/

Submission status: all non-personal fields were prepared on 2026-08-26. Final submission requires the maintainer's first name, last name, email, and explicit acceptance of the OpenAI Showcase Gallery Program Agreement.

## About you

- First name: `[maintainer input required]`
- Last name: `[maintainer input required]`
- Email: `[maintainer input required]`
- Website: https://github.com/cloudguo123

## Project

Mac Parallel Accelerator

## One-line description

A Codex plugin that compiles local macOS work into semantics-safe atomic plans, executes only proven concurrency, keeps long runs visibly live, and reports per-run and cumulative time saved.

## Form payload

- Project type: Open-source developer tool
- Built with Codex: Yes
- Built with another coding agent: No
- Tech stack: Python 3.10+, Node.js, TypeScript, MCP, Codex Plugin, GitHub Pages
- Use cases: Developer productivity; builds; tests; Docker/Compose; research and paper pipelines; long-running local workflows.
- Capability: Codex plugins, skills, and MCP tools for agentic local execution; typed structured plans; live progress reporting.
- OpenAI models and APIs: N/A — the open-source plugin runs inside Codex and does not call a separately billed OpenAI API.
- Other models or APIs: No.

### Building process

Built iteratively with Codex: started from live parallel command execution, then tested on web, Docker/Compose, Make, and research-paper workflows. Codex helped analyze traces, define a typed Atom IR, implement fail-closed compilation and scheduling, add PTY progress, reproduce a 5+ minute benchmark, and harden packaging/CI. Every release is validated by unit/integration tests, deterministic bundle checks, and an external plugin scanner.

### Project details

- Repository: https://github.com/cloudguo123/mac-parallel-accelerator
- Hosted evidence: https://cloudguo123.github.io/mac-parallel-accelerator/
- Setup: macOS with Python 3.10+. In Codex: `/plugin marketplace add cloudguo123/mac-parallel-accelerator`, then `/plugin install mac-parallel-accelerator@personal`. Restart Codex and ask it to inspect a build, test, Docker, or research workflow for safe parallelism. Full verification steps are in the README.
- Display title: Safe Parallel Workflows on Mac
- Tagline: Compile local workflows into semantics-safe parallel plans, keep long runs visibly live, and measure the time saved.
- Display author: cloudguo123
- Cover image: https://raw.githubusercontent.com/cloudguo123/mac-parallel-accelerator/v0.9.3/assets/growth/social-preview.png

### Project description

Mac Parallel Accelerator helps Codex accelerate local macOS builds, tests, Docker/Compose graphs, paper workflows, and batch jobs without guessing that commands are independent. It compiles supported entrypoints into a typed Atom IR covering control flow, artifacts, effects, lifecycles, and CPU/memory capacity; unknown semantics fail closed. A hashed immutable plan is executed with adaptive scheduling, while long runs show live running/ready/completed/failed counts plus per-run and cumulative estimated time saved. Public evidence includes 37/37 tests and a controlled 5m10s run versus 20m41s serial equivalent, saving 15m31s at 4.00× observed speedup. MIT licensed and local-first.

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

## Submission boundary

The form's attestation grants OpenAI a nonexclusive, worldwide, irrevocable, royalty-free license to use, test, store, copy, translate, display, modify, distribute, and promote the submitted Showcase Content. The maintainer must review and personally accept that agreement before submission.
