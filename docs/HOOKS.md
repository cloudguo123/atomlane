# AtomLane Hooks and live indicator

AtomLane 0.15.0 adds a plugin-bundled `UserPromptSubmit` hook. This closes the
gap between installing the plugin and remembering to invoke it: every submitted
task gets a fast, visible acceleration preflight before the model starts work.

## What runs

The hook receives the standard Codex `UserPromptSubmit` JSON event and examines
only its `prompt` string. It returns a fixed advisory result:

- **Direct path**: no concrete local execution batch is visible.
- **Inspect at the execution boundary**: local work may exist, but the prompt
  does not establish a worthwhile parallel batch.
- **Likely parallel candidate**: parallel, repeated, or long batch work is
  mentioned; the task must still be inspected and compiled before execution.

The classifier does not read the repository, transcript, environment, or user
files. It has no network access of its own, performs no target execution, makes
no mutation, never blocks the submitted prompt, and never declares concurrency
safe. It also never copies prompt text into its output. Its output explicitly
keeps the user's submitted request primary. The command uses Python's isolated,
no-site mode (`-I -S`) to ignore environment-based startup customization.

When an execution boundary is reached, `$accelerate-local-work` inspects the real
work. `atomic_task_plan` must prove control, dependency, effect, lifecycle,
realm, and resource compatibility before `atomic_exec` can run a parallel plan.
Short work, a single unit, shared-state mutation, or uncertain effects remain
direct or serial.

## First-run trust

Codex does not automatically trust plugin-bundled hooks. After installing or
upgrading AtomLane:

1. Start a new Codex task.
2. Open **Hooks**.
3. Review the AtomLane `UserPromptSubmit` command and source path.
4. Trust and enable it if the definition matches the installed release.

Changing the hook definition changes its hash, so Codex can require review again
after a future upgrade. This is intentional. The hook can remain disabled while
AtomLane's explicit skills and MCP tools continue to work.

## Live execution

The hook result is a preflight, not the execution indicator. When AtomLane
actually admits a run expected to exceed ten seconds, the MCP App indicator and
pollable runner show elapsed time, running/completed/failed counts, active atoms,
peak concurrency, estimated current savings, and verified per-run plus cumulative
savings. Savings are reported only from observed execution timings.

## `Unexpected response type` after an upgrade

Older tasks may retain a versioned MCP App resource URI such as
`ui://widget/atomlane-indicator-0.13.0.html`. Before 0.15.0, a newer server
rejected that URI and returned a tool-shaped error where the MCP protocol
expected a resource response, causing repeated `Unexpected response type`
messages.

AtomLane 0.15.0 accepts strict historical AtomLane indicator URIs and returns the
requested URI while advertising only the current resource. Missing or wrongly
typed URI parameters now receive JSON-RPC `Invalid params`; unrelated URI
strings receive `Resource not found`. Neither path is encoded as a tool result.
Start a new task after upgrading so Codex also reloads the new Hook and MCP
metadata.
