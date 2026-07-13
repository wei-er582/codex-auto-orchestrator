# Codex Auto Orchestrator project rules

- Keep the runtime dependency-free and compatible with Python 3.11 or newer.
- Build subprocess commands as argument arrays; never interpolate task text into a shell command.
- Preserve unknown or dirty workspace state. Never reset, discard, or force-remove a worktree containing uncommitted changes.
- Keep native Ultra exclusive within a wave and prevent recursive plugin invocation in child Codex processes.
- Add or update tests for routing, state transitions, process cleanup, Git worktree behavior, and schema changes.
- Keep implementation comments focused on concurrency, lifecycle, safety, and non-obvious failure semantics.
