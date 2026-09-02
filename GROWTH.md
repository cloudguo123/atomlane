# Open growth plan

This project uses free, privacy-respecting distribution. It does not use paid ads, automatic user-result uploads, tracking cookies, or purchased engagement.

## Thirty-day targets

| Signal | Target | Source |
| --- | ---: | --- |
| Unique repository visitors | 500 | GitHub 14-day traffic snapshots |
| Clone or release-download intent | 100 | GitHub clones plus release asset downloads |
| Stars | 50 | GitHub repository |
| First-run reports | 20 | Public first-run-labeled issues |
| External benchmark reports | 10 | Benchmark-labeled issues and discussions |
| Compatibility reports | 5 | Bug/scenario reports with environment evidence |
| Contributors | 3 | Merged external pull requests |
| Awesome-list entries | 2 | Accepted directory pull requests |

Clones, downloads, and first-run reports indicate intent, not verified installation. First-run reports are public and self-selected; Marketplace installs are not currently exposed as a public repository metric.

## Distribution sequence

1. Make the README answer value, trust, proof, and installation above the fold.
2. Publish an exact-text social preview and a short live-progress demo.
3. Submit to Codex plugin/skill directories and the OpenAI community showcase.
4. Share one technical launch post with reproducible evidence, then adapt it for English and Chinese communities.
5. Ask users for real-project benchmarks, including neutral, negative, and blocked results.
6. Convert repeated blockers into scenario rules, regression tests, and contributor-sized issues.

## Measurement

`scripts/collect_github_metrics.py` stores only aggregate GitHub counters and counts of public `first-run` and `benchmark` issues in `docs/metrics.json`; it does not retain issue bodies. A weekly GitHub Actions workflow refreshes public counters. Repository traffic covers GitHub's latest 14-day window and can lag. GitHub's default Actions token cannot read traffic; without the optional fine-grained `ATOMLANE_TRAFFIC_TOKEN` (repository Administration: read), the workflow retains the last authenticated traffic sample and marks it stale instead of inventing zeroes.

The most important qualitative signals are:

- Did a new user install without help?
- Did the planner find useful concurrency in a real project?
- Did it correctly refuse unsafe work?
- Was live progress visible throughout?
- Could the user explain and trust the savings number?

## Launch assets

Reusable drafts live under `docs/launch/`. Adapt each post to the community rather than cross-posting identical copy everywhere.

The official OpenAI Plugins Directory packet is maintained in
`docs/launch/OPENAI_PLUGIN_DIRECTORY.md`. Portal submission remains gated on a
verified publisher identity and confirmation that the local stdio runtime is
eligible as a skills-only local workflow; the project must not be presented as
a public remote MCP server.

The installable distribution is Codex-native so its task-assessment hook is
actually discovered. The root `mcp.json` remains an optional portable local
stdio configuration, but this release does not advertise simultaneous Agent
Plugins package conformance: current Codex releases suppress bundled lifecycle
hooks when a root Agent Plugins manifest is present.
