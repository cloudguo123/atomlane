# Privacy

AtomLane's execution engine runs locally. The plugin does not open a network
listener, add telemetry, sell personal data, or send project data to an
AtomLane-operated service.

AtomLane is an MCP tool used by a host such as Codex. To do the work you request,
the plugin receives task parameters from that host and returns results over local
standard input/output. Those results can include command arguments, working
directories, file paths, progress, and bounded command output. The host may
process or transmit that conversation and tool data according to its own privacy
terms and your configuration. Do not place secrets in task arguments or output;
review results before sharing or filing an issue.

Local benchmark ledgers and reports remain on your machine unless you explicitly
publish, upload, or share them. Public benchmark submissions and GitHub artifacts
are intentionally public and may retain the environment labels and measurements
you submit. AtomLane sanitizes built-in CI runner labels, but it cannot prove that
arbitrary command output contains no personal or confidential information.

The optional repository-maintainer metrics workflow records only aggregate,
public GitHub repository signals. It does not run as part of the installed
plugin and does not inspect user projects or Codex conversations.

Report a privacy or security concern through the private process in
[SECURITY.md](SECURITY.md).
