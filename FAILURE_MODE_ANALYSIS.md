# Failure Mode Analysis

This document describes concrete failure scenarios and how the agent responds without crashing or losing tickets.

## Scenario 1: Tool Timeout During Lookup

- Trigger: `get_order(order_id)` times out on first attempt (simulated fault injection).
- Detection: timeout exception captured by retry wrapper.
- Response:
  - emits `GET_ORDER_RETRY` audit event with error and backoff metadata
  - retries with exponential backoff up to budget
  - on success, pipeline continues normally
  - on retry exhaustion, graph records error and proceeds with safe fallback state
- User impact: customer still receives a deterministic response or escalation, never a hard failure.

## Scenario 2: Malformed/Partial Tool Output

- Trigger: `search_knowledge_base(query)` or `check_refund_eligibility(order_id)` returns malformed or partial payload.
- Detection: schema validation fails (`ToolMalformedResponseError` or `ToolPartialResponseError`).
- Response:
  - audit logs include retry attempt and validation error details
  - retries are attempted within budget
  - if eligibility tool still fails, fallback eligibility forces safe escalation (`recommended_action=escalate_human`)
- User impact: uncertain decisions are escalated with context instead of producing unsafe automation.

## Scenario 3: Irreversible Action Failure (Refund Path)

- Trigger: refund execution fails after decision says `approve_refund` (e.g., missing order, tool failure, retry exhaustion).
- Detection: `issue_refund(order_id, amount)` raises `ToolExecutionError`.
- Response:
  - ticket is automatically converted to escalation flow with structured summary
  - `dead_lettered=true` is set for operator follow-up
  - errors are persisted to `dead_letter_queue.json`
- User impact: no silent failure and no duplicate/unsafe refund behavior.

## Scenario 4: Reply Dispatch Failure

- Trigger: `send_reply(ticket_id, message)` fails after retries.
- Detection: write action raises `ToolExecutionError`.
- Response:
  - error captured in ticket state and audit log
  - ticket is escalated to human if not already escalated
  - ticket is marked dead-lettered for guaranteed operational visibility
- User impact: communication failure is surfaced for manual intervention.

## Scenario 5: Graph/Pipeline Exception

- Trigger: unexpected exception in graph execution.
- Detection: top-level exception handler in `process_ticket`.
- Response:
  - ticket receives safe fallback decision (`escalate_human`)
  - `PIPELINE_ERROR` audit event recorded
  - ticket marked dead-lettered with explicit error
- User impact: run completes for all tickets; one bad ticket cannot crash the batch.

## Operational Guarantees

- Batch processing remains concurrent via asyncio even when individual tickets fail.
- Failed tickets are preserved in `dead_letter_queue.json`.
- Every retry/fallback/escalation path is explainable through `audit_log.json`.
