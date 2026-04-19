# Failure Modes

This document lists concrete failure scenarios and how the current system handles each one.

## 1) Tool timeout during lookup

- Trigger: A read tool call times out, for example GET_ORDER first attempt.
- Detection: ToolTimeoutError inside the SupportTools retry wrapper.
- Handling:
  - Emits retry audit events such as GET_ORDER_RETRY.
  - Applies exponential backoff before retry.
  - Retries up to TOOL_RETRY_BUDGET.
  - If retries still fail, raises ToolExecutionError to graph node.
- Outcome: Ticket does not crash the batch; state captures errors and continues through safe graph behavior.

## 2) Malformed or partial tool payload

- Trigger: Tool returns wrong schema or missing required fields, for example SEARCH_KNOWLEDGE_BASE malformed list payload or CHECK_REFUND_ELIGIBILITY partial dict.
- Detection: Validator raises ToolMalformedResponseError or ToolPartialResponseError.
- Handling:
  - Wrapper logs retry metadata and error context in audit.
  - Retries within budget.
  - For eligibility failure after retries, graph writes CHECK_REFUND_ELIGIBILITY_FALLBACK and forces safe values:
    - recommended_action = escalate_human
    - confidence_hint = 0.4
- Outcome: Uncertain automation is prevented; system routes to human-safe escalation path.

## 3) Refund execution failure on resolve path

- Trigger: Decision chooses approve_refund, but ISSUE_REFUND fails (missing order id, duplicate refund, or tool retry exhaustion).
- Detection: ToolExecutionError in resolve_node.
- Handling:
  - Converts action to escalate_human.
  - Sets priority high and builds structured escalation summary.
  - Attempts explicit ESCALATE call.
  - Marks state dead_lettered = true.
- Outcome: No unsafe or silent write failure; ticket remains visible for manual intervention.

## 4) Reply dispatch failure

- Trigger: SEND_REPLY fails on resolve or escalate path.
- Detection: ToolExecutionError in resolve_node or escalate_node.
- Handling:
  - Adds detailed error to state.errors.
  - On resolve path, converts to escalation if not already escalated.
  - Marks dead_lettered when dispatch cannot be completed safely.
- Outcome: Communication failure becomes operationally visible and is not lost.

## 5) Unexpected graph/pipeline exception

- Trigger: Unhandled exception while invoking graph for a ticket.
- Detection: process_ticket top-level try/except in main.py.
- Handling:
  - Writes safe fallback decision with escalate_human action.
  - Sets escalated = true and dead_lettered = true.
  - Records PIPELINE_ERROR audit event.
- Outcome: One bad ticket cannot terminate the entire run; concurrency and batch completion are preserved.

## 6) Background API job failure

- Trigger: Exception during JobStore worker execution in api_server.py.
- Detection: Exception in _run_job.
- Handling:
  - Logs stack trace.
  - Writes error artifact at outputs/jobs/{job_id}/artifacts/error.json.
  - Marks job status failed and stores error on job record.
- Outcome: API remains available; failed jobs are traceable and diagnosable through job detail and artifacts.

## Operational evidence and recovery

- Audit evidence: audit_log.json captures tool attempts, retries, errors, and confidence.
- Escalation evidence: escalations.json captures cases requiring human specialist review.
- Dead-letter evidence: dead_letter_queue.json captures tickets with unrecoverable write/action failures.
- Run-level guardrails: summary.json provides dead-letter count, escalation count, and minimum tool-call metrics.
