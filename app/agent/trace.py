"""JSONL trace writer + context pruner.

The trace is the hero artifact — it's what the replay UI plays back. One line
per turn. Each line has enough information to reconstruct what the agent did
without replaying any model calls.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class TraceWriter:
    def __init__(self, trace_path: Path):
        self.path = Path(trace_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8", buffering=1)
        self._start_ms = int(time.time() * 1000)

    def write(self, record: dict[str, Any]) -> None:
        record = {"t_ms": int(time.time() * 1000) - self._start_ms, **record}
        self._fh.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def _json_default(obj: Any) -> Any:
    # Anthropic SDK blocks are pydantic-ish; try dict / vars fallback
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


# ---------------------------------------------------------------------------
# Context pruning — keep only the last N image blocks in the message history
# ---------------------------------------------------------------------------

def prune_image_blocks(
    messages: list[dict[str, Any]],
    keep_last_n_images: int = 2,
) -> list[dict[str, Any]]:
    """Walk the messages in reverse, keep the last N image blocks verbatim, and
    replace older image blocks with a short text placeholder referencing the
    tool_use_id they came from.

    Text blocks (including tool_use and tool_result text) are always preserved.

    Shape preserved:
      - messages[i]["role"] = "user" | "assistant"
      - messages[i]["content"] = list[block] where each block is dict-ish

    Anthropic SDK response .content can be pydantic objects — we only mutate
    plain dicts, so the caller should convert before calling (loop.py does).
    """
    images_kept = 0
    # walk in reverse, rewrite
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_content: list[Any] = []
        for block in content:
            replaced = _maybe_replace_image(block, images_kept, keep_last_n_images)
            if replaced is block:
                # unchanged; track image if it IS an image
                if _is_image_block(block):
                    images_kept += 1
            else:
                pass  # replaced with text, don't count
            new_content.append(replaced)
        msg["content"] = new_content
    return messages


def _is_image_block(block: Any) -> bool:
    if isinstance(block, dict):
        if block.get("type") == "image":
            return True
        # inside a tool_result content array
        if block.get("type") == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                return any(
                    isinstance(b, dict) and b.get("type") == "image" for b in inner
                )
    return False


def _maybe_replace_image(
    block: Any, images_kept: int, keep_last_n_images: int
) -> Any:
    """If block contains image(s) and we've already kept >= keep_last_n_images,
    replace image content with a text placeholder."""
    if not isinstance(block, dict):
        return block

    btype = block.get("type")
    if btype == "image":
        if images_kept >= keep_last_n_images:
            return {
                "type": "text",
                "text": "[image pruned — older than last N turns]",
            }
        return block

    if btype == "tool_result":
        inner = block.get("content")
        if not isinstance(inner, list):
            return block
        contains_image = any(
            isinstance(b, dict) and b.get("type") == "image" for b in inner
        )
        if not contains_image:
            return block
        if images_kept >= keep_last_n_images:
            # rewrite: strip images from the tool_result's content list
            new_inner = []
            for b in inner:
                if isinstance(b, dict) and b.get("type") == "image":
                    new_inner.append({
                        "type": "text",
                        "text": "[image pruned to save tokens]",
                    })
                else:
                    new_inner.append(b)
            new_block = dict(block)
            new_block["content"] = new_inner
            return new_block
        # otherwise keep as-is and let caller count the image
        return block

    return block


def count_image_blocks(messages: list[dict[str, Any]]) -> int:
    n = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if _is_image_block(b):
                n += 1
    return n
