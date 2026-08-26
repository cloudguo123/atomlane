# Security Policy

## Supported versions

Security fixes are applied to the latest released version. Version 0.9.x is
the supported release line at initial publication.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting or a private Security
Advisory for this repository. Do not include live credentials, private project
files, prompts, command output, or other users' data in a public issue.

Include the affected version, the relevant entrypoint or Atom contract, a
minimal synthetic reproduction, expected behavior, and observed behavior. Do
not test credentials, access unrelated files, or exercise remote side effects.

## Security model

The plugin intentionally reads user-authorized local project metadata and can
execute user-authorized local commands. These capabilities are bounded by:

- strict argv, input, task-count, output, and timeout limits;
- typed dependency, artifact, effect, capacity, and lifecycle contracts;
- fail-closed unknown-effect handling;
- immutable plan-envelope and semantic hashes;
- source-snapshot validation before execution;
- process-group cancellation and no automatic retry of uncertain side effects;
- explicit opt-in for bounded local trace routing signals.

The bundled indicator includes MCP Ext Apps and Zod. Zod may use dynamic code
generation for schema parsing when the browser permits it. This is third-party
library behavior; the indicator does not load remote scripts or contact remote
destinations.

A successful scan or test suite reduces known risk but is not proof of complete
security.
