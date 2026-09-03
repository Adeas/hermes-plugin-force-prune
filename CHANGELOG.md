# Changelog

## 1.0.1

- Fix: a failed session-store commit reported as
  "Nothing to prune — everything outside the protected tail is already
  compact". `prune_tool_results_only` returns `(messages, 0)` for *every*
  rejection including a failed `archive_and_compact`, so the branch that was
  meant to catch it sat behind the zero-count check and could never run. The
  structural gates are now checked before pruning and reported by name, and a
  zero-count result is re-scanned to tell a compact transcript apart from a
  rejected commit.
- Fix: `--dry-run` could promise a reclaim `/prune` would then refuse, because
  the dry pass calls `_prune_old_tool_results` directly and bypasses both the
  message-count floor and the session-store capability gate. It now runs behind
  the same pre-flight check.
- Fix: the post-commit re-arm runway lost its configured floor.
  `prune_tool_results_only` derives the runway from
  `proactive_prune_min_reclaim_tokens` while this plugin's override is still
  live, so the default 4096-token floor was replaced by `1`, shortening the
  cache-break spacing. The difference is now added back and persisted via
  `patch_session_model_config`.
- Docs: `protect_first_n` was described as a guard the prune respects. It is
  not — `_prune_old_tool_results` has no head logic; `protect_first_n` only
  sizes the eligibility floor in the caller's message-count gate.

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
