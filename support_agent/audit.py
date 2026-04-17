from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from support_agent.models import TicketState, ToolCallRecord


MAX_AUDIT_FIELD_CHARS = 2000


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(value: Any) -> Any:
    """Keep audit payloads compact and always JSON-safe."""
    try:
        serialized = json.dumps(value, default=str)
    except TypeError:
        return str(value)[:MAX_AUDIT_FIELD_CHARS]

    if len(serialized) <= MAX_AUDIT_FIELD_CHARS:
        return json.loads(serialized)

    return {
        "_truncated": True,
        "preview": serialized[:MAX_AUDIT_FIELD_CHARS],
    }


def record_tool_call(
    state: TicketState,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Any,
    confidence: float | None = None,
) -> None:
    if "audit" not in state:
        state["audit"] = []
    if "tool_call_count" not in state:
        state["tool_call_count"] = 0

    ticket_id = state.get("ticket", {}).get("ticket_id", "unknown-ticket")

    entry: ToolCallRecord = {
        "timestamp": utc_now_iso(),
        "ticket_id": ticket_id,
        "tool_name": tool_name,
        "tool_input": _truncate(tool_input),
        "tool_output": _truncate(tool_output),
        "confidence": confidence,
    }

    state["audit"].append(entry)
    state["tool_call_count"] += 1


def apply_confidence_to_audit(state: TicketState, confidence: float) -> None:
    if "audit" not in state:
        return
    for entry in state["audit"]:
        if entry.get("confidence") is None:
            entry["confidence"] = confidence
