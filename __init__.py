"""``/prune`` — force Hermes' deterministic tool-result prune on demand.

Hermes already ships the prune this command exposes:
``ContextCompressor.prune_tool_results_only()``. It dedups byte-identical tool
results, summarizes oversized ones, truncates bloated tool_call arguments and
retires stale image payloads — all deterministically, with **no LLM call**.

By default it only runs mid-turn, and only once two gates open: history must
exceed ``compression.proactive_prune_tokens``, and the prune must reclaim at
least ``compression.proactive_prune_min_reclaim_tokens`` before it commits.
That is the right policy for automatic operation, but it leaves no way to say
"reclaim what you can, now".

Why that matters here: the LLM-backed compaction path can fail outright on a
local provider — a 312K-token compaction streamed past its 600s ceiling and
aborted without committing, leaving the session unusable. The deterministic
prune has no model call, so it cannot stall or time out. This command makes it
reachable directly, off the chat turn.

Scope of the override: only the three *gating* knobs are relaxed. The recent
tail (``protect_last_n``), the head (``protect_first_n``) and the session-store
capability check are correctness guards and are left alone — a forced prune
never drops content the automatic one would have protected.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Captured at register() so the CLI-mode agent lookup can reach the plugin
# manager's back-reference to the running CLI. Unused on the desktop path.
_ctx: Any = None

_HELP_TEXT = """/prune — force the deterministic, no-LLM tool-result prune.

  /prune            reclaim now and commit
  /prune --dry-run  report what would be reclaimed, change nothing
  /prune --help     this message

Unlike /compress this never calls a summariser model, so it cannot stall or
time out. It protects the recent tail (compression.protect_last_n) and the
head (compression.protect_first_n) exactly as the automatic prune does."""


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------

def _estimate_tokens(messages: list) -> int:
    """Best-effort token estimate for a message list.

    Prefers the compressor's own estimator so the numbers reported here match
    the ones its internal gates reason about.
    """
    try:
        from agent.context_compressor import _estimate_msg_budget_tokens

        return sum(_estimate_msg_budget_tokens(m) for m in messages)
    except Exception:
        # ~4 chars/token over serialized bodies. Only used for display.
        total = 0
        for m in messages or ():
            try:
                total += len(str(m.get("content") or "")) // 4
                for call in m.get("tool_calls") or ():
                    total += len(str(call)) // 4
            except Exception:
                continue
        return total


# ---------------------------------------------------------------------------
# Target resolution
#
# register_command() handlers receive only ``raw_args`` — no session handle —
# so the live conversation has to be located. Two surfaces matter:
#   * desktop / TUI : tui_gateway.server._sessions
#   * classic CLI   : the plugin manager's _cli_ref back-reference
# ---------------------------------------------------------------------------

class _Target:
    """A resolved conversation the prune can act on."""

    def __init__(self, label: str, agent: Any, messages: list, commit: Callable):
        self.label = label
        self.agent = agent
        self.messages = messages
        self.commit = commit


def _desktop_target() -> tuple[Optional[_Target], Optional[str]]:
    """Resolve the live desktop/TUI session.

    Returns ``(target, error)``. A running session is refused rather than
    raced: the turn loop reassigns ``messages`` as it goes, so rewriting the
    transcript underneath it would either be silently discarded or corrupt the
    in-flight request.
    """
    try:
        from tui_gateway import server as tui
    except Exception:
        return None, None

    sessions = getattr(tui, "_sessions", None)
    if not isinstance(sessions, dict):
        return None, None

    lock = getattr(tui, "_sessions_lock", None)
    if lock is not None:
        with lock:
            items = list(sessions.items())
    else:
        items = list(sessions.items())

    candidates = [
        (sid, s)
        for sid, s in items
        if isinstance(s, dict) and s.get("agent") is not None
    ]
    if not candidates:
        return None, None

    # dict preserves insertion order, so the newest registered session is the
    # one the user is most likely looking at. The label in the output names it
    # explicitly, so a wrong guess is visible rather than silent.
    sid, session = candidates[-1]
    agent = session.get("agent")

    if session.get("running"):
        return None, (
            "(._.) The agent is mid-turn — /prune rewrites the transcript and "
            "won't race a running turn. Try again once it finishes."
        )

    hist_lock = session.get("history_lock")
    if hist_lock is not None:
        with hist_lock:
            messages = list(session.get("history") or [])
    else:
        messages = list(session.get("history") or [])

    def _commit(new_messages: list) -> None:
        # session["history"] and agent._session_messages alias the SAME list
        # once a turn completes (tui_gateway/server.py), so both references
        # must be repointed at the new list or the next turn resurrects the
        # unpruned one. Durable persistence already happened inside
        # prune_tool_results_only via session_db.archive_and_compact.
        if hist_lock is not None:
            with hist_lock:
                session["history"] = new_messages
        else:
            session["history"] = new_messages
        try:
            agent._session_messages = new_messages
        except Exception:
            logger.debug("could not repoint agent._session_messages", exc_info=True)

    label = str(session.get("session_key") or sid)
    return _Target(label, agent, messages, _commit), None


def _cli_target() -> Optional[_Target]:
    """Resolve the classic-CLI agent, if this process is running one."""
    manager = getattr(_ctx, "_manager", None) if _ctx is not None else None
    cli = getattr(manager, "_cli_ref", None) if manager is not None else None
    agent = getattr(cli, "agent", None) if cli is not None else None
    if agent is None:
        return None

    messages = list(getattr(agent, "_session_messages", None) or [])
    if not messages:
        return None

    def _commit(new_messages: list) -> None:
        agent._session_messages = new_messages

    label = str(getattr(agent, "session_id", "") or "cli")
    return _Target(label, agent, messages, _commit)


def _resolve_target() -> tuple[Optional[_Target], Optional[str]]:
    target, err = _desktop_target()
    if err:
        return None, err
    if target is not None:
        return target, None
    return _cli_target(), None


# ---------------------------------------------------------------------------
# The prune itself
# ---------------------------------------------------------------------------

def _force_prune(compressor: Any, messages: list) -> tuple[list, int]:
    """Run ``prune_tool_results_only`` with the automatic gates relaxed.

    Three gates are stood down, and nothing else:

    * ``proactive_prune_tokens <= 0`` (feature switched off) — raised to 1 only
      when it is actually disabled, so an operator who configured a real
      trigger keeps it. That value also feeds the post-commit runway, so
      leaving it intact preserves the configured cache-break spacing.
    * the below-trigger check — skipped by passing ``current_tokens=None``.
    * ``proactive_prune_min_reclaim_tokens`` — lowered to 1, because "force"
      means commit whatever is reclaimable, not just a large batch.

    The disarm runway is zeroed so a recent automatic prune can't veto this
    run. If the prune commits, the method sets its own fresh runway (and
    persists it) — that value is kept. Only a no-op restores the old one.
    """
    saved_trigger = getattr(compressor, "proactive_prune_tokens", 0)
    saved_min_reclaim = getattr(compressor, "proactive_prune_min_reclaim_tokens", 0)
    saved_rearm = getattr(compressor, "_proactive_prune_rearm_tokens", 0)

    try:
        if saved_trigger <= 0:
            compressor.proactive_prune_tokens = 1
        compressor.proactive_prune_min_reclaim_tokens = 1
        compressor._proactive_prune_rearm_tokens = 0

        pruned, count = compressor.prune_tool_results_only(
            messages, current_tokens=None
        )
    finally:
        try:
            compressor.proactive_prune_tokens = saved_trigger
            compressor.proactive_prune_min_reclaim_tokens = saved_min_reclaim
        except Exception:
            logger.debug("could not restore prune gates", exc_info=True)

    committed = bool(count) and pruned is not messages
    if not committed:
        # No commit happened, so the zeroed runway would wrongly re-arm the
        # automatic prune on the next turn. Put it back.
        try:
            compressor._proactive_prune_rearm_tokens = saved_rearm
        except Exception:
            logger.debug("could not restore prune runway", exc_info=True)

    return pruned, count


def _dry_run(compressor: Any, messages: list) -> tuple[list, int]:
    """Report what a prune would reclaim without committing anything.

    Calls the pure pass directly, on a deep copy — the prune passes rewrite
    message bodies in place, so a shallow copy would leak edits into the live
    transcript even though nothing was committed.
    """
    scratch = copy.deepcopy(messages)
    return compressor._prune_old_tool_results(
        scratch,
        protect_tail_count=getattr(compressor, "protect_last_n", 20),
        protect_tail_tokens=None,
        min_prune_chars=getattr(compressor, "proactive_prune_min_result_chars", 8000),
    )


# ---------------------------------------------------------------------------
# Slash handler
# ---------------------------------------------------------------------------

def _handle_slash(raw_args: str) -> str:
    args = (raw_args or "").strip().lower()
    if args in {"-h", "--help", "help"}:
        return _HELP_TEXT

    dry = args in {"--dry-run", "--preview", "-n", "dry-run", "preview"}
    if args and not dry:
        return f"Unknown argument: {raw_args.strip()}\n\n{_HELP_TEXT}"

    target, err = _resolve_target()
    if err:
        return err
    if target is None:
        return "(._.) No active session — send a message first."

    compressor = getattr(target.agent, "context_compressor", None)
    if compressor is None or not callable(
        getattr(compressor, "prune_tool_results_only", None)
    ):
        return (
            "(._.) This session's context engine has no deterministic prune "
            "(a plugin context engine may have replaced the built-in compressor)."
        )

    before_msgs = len(target.messages)
    before_tokens = _estimate_tokens(target.messages)

    try:
        if dry:
            pruned, count = _dry_run(compressor, target.messages)
        else:
            pruned, count = _force_prune(compressor, target.messages)
    except Exception as exc:
        logger.debug("forced prune failed", exc_info=True)
        return f"(._.) Prune failed: {exc}"

    if not count:
        return (
            f"Nothing to prune in {target.label} — "
            f"{before_msgs} messages, ~{before_tokens:,} tokens.\n"
            "Everything outside the protected tail is already compact."
        )

    after_tokens = _estimate_tokens(pruned)
    reclaimed = max(0, before_tokens - after_tokens)

    if dry:
        return (
            f"/prune --dry-run — {count} tool result(s) would be pruned\n"
            f"  session   {target.label}\n"
            f"  messages  {before_msgs} → {len(pruned)}\n"
            f"  tokens    ~{before_tokens:,} → ~{after_tokens:,} "
            f"(would reclaim ~{reclaimed:,})\n"
            "Nothing was changed. Run /prune to commit."
        )

    if pruned is target.messages:
        # prune_tool_results_only's no-op contract: the DB commit failed and it
        # deliberately kept the original transcript.
        return (
            "(._.) The prune could not be committed to the session store; "
            "the transcript was left untouched. See the agent log for details."
        )

    target.commit(pruned)
    return (
        f"/prune — {count} tool result(s) pruned\n"
        f"  session   {target.label}\n"
        f"  messages  {before_msgs} → {len(pruned)}\n"
        f"  tokens    ~{before_tokens:,} → ~{after_tokens:,} "
        f"(reclaimed ~{reclaimed:,})\n"
        "No summariser model was called."
    )


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_command(
        "prune",
        handler=_handle_slash,
        description="Force the deterministic, no-LLM tool-result prune (no summariser call).",
        args_hint="[--dry-run]",
    )
