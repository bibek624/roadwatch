"""Shared agent-loop machinery for Captain / Surveyor / Investigator.

Handles:
  - API call concurrency (RunState.api_sem) + RateLimitError exp backoff
  - Cost estimation
  - Message-history + image-block pruning
  - The turn-driver loop with a pluggable tool dispatcher

Each role's own module supplies:
  - Its tool schemas
  - Its system prompt (composed once per role per run, cache amortized)
  - The seed user message
  - A `dispatch_tool` async callable that maps tool name -> result tuple
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

import anthropic

from app.agent.hierarchy.agent_scratch import AgentScratch
from app.agent.hierarchy.run_state import RunState
from app.agent.loop import (
    _extract_text,
    _extract_thinking,
    _extract_tool_uses,
    _response_content_to_jsonable,
)
from app.agent.trace import prune_image_blocks
from app.claude import (
    PRICE_INPUT_PER_MTOK,
    PRICE_OUTPUT_PER_MTOK,
    _usage_to_tokens,
    estimate_cost_with_cache,
)


# Tool dispatcher contract:
#   async def dispatch_tool(tname: str, targs: dict, scratch: AgentScratch)
#     -> dict: {
#         "content": list[dict],           # Anthropic-shape blocks for tool_result
#         "summary": str,                  # short human label for trace
#         "is_error": bool,
#         "state_delta": dict,             # arbitrary trace fields
#         "side_effect": str | None,       # "done" | "report_year_findings" | etc.
#     }
ToolDispatcher = Callable[[str, dict, AgentScratch], Awaitable[dict[str, Any]]]


async def call_messages_with_retry(
    rs: RunState,
    aclient: anthropic.AsyncAnthropic,
    *,
    model: str,
    max_tokens: int,
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    max_retries: int = 3,
) -> Any:
    """messages.create wrapped in (a) RunState.api_sem (global concurrency cap)
    and (b) exponential backoff on RateLimitError / APIConnectionError."""
    delays = [4.0, 8.0, 16.0]
    last_exc: Exception | None = None
    async with rs.api_sem:
        for attempt in range(max_retries + 1):
            try:
                return await aclient.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    tools=tools,
                    messages=messages,
                )
            except anthropic.RateLimitError as e:
                last_exc = e
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
            except anthropic.APIConnectionError as e:
                last_exc = e
                if attempt >= max_retries:
                    raise
                await asyncio.sleep(2.0)
    if last_exc:
        raise last_exc
    raise RuntimeError("call_messages_with_retry exhausted without exception")


async def run_agent_loop(
    *,
    rs: RunState,
    scratch: AgentScratch,
    aclient: anthropic.AsyncAnthropic,
    model: str,
    system_prompt: list[dict[str, Any]],
    tool_schemas: list[dict[str, Any]],
    seed_user_text: str,
    dispatch_tool: ToolDispatcher,
    max_turns: int,
    max_tokens_per_turn: int,
    keep_last_n_images: int,
    terminal_side_effects: tuple[str, ...] = ("done",),
) -> dict[str, Any]:
    """Generic tool-use loop. Returns {stop_reason, turns_used, side_effects}.

    Records `agent_spawned` on entry and `agent_completed` on exit.
    Records `turn_assistant` + `tool_result` per turn (with agent fields
    auto-injected via scratch.trace).
    """
    await rs.update_agent(scratch.agent_id, state="running")
    await scratch.trace({
        "record_type": "agent_spawned",
        "spawn_reason": seed_user_text[:160],
    })

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": seed_user_text}]}
    ]
    side_effects_seen: list[str] = []
    stop_reason = "unknown"
    budget_warning_injected = False
    turn_warning_injected = False

    # Outer guard: budget + stop-flag at top of each turn
    for turn in range(max_turns):
        if rs.should_stop():
            stop_reason = "fleet_stopped"
            break

        scratch.turns_used = turn + 1

        # Prune image blocks BEFORE sending
        messages = prune_image_blocks(messages, keep_last_n_images=keep_last_n_images)

        try:
            resp = await call_messages_with_retry(
                rs, aclient,
                model=model,
                max_tokens=max_tokens_per_turn,
                system=system_prompt,
                tools=tool_schemas,
                messages=messages,
            )
        except anthropic.APIError as e:
            await scratch.trace({
                "record_type": "turn_error",
                "turn": turn + 1,
                "error": f"{type(e).__name__}: {e}",
            })
            stop_reason = f"api_error:{type(e).__name__}"
            break

        usage = _usage_to_tokens(resp.usage)
        turn_cost = estimate_cost_with_cache(
            usage, PRICE_INPUT_PER_MTOK, PRICE_OUTPUT_PER_MTOK
        )
        await scratch.add_cost(turn_cost)

        asst_content = _response_content_to_jsonable(resp.content)
        thinking = _extract_thinking(asst_content)
        text_out = _extract_text(asst_content)
        tool_uses = _extract_tool_uses(asst_content)

        await scratch.trace({
            "record_type": "turn_assistant",
            "turn": turn + 1,
            "thinking": thinking,
            "text": text_out,
            "tool_uses": [
                {"id": tu["id"], "name": tu["name"], "input": tu.get("input", {})}
                for tu in tool_uses
            ],
            "usage": usage,
            "cost_usd": round(turn_cost, 5),
            "budget_used_usd": round(rs.budget_used_usd, 4),
            "stop_reason": resp.stop_reason,
        })

        messages.append({"role": "assistant", "content": asst_content})

        if not tool_uses:
            stop_reason = str(resp.stop_reason or "end_turn")
            break

        tool_result_blocks: list[dict[str, Any]] = []
        for tu in tool_uses:
            tname = tu["name"]
            targs = tu.get("input", {}) or {}
            try:
                tres = await dispatch_tool(tname, targs, scratch)
            except Exception as e:
                tres = {
                    "content": [{"type": "text", "text": f"tool error: {e}"}],
                    "summary": f"error: {type(e).__name__}",
                    "is_error": True,
                    "state_delta": {"error": str(e)},
                    "side_effect": None,
                }

            await scratch.trace({
                "record_type": "tool_result",
                "turn": turn + 1,
                "tool_use_id": tu["id"],
                "tool_name": tname,
                "args": targs,
                "summary": tres.get("summary", ""),
                "is_error": bool(tres.get("is_error")),
                "state_delta": tres.get("state_delta", {}),
            })
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": tres.get("content", []),
            }
            if tres.get("is_error"):
                block["is_error"] = True
            tool_result_blocks.append(block)

            se = tres.get("side_effect")
            if se:
                side_effects_seen.append(se)

        messages.append({"role": "user", "content": tool_result_blocks})

        if any(se in terminal_side_effects for se in side_effects_seen):
            stop_reason = "agent_done"
            break

        # Budget hard-cap (atomic flag set by RunState.add_cost at 95%)
        if rs.stop_event.is_set():
            await scratch.trace({
                "record_type": "system_note",
                "turn": turn + 1,
                "note": "fleet_budget_hard_cap_reached",
            })
            stop_reason = "fleet_budget_cap"
            break

        # Budget warning at 85%
        if not budget_warning_injected and rs.budget_pct_used >= 0.85:
            messages.append({"role": "user", "content": [{
                "type": "text",
                "text": (
                    f"BUDGET NOTICE: fleet has used "
                    f"${rs.budget_used_usd:.2f} of ${rs.budget_cap_usd:.2f}. "
                    "Wrap up cleanly with what you have. Don't start new "
                    "deep investigations."
                ),
            }]})
            budget_warning_injected = True
            await scratch.trace({
                "record_type": "system_note",
                "turn": turn + 1,
                "note": "budget_warning_injected",
            })

        # Turn-cap warning at cap-1
        if not turn_warning_injected and scratch.turns_used >= max_turns - 1:
            messages.append({"role": "user", "content": [{
                "type": "text",
                "text": (
                    "TURN NOTICE: this is your final turn. Wrap up immediately "
                    "with whatever evidence you have."
                ),
            }]})
            turn_warning_injected = True

    # Mark complete
    await rs.update_agent(
        scratch.agent_id,
        state="completed",
        cost_usd=scratch.cost_usd_local,
        turns_used=scratch.turns_used,
        completed=True,
    )
    await scratch.trace({
        "record_type": "agent_completed",
        "stop_reason": stop_reason,
        "turns_used": scratch.turns_used,
        "cost_usd": round(scratch.cost_usd_local, 5),
    })

    return {
        "stop_reason": stop_reason,
        "turns_used": scratch.turns_used,
        "cost_usd": scratch.cost_usd_local,
        "side_effects": side_effects_seen,
    }
