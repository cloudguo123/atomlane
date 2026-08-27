# Roadmap

This roadmap favors evidence and semantics over raw process count.

## Now

- Collect reproducible Web, Docker, research, native-build, and batch-workload results.
- Improve the shareable, sanitized result summary and cumulative savings ledger.
- Expand static frontends for real package, Make, Compose, and test-runner edge cases.
- Make planner refusals easier to understand and resolve without guessing effects.

## Next

- Calibrated duration and memory forecasts with confidence ranges.
- Incremental plan reuse guarded by source snapshots and semantic hashes.
- Better nested-worker budgeting across compilers, tests, BLAS, BuildKit, GPU, and outer tasks.
- Scenario-specific split/fuse recommendations with explicit proof obligations.
- More lifecycle-aware native delegates and deterministic result merges.

## Later

- Optional Rust implementation for process supervision and scheduling hot paths, only if profiling proves Python is the bottleneck and cross-language equivalence tests are in place.
- A portable execution core beyond macOS while retaining platform-specific resource adapters.
- Privacy-preserving local trend summaries and opt-in community benchmark aggregation.

Votes and design evidence are welcome in [GitHub Discussions](https://github.com/cloudguo123/atomlane/discussions).
