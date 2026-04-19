from __future__ import annotations

from typing import Any, Literal, TypedDict

ActionType = Literal[
    "approve_refund",
    "deny_refund",
    "approve_return",
    "approve_exchange",
    "cancel_order",
    "provide_status_update",
    "ask_clarification",
    "provide_policy_info",
    "escalate_human",
]

PriorityType = Literal["low", "medium", "high", "urgent"]


class Decision(TypedDict):
    action: ActionType
    confidence: float
    needs_escalation: bool
    priority: PriorityType
    reasoning: str
    customer_message: str
    escalation_summary: str


class ToolCallRecord(TypedDict):
    timestamp: str
    ticket_id: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: dict[str, Any] | list[Any] | str | None
    confidence: float | None


class EligibilityResult(TypedDict, total=False):
    eligible: bool
    recommended_action: ActionType
    reasons: list[str]
    escalation_reasons: list[str]
    within_return_window: bool | None
    days_past_deadline: int | None
    warranty_active: bool
    detected_intent: str
    confidence_hint: float


class TicketState(TypedDict, total=False):
    ticket: dict[str, Any]
    order_id: str | None
    customer: dict[str, Any] | None
    order: dict[str, Any] | None
    product: dict[str, Any] | None
    knowledge_snippets: list[str]
    eligibility: EligibilityResult
    decision: Decision
    final_response: dict[str, Any]
    escalated: bool
    tool_call_count: int
    audit: list[ToolCallRecord]
    errors: list[str]
    dead_lettered: bool
    retry_counters: dict[str, int]
    reply_outbox: list[dict[str, Any]]
    escalation_queue: list[dict[str, Any]]
    last_refund: dict[str, Any]
