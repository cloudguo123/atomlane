# OpenAI Showcase submission

Official form: https://openai.com/form/showcase-submission/

Submission status: content is prepared. Maintainer identity and agreement authorization are intentionally held outside the public repository and supplied only to the official OpenAI form.

## About you

- First name: `[maintainer input required]`
- Last name: `[maintainer input required]`
- Email: `[maintainer input required]`
- Website: https://github.com/cloudguo123

## Project

AtomLane

## One-line description

Parallelize only what is proven safe. AtomLane proves independent local work, keeps long runs visibly live, suggests spawn-safe Python refactors, and reports verified time savings on macOS and Windows Preview.

## Form payload

- Project type: Open-source developer tool
- Built with Codex: Yes
- Built with another coding agent: No
- Tech stack: Python 3.10+, Node.js, TypeScript, MCP, Codex Plugin, GitHub Pages
- Use cases: Developer productivity; Python optimization; builds; tests; Docker/Compose; research and paper pipelines; long-running local workflows.
- Capability: Non-executing Python AST/effect analysis; source-hash-bound refactor previews; Codex plugins, skills, and MCP tools for agentic local execution; typed structured plans; live progress reporting.
- OpenAI models and APIs: N/A — the open-source plugin runs inside Codex and does not call a separately billed OpenAI API.
- Other models or APIs: No.

### Building process

Built iteratively with Codex, from live parallel commands to typed Atom and Python Candidate IRs tested on build, Docker, Make, paper, and Python workflows. Codex helped implement fail-closed safety gates, live progress, 5+ minute benchmarks, and CI. v0.12 adds Windows probes, Job Objects, ConPTY, PowerShell files, and Windows/WSL/Docker boundaries. Releases require tests, deterministic bundle/license gates, safety fixtures, and source-bound evidence.

### Project details

- Repository: https://github.com/cloudguo123/atomlane
- Hosted evidence: https://cloudguo123.github.io/atomlane/
- Setup: macOS stable, or the scoped native Windows Preview, with Python 3.10+ exposed as `python3`. Run `codex plugin marketplace add cloudguo123/atomlane`, then `codex plugin add mac-parallel-accelerator@mac-parallel-accelerator`. Open a new Codex task and ask it to inspect a build, test, Docker, research workflow, or long-running Python entrypoint for safe parallelism. Full boundaries and verification steps are in the README and Windows Preview guide.
- Display title: AtomLane — Safe Parallel Workflows
- Tagline: Parallelize only what is proven safe.
- Display author: cloudguo123
- Cover image: https://raw.githubusercontent.com/cloudguo123/atomlane/main/assets/growth/social-preview.png

### Project description

AtomLane helps Codex accelerate local workflows only when independence is provable. macOS stable supports selected shell, build, test, Docker/Compose, Make, paper, and batch work; native Windows Preview supports exact argv and declared PowerShell files, with WSL and Docker treated as separate realms. Its bounded Python advisor never imports or executes target modules. It propagates effects, checks GIL/native ownership and explicit-spawn safety, and emits source-hash-bound review previews only for a narrow ordered-map subset. Other workflows compile to a typed Atom IR covering control flow, artifacts, effects, lifecycles, and CPU/memory capacity; unknown semantics fail closed. Long runs show live progress plus per-run and cumulative savings. The public dashboard reports regression results, safety fixtures, and source-bound five-minute evidence for macOS and native Windows. Local-first and MIT licensed.

## What problem does it solve?

Builds, tests, Docker pipelines, paper workflows, batch tasks, and Python loops often contain independent work, but naïve fan-out can change control flow, race shared state and outputs, break exception or result ordering, oversubscribe native worker pools, or hide failures. This plugin performs bounded static analysis, represents command work in a typed Atom IR and Python candidates in a separate proof-oriented IR, then fails closed when independence cannot be proven.

## Evidence

The public test dashboard retains a controlled five-minute run: four independent workloads completed in 5m10s of wall time versus 20m40s serial equivalent, saving 15m30s at 4.00× observed speedup. The serial equivalent is the sum of observed task durations, not a separately rerun serial workflow, and the project does not claim every workload will achieve this result.

## Links

- Source: https://github.com/cloudguo123/atomlane
- Live report: https://cloudguo123.github.io/atomlane/
- Installation: https://github.com/cloudguo123/atomlane#install-in-two-commands

## Open-source status

MIT licensed. macOS stable plus a scoped native Windows Preview, Python 3.10+, and no third-party Python runtime dependency. Project inspection and optional trace analysis remain local; results are never uploaded automatically.

## Submission boundary

The form's attestation grants OpenAI a nonexclusive, worldwide, irrevocable, royalty-free license to use, test, store, copy, translate, display, modify, distribute, and promote the submitted Showcase Content. Keep the maintainer's identity and acceptance outside version control and transmit them only through the official form.
