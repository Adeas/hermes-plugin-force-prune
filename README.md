# force-prune

**`/prune` — force Hermes' deterministic, no-LLM tool-result prune on demand.**

Hermes already ships the prune this plugin exposes:
`ContextCompressor.prune_tool_results_only()`. It dedups byte-identical tool
results, summarizes oversized ones, truncates bloated `tool_call` arguments and
retires stale image payloads — all deterministically, with **no model call**.

By default it only runs mid-turn, and only once two gates open: history must
exceed `compression.proactive_prune_tokens`, and the prune must reclaim at
least `compression.proactive_prune_min_reclaim_tokens` before it commits. That
is the right policy for automatic operation, but it leaves no way to say
*"reclaim what you can, now."*

This plugin adds that.

## Why you might want it

`/compress` calls a summariser model, so it inherits that model's failure
modes. On a local provider a large compaction can stall: a 312K-token
compaction streamed past its 600s ceiling and aborted without committing,
leaving the session unusable.

There is also a chicken-and-egg case. Compaction has to *send* the history it
is compacting, so the transcript must still fit the summariser's context
window in order to be shrunk. Once you are over that limit — or you switch to
a model with a smaller window mid-session, so history that fit an hour ago no
longer does — `/compress` cannot run at all. The one command that would
recover the session is the one the session has outgrown.

The deterministic prune never sends the transcript anywhere, so no context
limit applies to it: it cannot stall, time out, or be refused for being too
large. `/prune` makes it reachable directly, off the chat turn — including as
a way back from a session `/compress` has already given up on.

## Install

```sh
hermes plugins install Adeas/hermes-plugin-force-prune
hermes plugins enable force-prune
```

Or clone it manually:

```sh
git clone https://github.com/Adeas/hermes-plugin-force-prune.git \
  ~/.hermes/plugins/force-prune
hermes plugins enable force-prune
```

Enabling adds it to `plugins.enabled` in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - force-prune
```

Restart Hermes (or start a new session) so the plugin registers its command.

## Usage

```
/prune            reclaim now and commit
/prune --dry-run  report what would be reclaimed, change nothing
/prune --help     usage
```

`--preview` and `-n` are accepted as aliases for `--dry-run`.

Example output:

```
/prune — 14 tool result(s) pruned
  session   desktop-main
  messages  238 → 238
  tokens    ~312,480 → ~119,730 (reclaimed ~192,750)
No summariser model was called.
```

A dry run reports the same numbers as "would reclaim" and changes nothing.

## What the override actually relaxes

Only the three **gating** knobs, and nothing else:

| Knob | Normal behaviour | Under `/prune` |
| --- | --- | --- |
| `proactive_prune_tokens` | history must exceed it | raised to `1` **only if it is `≤ 0`** (feature off); a real configured trigger is left intact, because it also feeds the post-commit runway |
| below-trigger check | skips the prune under the threshold | skipped, via `current_tokens=None` |
| `proactive_prune_min_reclaim_tokens` | won't commit a small reclaim | lowered to `1` — *force* means commit whatever is reclaimable |
| `_proactive_prune_rearm_tokens` | a recent automatic prune vetoes a new one | zeroed, so the veto can't block a manual run. On a commit the method sets and persists its own fresh runway — but it derives that runway from `proactive_prune_min_reclaim_tokens` too, while the override above is still live, so the plugin adds the lost floor back and persists the correction. Only a no-op restores the old runway wholesale |

The recent tail (`compression.protect_last_n`) and the session-store
capability check are **left alone**, and the prune is invoked with exactly the
arguments `prune_tool_results_only` passes itself — so a forced prune never
drops content the automatic one would have protected.

`compression.protect_first_n` is **not** a guard on this path, despite the
name: `_prune_old_tool_results` contains no head logic at all, and
`protect_first_n` only sizes the eligibility floor in the caller's
message-count gate.

All gates are restored in a `finally` block, so a failure mid-prune cannot
leave the compressor permanently unlatched.

## Safety notes

- **Never races a running turn.** The turn loop reassigns `messages` as it
  goes, so rewriting the transcript underneath it would either be silently
  discarded or corrupt the in-flight request. A mid-turn session is refused
  with a message, not pruned.
- **Dry run deep-copies.** The prune passes rewrite message bodies in place, so
  `--dry-run` works on a `copy.deepcopy` — a shallow copy would leak edits into
  the live transcript even though nothing was committed.
- **Commits both aliases.** On the desktop path `session["history"]` and
  `agent._session_messages` alias the same list once a turn completes, so both
  references are repointed at the pruned list. Otherwise the next turn
  resurrects the unpruned one.
- **Names the gate instead of guessing.** `prune_tool_results_only` collapses
  every rejection — disabled, below trigger, too few messages, no persistence
  capability, nothing found, reclaim too small, *and a failed DB commit* — into
  one `(messages, 0)`. The two structural gates are therefore checked before
  pruning, so they can be reported precisely; and if a prune still comes back
  empty, the pure pass is re-run to tell "already compact" apart from "the
  store rejected the commit". A failed commit is never reported as success, nor
  as an already-compact transcript.
- **`--dry-run` cannot over-promise.** The dry pass calls
  `_prune_old_tool_results` directly and so bypasses those gates; it is run
  behind the same pre-flight check, so it will not advertise a reclaim that
  `/prune` would then refuse.

## Where it looks for your session

`register_command()` handlers receive only `raw_args` — no session handle — so
the live conversation has to be located. Two surfaces are supported:

- **desktop / TUI** — `tui_gateway.server._sessions`, newest registered
  session, read under `_sessions_lock` and `history_lock`. The session label is
  printed in the output, so a wrong guess is visible rather than silent.
- **classic CLI** — the plugin manager's `_cli_ref` back-reference, captured
  from the `PluginContext` at `register()`.

If neither resolves, `/prune` says so instead of failing silently.

## Compatibility

Developed and verified against **hermes-agent 0.21.0**.

The plugin reaches into Hermes internals that are not a public API:

- `agent.context_compressor._estimate_msg_budget_tokens`
- `ContextCompressor.prune_tool_results_only`
- `ContextCompressor._prune_old_tool_results`
- `tui_gateway.server._sessions` / `_sessions_lock`

Every one of those touchpoints is wrapped so a rename degrades to a readable
message rather than a traceback — the token estimator falls back to a
~4-chars-per-token approximation, and a session whose context engine has been
replaced by a plugin gets an explicit "no deterministic prune" reply. Still,
expect to bump this plugin when Hermes reworks its compaction internals.

## License

MIT — see [LICENSE](LICENSE).
