# Hacker News draft

## Title

Show HN: AtomLane – Parallelize only what is proven safe

## Text

AtomLane is an MIT-licensed Codex plugin for safely shortening local build, test, Docker, research, and batch workflows.

The main idea is to treat concurrency planning as compilation instead of splitting command strings. Supported entrypoints are lowered to a typed Atom IR containing control edges, artifact access, non-file effects, lifecycle events, capacity claims, and source snapshots. The executor accepts only the exact compiled plan and hash; unknown effects and unsupported lifecycle contracts fail closed.

It delegates parallelism to Make, test runners, compiler drivers, Compose, or BuildKit when those tools own the semantics, and budgets native inner workers together with outer concurrency. Long executions stream live counters and estimated savings.

The retained controlled test runs four independent workloads for at least five minutes each. Observed result: 20m41s serial equivalent, 5m10s parallel wall, 15m31s saved, 4.00×. This is evidence for the controlled workload and reporting path, not a universal speedup claim.

Source: https://github.com/cloudguo123/mac-parallel-accelerator

Report: https://cloudguo123.github.io/mac-parallel-accelerator/

I would value criticism of the Atom IR, failure propagation, artifact aliasing, nested worker budgeting, and the boundary between static planning and native delegates.
