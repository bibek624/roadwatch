"""Claude vision client for hazard analysis.

Single-category entry point `analyze_hazards(viewports, category, meta)`.
Sends all viewports in one multimodal message with per-image label text so
the model can reference viewports by name in its JSON output.
"""
from __future__ import annotations

import base64
import io
import json
import os
from typing import Any

import anthropic
from PIL import Image

from app.panorama import Viewport
from app.prompts import load as load_prompt

DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_TRIAGE_MODEL = "claude-haiku-4-5"

# Per-1M-token pricing
PRICE_INPUT_PER_MTOK = 15.0   # Opus 4.7 input
PRICE_OUTPUT_PER_MTOK = 75.0  # Opus 4.7 output
HAIKU_PRICE_INPUT = 1.0       # Haiku 4.5 input
HAIKU_PRICE_OUTPUT = 5.0      # Haiku 4.5 output


def _b64_jpeg(path: str) -> str:
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("ascii")


def _extract_json(text: str) -> dict[str, Any]:
    """Find and parse the first top-level JSON object in `text`.
    Falls back to {"raw": text} if parsing fails."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return {"raw": text}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {"raw": text}


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * PRICE_INPUT_PER_MTOK / 1_000_000.0
        + output_tokens * PRICE_OUTPUT_PER_MTOK / 1_000_000.0
    )


def analyze_hazards(
    viewports: list[Viewport],
    category: str,
    meta: dict[str, Any],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 2000,
) -> dict[str, Any]:
    """Run one Claude vision call over the rendered viewports.

    Returns:
        {
          "category": str,
          "model": str,
          "findings": <parsed JSON from Claude>,
          "usage": {"input_tokens": int, "output_tokens": int},
          "cost_usd": float,
        }
    """
    prompt_mod = load_prompt(category)
    system_prompt = prompt_mod.SYSTEM_PROMPT
    user_template = prompt_mod.USER_PROMPT_TEMPLATE

    if not viewports:
        raise ValueError("no viewports provided")
    for vp in viewports:
        if not vp.path or not os.path.exists(vp.path):
            raise FileNotFoundError(
                f"viewport '{vp.name}' has no saved path (expected JPEG at vp.path)"
            )

    content: list[dict[str, Any]] = []
    for vp in viewports:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": _b64_jpeg(vp.path),
            },
        })
        content.append({
            "type": "text",
            "text": (
                f"Viewport: {vp.name} "
                f"(yaw={vp.yaw:+.0f}°, pitch={vp.pitch:+.0f}°, hfov={vp.hfov:.0f}°)"
            ),
        })
    content.append({"type": "text", "text": user_template.format(**meta)})

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    )

    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    findings = _extract_json(text)
    usage = {
        "input_tokens": int(msg.usage.input_tokens),
        "output_tokens": int(msg.usage.output_tokens),
    }
    return {
        "category": category,
        "model": model,
        "findings": findings,
        "usage": usage,
        "cost_usd": round(estimate_cost(usage["input_tokens"], usage["output_tokens"]), 4),
    }


# ---------------------------------------------------------------------------
# Tier A — Haiku triage
# ---------------------------------------------------------------------------

TRIAGE_MAX_LONG_SIDE = 1568  # downscale panos before triage for cost


def _prep_image_for_triage(path: str, max_long: int = TRIAGE_MAX_LONG_SIDE) -> bytes:
    """Load, downscale if longest side > max_long, return JPEG bytes."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        longest = max(w, h)
        if longest > max_long:
            scale = max_long / longest
            im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()


def estimate_triage_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * HAIKU_PRICE_INPUT / 1_000_000.0
        + output_tokens * HAIKU_PRICE_OUTPUT / 1_000_000.0
    )


async def triage_image_async(
    client: "anthropic.AsyncAnthropic",
    path: str,
    model: str = DEFAULT_TRIAGE_MODEL,
    max_tokens: int = 400,
) -> dict[str, Any]:
    """One Haiku triage call on a single image. Async — pass a shared client."""
    from app.prompts.triage import SYSTEM_PROMPT, USER_PROMPT

    jpeg_bytes = _prep_image_for_triage(path)
    b64 = base64.standard_b64encode(jpeg_bytes).decode("ascii")

    msg = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": USER_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    triage = _extract_json(text)
    usage = {
        "input_tokens": int(msg.usage.input_tokens),
        "output_tokens": int(msg.usage.output_tokens),
    }
    return {
        "path": path,
        "model": model,
        "triage": triage,
        "usage": usage,
        "cost_usd": round(estimate_triage_cost(usage["input_tokens"], usage["output_tokens"]), 5),
    }


# ---------------------------------------------------------------------------
# Pavement pipeline — Haiku validity+visibility, Opus 5-tier rating
# ---------------------------------------------------------------------------

# Cache-aware pricing. Anthropic's prompt caching discounts CACHED input at
# ~10% of base ("cache_read"), and CACHE-WRITES cost ~1.25x base ("cache_write").
# See: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
CACHE_READ_DISCOUNT = 0.10
CACHE_WRITE_MULTIPLIER = 1.25


def _usage_to_tokens(usage: Any) -> dict[str, int]:
    """Extract token counts from an Anthropic Usage object, including cache
    fields when present. Missing fields -> 0."""
    def _i(x: Any) -> int:
        return int(x) if x is not None else 0
    return {
        "input_tokens": _i(getattr(usage, "input_tokens", 0)),
        "output_tokens": _i(getattr(usage, "output_tokens", 0)),
        "cache_creation_input_tokens": _i(getattr(usage, "cache_creation_input_tokens", 0)),
        "cache_read_input_tokens": _i(getattr(usage, "cache_read_input_tokens", 0)),
    }


def estimate_cost_with_cache(
    usage: dict[str, int], price_in_per_mtok: float, price_out_per_mtok: float,
) -> float:
    """Cost estimate accounting for prompt-cache read/write line items."""
    base_in = usage.get("input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    cache_write = usage.get("cache_creation_input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cost = (
        base_in * price_in_per_mtok / 1_000_000.0
        + cache_read * price_in_per_mtok * CACHE_READ_DISCOUNT / 1_000_000.0
        + cache_write * price_in_per_mtok * CACHE_WRITE_MULTIPLIER / 1_000_000.0
        + out * price_out_per_mtok / 1_000_000.0
    )
    return cost


# --- Pass 1: Haiku validity + visibility ----------------------------------

PAVEMENT_TRIAGE_MAX_LONG = 1024  # the thumb_1024 cache already matches this


async def classify_validity_visibility_async(
    client: "anthropic.AsyncAnthropic",
    path: str,
    model: str = DEFAULT_TRIAGE_MODEL,
    max_tokens: int = 200,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Haiku call: is this a valid pavement-assessable street image, and what
    are the forward/backward visibility distances? Returns:
      {
        "path": str, "model": str,
        "result": {"valid": bool, "reason": str,
                   "vis_forward_m": int, "vis_backward_m": int},
        "usage": {...}, "cost_usd": float,
      }
    """
    from app.prompts.pavement import (
        VALIDITY_VISIBILITY_SYSTEM_PROMPT,
        VALIDITY_VISIBILITY_USER_PROMPT,
    )

    jpeg_bytes = _prep_image_for_triage(path, max_long=PAVEMENT_TRIAGE_MAX_LONG)
    b64 = base64.standard_b64encode(jpeg_bytes).decode("ascii")

    system: Any
    if use_cache:
        system = [{
            "type": "text",
            "text": VALIDITY_VISIBILITY_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }]
    else:
        system = VALIDITY_VISIBILITY_SYSTEM_PROMPT

    msg = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": VALIDITY_VISIBILITY_USER_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    parsed = _extract_json(text)
    usage = _usage_to_tokens(msg.usage)
    cost = estimate_cost_with_cache(usage, HAIKU_PRICE_INPUT, HAIKU_PRICE_OUTPUT)
    return {
        "path": path,
        "model": model,
        "result": parsed,
        "usage": usage,
        "cost_usd": round(cost, 6),
    }


# --- Pass 2: Opus pavement rating (forward + backward strips) --------------

PAVEMENT_STRIP_LONG_SIDE = 768  # match panorama.STRIP_W — no extra downscaling


def _image_block(path: str, max_long: int = PAVEMENT_STRIP_LONG_SIDE) -> dict[str, Any]:
    jpeg_bytes = _prep_image_for_triage(path, max_long=max_long)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg_bytes).decode("ascii"),
        },
    }


async def detect_forward_yaw_async(
    client: "anthropic.AsyncAnthropic",
    probe_path: str,
    model: str = DEFAULT_TRIAGE_MODEL,
    max_tokens: int = 150,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Haiku call on a 2x2 orientation-probe image (top-left=yaw0, top-right=yaw90,
    bot-left=yaw180, bot-right=yaw270). Returns {'forward_yaw_deg': 0|90|180|270,
    'confidence': float, 'reason': str} plus usage/cost.
    """
    from app.prompts.pavement import (
        ORIENTATION_SYSTEM_PROMPT,
        ORIENTATION_USER_PROMPT,
    )
    jpeg_bytes = _prep_image_for_triage(probe_path, max_long=1024)
    b64 = base64.standard_b64encode(jpeg_bytes).decode("ascii")
    system: Any
    if use_cache:
        system = [{"type": "text", "text": ORIENTATION_SYSTEM_PROMPT,
                   "cache_control": {"type": "ephemeral"}}]
    else:
        system = ORIENTATION_SYSTEM_PROMPT
    msg = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": ORIENTATION_USER_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    parsed = _extract_json(text)
    usage = _usage_to_tokens(msg.usage)
    cost = estimate_cost_with_cache(usage, HAIKU_PRICE_INPUT, HAIKU_PRICE_OUTPUT)
    # normalize to one of {0, 90, 180, 270}
    y = int(parsed.get("forward_yaw_deg", 0)) if isinstance(parsed, dict) else 0
    y = min([0, 90, 180, 270], key=lambda v: abs(((y - v) + 180) % 360 - 180))
    return {
        "probe_path": probe_path,
        "model": model,
        "forward_yaw_deg": y,
        "confidence": float(parsed.get("confidence", 0.0)) if isinstance(parsed, dict) else 0.0,
        "reason": str(parsed.get("reason", "")) if isinstance(parsed, dict) else "",
        "usage": usage,
        "cost_usd": round(cost, 6),
    }


async def rate_pavement_async(
    client: "anthropic.AsyncAnthropic",
    strip_paths: dict[str, str | None],
    captured_at: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 1500,
    use_cache: bool = True,
) -> dict[str, Any]:
    """One Opus call on up to four pavement strips.

    `strip_paths` is a dict with optional keys {"forward", "backward", "left",
    "right"}. Any key with a truthy value is attached; the others are omitted.
    Strips are always sent in the fixed order forward -> backward -> left ->
    right (with a text caption preceding each image so Opus can label
    bounding-box viewports correctly).

    Returns:
      {
        "model": str,
        "result": {"condition": str, "distresses": [...], "overall_note": str},
        "viewports_sent": list[str],
        "usage": {...},
        "cost_usd": float,
      }
    """
    from app.prompts.pavement import RATING_SYSTEM_PROMPT, format_rating_user_prompt

    ordered = ["forward", "backward", "left", "right"]
    present = [v for v in ordered if strip_paths.get(v)]
    if not present:
        raise ValueError("rate_pavement_async: no strip paths provided")

    content: list[dict[str, Any]] = []
    for vp in present:
        # label the viewport BEFORE the image so the caption binds to it
        content.append({"type": "text", "text": f"VIEWPORT: {vp}"})
        content.append(_image_block(strip_paths[vp]))
    content.append({
        "type": "text",
        "text": format_rating_user_prompt(captured_at, viewports_present=present),
    })

    system: Any
    if use_cache:
        system = [{"type": "text", "text": RATING_SYSTEM_PROMPT,
                   "cache_control": {"type": "ephemeral"}}]
    else:
        system = RATING_SYSTEM_PROMPT

    msg = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    parsed = _extract_json(text)
    usage = _usage_to_tokens(msg.usage)
    cost = estimate_cost_with_cache(usage, PRICE_INPUT_PER_MTOK, PRICE_OUTPUT_PER_MTOK)
    return {
        "model": model,
        "result": parsed,
        "viewports_sent": present,
        "usage": usage,
        "cost_usd": round(cost, 5),
    }
