# Open growth plan

This project uses free, privacy-respecting distribution. It does not use paid ads, automatic user-result uploads, tracking cookies, or purchased engagement.

## Thirty-day targets

| Signal | Target | Source |
| --- | ---: | --- |
| Unique repository visitors | 500 | GitHub 14-day traffic snapshots |
| Clone or release-download intent | 100 | GitHub clones plus release asset downloads |
| Stars | 50 | GitHub repository |
| External benchmark reports | 10 | Benchmark-labeled issues and discussions |
| Compatibility reports | 5 | Bug/scenario reports with environment evidence |
| Contributors | 3 | Merged external pull requests |
| Awesome-list entries | 2 | Accepted directory pull requests |

Clones and downloads indicate intent, not verified installation. Marketplace installs are not currently exposed as a public repository metric.

## Distribution sequence

1. Make the README answer value, trust, proof, and installation above the fold.
2. Publish an exact-text social preview and a short live-progress demo.
3. Submit to Codex plugin/skill directories and the OpenAI community showcase.
4. Share one technical launch post with reproducible evidence, then adapt it for English and Chinese communities.
5. Ask users for real-project benchmarks, including neutral, negative, and blocked results.
6. Convert repeated blockers into scenario rules, regression tests, and contributor-sized issues.

## Measurement

`scripts/collect_github_metrics.py` stores only aggregate GitHub counters in `docs/metrics.json`. A weekly GitHub Actions workflow refreshes the snapshot. Repository traffic covers GitHub's latest 14-day window and can lag.

The most important qualitative signals are:

- Did a new user install without help?
- Did the planner find useful concurrency in a real project?
- Did it correctly refuse unsafe work?
- Was live progress visible throughout?
- Could the user explain and trust the savings number?

## Launch assets

Reusable drafts live under `docs/launch/`. Adapt each post to the community rather than cross-posting identical copy everywhere.
