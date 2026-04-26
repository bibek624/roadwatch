"""Per-category hazard-detection prompts. Each module exports
SYSTEM_PROMPT and USER_PROMPT_TEMPLATE."""
from importlib import import_module

AVAILABLE = {"near_miss", "triage"}


def load(category: str):
    if category not in AVAILABLE:
        raise ValueError(
            f"unknown category '{category}'. available: {sorted(AVAILABLE)}"
        )
    return import_module(f"app.prompts.{category}")
