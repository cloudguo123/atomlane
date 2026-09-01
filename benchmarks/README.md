# Community project benchmarks

This directory defines the machine-readable format for real-project results. It deliberately starts with an empty result set; the controlled scheduler benchmark in `docs/benchmark-results.json` is not relabeled as external project evidence.

Accepted result entries must satisfy [BENCHMARKING.md](../BENCHMARKING.md) and
validate against `project-result.schema.json` version 1.2. Version 1.2 requires
a full source commit, at least three comparable repetitions, an exact
platform/execution-realm/process-tree/terminal tuple, and reproducible
correctness evidence. The `schema_version` on `external-results.json` describes
the collection wrapper and is independent from each result entry's version.

Native Windows entries additionally require structured `windows_evidence`.
That object records the supervisor-first PID assignment sequence, explicitly
states that target creation is not atomic, separates requested from queried Job
limits, and defines `job_active_process_limit` as the whole-Job member ceiling,
including the supervisor. ConPTY evidence must leave that limit null. Evidence
limits containment to the client plus normally inherited Windows descendants.
Broker-created WSL, Docker, WMI,
service, scheduled-task, or remote work must be recorded as outside the Job.
Pipe mode keeps stdout/stderr separate; ConPTY combines them. In both modes,
captured task output belongs to the completion result rather than the live
lifecycle/count/savings channel. A Windows Server CI result is not Windows 11
Desktop UI evidence.

The schema deliberately rejects cross-realm combinations such as macOS with
`windows_native`, native Windows with a POSIX process backend or PTY, and
Windows evidence attached to a non-Windows result. Private project content is
never required.

Submit a sanitized result through the [benchmark issue form](https://github.com/cloudguo123/atomlane/issues/new?template=benchmark.yml). After review, maintainers add it to `external-results.json` with a link to the public evidence.
