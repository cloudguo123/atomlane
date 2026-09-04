# Roadmap

This roadmap favors evidence and semantics over raw process count.

## Now

- Harden the native Windows Preview with real-project results across `pwsh`,
  exact argv, optional ConPTY, in-Job descendant cancellation, staged (not
  atomic) target startup, broker boundaries, and Job budgets that include the
  supervisor with a 128 MiB memory minimum.
- Collect explicit Windows Desktop integration evidence; do not infer Windows
  11 Desktop UI support from the headless `windows-2025` CI runner.
- Collect separate native Windows, WSL, and Docker VM resource evidence; never
  merge their execution envelopes or performance claims.
- Collect reproducible Web, Docker, research, native-build, and batch-workload results.
- Collect reproducible pytest-xdist evidence for suites with 100+ independent
  cases across macOS and native Windows, including fixture locality, each
  supported distribution mode, shared-resource refusals, memory behavior,
  effective-config/addopts preservation, output collision refusals, fresh
  counter-consistent JUnit summaries, and session-attested serial baselines
  bound to explicit source snapshots and testcase identity.
- Collect opt-in Python refactor certificates with semantic equivalence, memory,
  p50/p90, and negative-result evidence across CPU, I/O, and native workloads.
- Expose the v2 measured, estimated, and legacy-unclassified savings buckets in
  shareable, sanitized result summaries without collapsing their provenance.
- Expand static frontends for real package, Make, Compose, and non-pytest
  test-runner edge cases.
- Make command and Python-refactor refusals easier to understand and resolve
  without guessing effects.

## Next

- Calibrated duration and memory forecasts with confidence ranges.
- Incremental plan reuse guarded by source snapshots and semantic hashes.
- Better nested-worker budgeting across compilers, tests, BLAS, BuildKit, GPU, and outer tasks.
- Add optional native-worker observation only where runtime evidence can
  distinguish workers actually started from the hash-bound configured ceiling;
  never infer it from outer concurrency or JUnit case counts.
- Replace per-output persistent pytest lease files with a fixed-size hash-stripe
  or safe generation/cleanup protocol so high-volume CI does not accumulate one
  inode per unique JUnit/base-temp path without introducing unlink/replacement
  races.
- Scenario-specific split/fuse recommendations with explicit proof obligations.
- More lifecycle-aware native delegates and deterministic result merges.
- Evaluate pytest external sharding only for suites whose reports, caches,
  temporary paths, databases, services, fixture scope, and deterministic merge
  can all be represented; keep pytest-xdist as the default semantic owner.
- Carefully broaden the Python Candidate IR to reductions, chunked maps, and
  multi-argument starmaps only when order, associativity, exceptions, effects,
  serialization, and memory can be represented and tested fail-closed.
- Generate an opt-in differential-test harness from an approved preview while
  keeping code application and execution as separate user-authorized steps.
- Add semantically owned Windows frontends only where control flow, quoting,
  artifacts, and cancellation can be represented without shell-text guessing.

## Later

- Optional Rust implementation for process supervision and scheduling hot paths, only if profiling proves Python is the bottleneck and cross-language equivalence tests are in place.
- Program-level advisors for JavaScript/TypeScript workers, Rust iterators/Rayon,
  Go goroutines, and native kernels, each backed by language-specific semantics
  rather than text-pattern substitution.
- Linux-native release support after the WSL/Linux realm contract earns its own
  containment, resource, terminal, and CI evidence.
- Privacy-preserving local trend summaries and opt-in community benchmark aggregation.

Votes and design evidence are welcome in [GitHub Discussions](https://github.com/cloudguo123/atomlane/discussions).
