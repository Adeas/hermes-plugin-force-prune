# Changelog

## 1.0.0

- Initial release.
- `/prune` forces `ContextCompressor.prune_tool_results_only()` on demand,
  relaxing only the three gating knobs (`proactive_prune_tokens`,
  the below-trigger check, `proactive_prune_min_reclaim_tokens`) plus the
  post-commit re-arm runway.
- `/prune --dry-run` reports reclaimable tokens against a deep copy without
  committing.
- Resolves the live conversation on both surfaces: desktop/TUI
  (`tui_gateway.server._sessions`) and classic CLI (plugin manager `_cli_ref`).
- Refuses to run against a session that is mid-turn.
