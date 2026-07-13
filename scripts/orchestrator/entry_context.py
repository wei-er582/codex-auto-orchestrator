from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .speed_profiles import canonical_service_tier


def discover_entry_context(
    *,
    thread_id: str | None = None,
    session_root: Path | None = None,
    model: str | None = None,
    reasoning: str | None = None,
    service_tier: str | None = None,
) -> dict[str, Any]:
    selected_thread = thread_id or os.environ.get("CODEX_THREAD_ID", "")
    context: dict[str, Any] = {
        "thread_id": selected_thread,
        "model": model or "unknown",
        "reasoning": reasoning or "unknown",
        "service_tier": canonical_service_tier(service_tier or "default"),
        "service_tier_source": "argument" if service_tier else "default-unavailable",
        "source": "arguments" if any((model, reasoning, service_tier)) else "unavailable",
    }
    if not selected_thread:
        return context
    root = session_root or (Path.home() / ".codex" / "sessions")
    if not root.is_dir():
        return context
    matches = sorted(root.glob(f"*/*/*/*{selected_thread}.jsonl"), reverse=True)
    if not matches:
        return context
    settings: dict[str, Any] = {}
    session_tier_available = False
    try:
        with matches[0].open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                if event.get("type") == "turn_context" and isinstance(payload, dict):
                    turn_model = payload.get("model")
                    turn_reasoning = payload.get("effort")
                    turn_tier = payload.get("service_tier")
                    if isinstance(turn_model, str) and turn_model:
                        settings["model"] = turn_model
                    if isinstance(turn_reasoning, str) and turn_reasoning:
                        settings["reasoning_effort"] = turn_reasoning
                    if isinstance(turn_tier, str) and turn_tier:
                        settings["service_tier"] = turn_tier
                        session_tier_available = True
                elif (
                    event.get("type") == "event_msg"
                    and isinstance(payload, dict)
                    and payload.get("type") == "thread_settings_applied"
                    and isinstance(payload.get("thread_settings"), dict)
                ):
                    applied = payload["thread_settings"]
                    for key in ("model", "reasoning_effort", "service_tier"):
                        value = applied.get(key)
                        if isinstance(value, str) and value:
                            settings[key] = value
                    session_tier_available = bool(settings.get("service_tier"))
    except OSError:
        return context
    if settings:
        context.update(
            {
                "model": model or str(settings.get("model", "unknown")),
                "reasoning": reasoning or str(settings.get("reasoning_effort", "unknown")),
                "service_tier": canonical_service_tier(
                    service_tier or settings.get("service_tier", "default")
                ),
                "service_tier_source": (
                    "argument"
                    if service_tier
                    else ("session" if session_tier_available else "default-unavailable")
                ),
                "source": str(matches[0]),
            }
        )
    return context
