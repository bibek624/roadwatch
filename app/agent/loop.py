"""Agent loop: tool-use driver + context pruning + cost tracking.

Runs a single survey session. Writes `agent_trace.jsonl` as it goes.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import anthropic

from app.agent.prompts import AGENT_SYSTEM_PROMPT, build_seed_user_message
from app.agent.state import AgentState
from app.agent.tools import (
    TOOL_SCHEMAS,
    ToolResult,
    ToolServices,
    execute_tool,
)
from app.agent.trace import TraceWriter, prune_image_blocks, count_image_blocks
from app.claude import (
    PRICE_INPUT_PER_MTOK,
    PRICE_OUTPUT_PER_MTOK,
    _usage_to_tokens,
    estimate_cost_with_cache,
)

DEFAULT_MODEL = "claude-opus-4-7"


def _response_content_to_jsonable(content_blocks: Any) -> list[dict[str, Any]]:
    """Convert an Anthropic response's .content blocks to plain dicts we can
    store in the messages list and pass back to the API."""
    out: list[dict[str, Any]] = []
    for b in content_blocks:
        btype = getattr(b, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": b.text})
        elif btype == "tool_use":
            out.append({
                "type": "tool_use",
                "id": b.id,
                "name": b.name,
                "input": b.input if isinstance(b.input, dict) else dict(b.input),
            })
        elif btype == "thinking":
            # extended thinking block — preserve if present
            thinking_text = getattr(b, "thinking", None)
            signature = getattr(b, "signature", None)
            block: dict[str, Any] = {"type": "thinking"}
            if thinking_text is not None:
                block["thinking"] = thinking_text
            if signature is not None:
                block["signature"] = signature
            out.append(block)
        else:
            # best-effort fallback
            if hasattr(b, "model_dump"):
                out.append(b.model_dump())
            else:
                out.append({"type": btype or "unknown", "text": str(b)})
    return out


def _extract_tool_uses(content_blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in content_blocks if b.get("type") == "tool_use"]


def _extract_text(content_blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        b.get("text", "") for b in content_blocks if b.get("type") == "text"
    ).strip()


def _extract_thinking(content_blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        b.get("thinking", "") for b in content_blocks if b.get("type") == "thinking"
    ).strip()


async def run_agent(
    run_dir: Path,
    state: AgentState,
    svc: ToolServices,
    block_name: str,
    block_description: str,
    model: str = DEFAULT_MODEL,
    max_tokens_per_turn: int = 2500,
    keep_last_n_images: int = 2,
) -> dict[str, Any]:
    """Run the tool-use loop to completion or until caps hit."""
    trace_path = run_dir / "agent_trace.jsonl"
    trace = TraceWriter(trace_path)

    # header record — useful for the replay UI
    trace.write({
        "record_type": "run_header",
        "block_name": block_name,
        "model": model,
        "turn_cap": state.turn_cap,
        "budget_cap_usd": state.budget_cap_usd,
        "segments": [s.to_public() for s in state.segments.values()],
        "primaries_count": len(state.primaries),
        "started_ts": int(time.time() * 1000),
    })

    segments_summary = [s.to_public() for s in state.segments.values()]
    seed_text = build_seed_user_message(
        block_name=block_name,
        block_description=block_description,
        segments_summary=segments_summary,
        primaries_count=len(state.primaries),
        budget_cap_usd=state.budget_cap_usd,
        turn_cap=state.turn_cap,
    )

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": seed_text}]}
    ]

    system_param = [{
        "type": "text",
        "text": AGENT_SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]

    client = svc.anthropic_client

    done_signalled = False
    budget_warning_injected = False
    stop_reason = "unknown"

    for turn in range(state.turn_cap):
        state.turns_used = turn + 1

        # Context pruning BEFORE sending
        messages = prune_image_blocks(messages, keep_last_n_images=keep_last_n_images)

        # Send the turn
        try:
            resp = await client.messages.create(
                model=model,
                max_tokens=max_tokens_per_turn,
                system=system_param,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
        except anthropic.APIError as e:
            trace.write({
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
        state.add_cost(turn_cost)

        # Convert response to plain dicts
        asst_content = _response_content_to_jsonable(resp.content)
        thinking = _extract_thinking(asst_content)
        text_out = _extract_text(asst_content)
        tool_uses = _extract_tool_uses(asst_content)

        # Record the turn (Opus side) — we'll append tool_result records below
        trace.write({
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
            "budget_used_usd": round(state.budget_used_usd, 4),
            "stop_reason": resp.stop_reason,
        })

        # Append assistant message to history
        messages.append({"role": "assistant", "content": asst_content})

        # If no tool uses and stop_reason=end_turn → agent finished
        if not tool_uses:
            stop_reason = str(resp.stop_reason or "end_turn")
            break

        # Execute each tool call, collect results
        tool_result_blocks: list[dict[str, Any]] = []
        for tu in tool_uses:
            tname = tu["name"]
            targs = tu.get("input", {}) or {}
            tresult: ToolResult = await execute_tool(
                tname, tu["id"], targs, state, svc
            )
            trace.write({
                "record_type": "tool_result",
                "turn": turn + 1,
                "tool_use_id": tresult.tool_use_id,
                "tool_name": tresult.name,
                "args": targs,
                "summary": tresult.summary,
                "is_error": tresult.is_error,
                "cost_usd": round(tresult.cost_usd, 5),
                "state_delta": tresult.state_delta,
            })
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": tresult.tool_use_id,
                "content": tresult.content,
            }
            if tresult.is_error:
                block["is_error"] = True
            tool_result_blocks.append(block)
            if tresult.name == "done":
                done_signalled = True

        # Append tool_result message
        messages.append({"role": "user", "content": tool_result_blocks})

        # Done check
        if done_signalled:
            stop_reason = "agent_done"
            break

        # Budget cap reached
        if state.budget_used_usd >= state.budget_cap_usd:
            trace.write({
                "record_type": "system_note",
                "turn": turn + 1,
                "note": "budget_cap_reached_forced_exit",
            })
            stop_reason = "budget_cap"
            break

        # Budget warning — inject a user message once
        if (
            not budget_warning_injected
            and state.budget_used_usd >= state.budget_cap_usd * 0.85
        ):
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "BUDGET NOTICE: you have used "
                        f"${state.budget_used_usd:.2f} of ${state.budget_cap_usd:.2f}. "
                        "Wrap up cleanly. Mark any nearly-covered segments, then call `done` "
                        "with a summary. Do not start new deep analyses."
                    ),
                }],
            })
            budget_warning_injected = True
            trace.write({
                "record_type": "system_note",
                "turn": turn + 1,
                "note": "budget_warning_injected",
            })

        # Turn cap check — if we're about to enter the last turn, nudge
        if (
            not budget_warning_injected
            and state.turns_used >= state.turn_cap - 3
        ):
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "TURN NOTICE: you are close to the turn cap. "
                        "Wrap up cleanly and call `done` with a summary."
                    ),
                }],
            })
            budget_warning_injected = True

    trace.write({
        "record_type": "run_complete",
        "turns_used": state.turns_used,
        "budget_used_usd": round(state.budget_used_usd, 4),
        "findings_count": len(state.findings),
        "stop_reason": stop_reason,
    })
    trace.close()

    # Final artifacts
    state.write_artifacts()
    return {
        "stop_reason": stop_reason,
        "turns_used": state.turns_used,
        "budget_used_usd": state.budget_used_usd,
        "findings_count": len(state.findings),
    }
