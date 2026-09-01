# Contributing

Thanks for helping make safe local acceleration more useful.

## Good first contributions

- Share a sanitized real-project benchmark, including negative or blocked results.
- Add a scenario with explicit optimization targets, hazards, evidence, and stop conditions.
- Add a regression test for a control-flow, artifact, lifecycle, or resource edge case.
- Improve macOS, Apple-silicon, or native Windows resource observations without
  weakening fail-closed behavior or crossing Windows/WSL/Docker realms.
- Improve documentation or translations.

Use GitHub Discussions for design questions and an issue for reproducible defects. Security reports belong in the private process described in [SECURITY.md](SECURITY.md).

## Development checks

```bash
python3 -m compileall -q scripts
python3 -m unittest discover -s scripts -p 'test*.py' -v
python3 scripts/self_test.py
uvx ruff check scripts
npm ci
npm run build:indicator
git diff --exit-code -- assets/parallel-indicator-host.bundle.js
```

If a change touches the skill, validate it with Codex's skill validator. If it changes the public report, regenerate `docs/index.html` and `docs/test-results.json`.

## Design invariants

- Exact control flow is the default.
- Unknown effects fail closed.
- The executor consumes the exact compiled plan and hash.
- Artifact conflicts and capacity resources are separate concepts.
- Native semantic owners keep their concurrency when that is safer.
- Long execution keeps lifecycle counts and savings visible; captured task
  stdout/stderr is returned with the completed result.
- Native Windows uses staged supervisor-to-Job assignment rather than claiming
  atomic target creation. Job resource budgets include the supervisor and the
  normally inherited target tree, require at least 128 MiB of memory, and do
  not cover WSL, Docker, WMI, services, scheduled tasks, or other brokered work.
- Parallelism does not expand authorization.

Pull requests should add or update tests for behavioral changes and explain any compatibility impact. Small focused changes are easiest to review.
