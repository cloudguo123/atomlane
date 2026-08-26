# OpenAI Developer Forum draft

- Target category: https://community.openai.com/c/codex/37
- Suggested tags: `codex-app`, `community`, `best-practices`
- Publishing status: draft ready; the current browser session must be logged in before a topic can be created.

## Title

I built a semantics-preserving parallel execution plugin for Codex on macOS

## Post

I kept running into a bad tradeoff in AI-generated projects: many build, test, Docker, and research steps look parallel, but splitting shell commands can silently change control flow or race shared state.

Mac Parallel Accelerator treats planning like compilation. It parses supported shell/package/Make/Compose/test entrypoints into a typed Atom IR, keeps success/failure/data/lifecycle edges distinct, models artifacts and resource capacity, and executes only the exact hashed plan. Unknown effects fail closed. For runs over ten seconds, a PTY view shows running/ready/completed/failed counts and current estimated savings instead of a blank spinner.

The public controlled run keeps every workload active for more than five minutes: 20m41s serial equivalent, 5m10s parallel wall time, 15m31s saved, 4.00× observed. The serial equivalent is the sum of observed independent task durations—not a separate 20-minute serial rerun—and it is not a universal speedup claim.

I am especially looking for sanitized real-project results in three areas: Web quality gates, Docker/Compose health DAGs, and research/paper pipelines. Neutral, slower, and correctly blocked results are welcome because they expose missing semantic rules.

Source and installation: https://github.com/cloudguo123/mac-parallel-accelerator

Live evidence: https://cloudguo123.github.io/mac-parallel-accelerator/

What workflow would you trust an agent to parallelize only if it could prove the dependency and shared-state model first?
