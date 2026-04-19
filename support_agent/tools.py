from __future__ import annotations

import calendar
import hashlib
import os
import re
import time
import uuid
from datetime import date, datetime
from typing import Any

from support_agent.audit import record_tool_call
from support_agent.data_store import DataStore
from support_agent.models import ActionType, EligibilityResult, TicketState

ORDER_ID_REGEX = re.compile(r"\bORD-\d{4}\b", re.IGNORECASE)
DEFAULT_RETRY_BUDGET = 2


class ToolTimeoutError(RuntimeError):
    pass


class ToolMalformedResponseError(RuntimeError):
    pass


class ToolPartialResponseError(RuntimeError):
    pass


class ToolExecutionError(RuntimeError):
    pass


def extract_order_id(text: str) -> str | None:
    match = ORDER_ID_REGEX.search(text)
    if not match:
        return None
    return match.group(0).upper()


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    return datetime.fromisoformat(value).date()


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _add_months(input_date: date, months: int) -> date:
    month_index = input_date.month - 1 + months
    year = input_date.year + month_index // 12
    month = month_index % 12 + 1
    day = min(input_date.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def detect_intent(subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()

    if "return policy" in text or "exchange" in text and "?" in text:
        return "policy_question"
    if "where is my order" in text or "tracking" in text or "went through" in text:
        return "order_status"
    if "cancel" in text:
        return "cancel_order"
    if "replacement" in text:
        return "replacement"
    if "refund" in text:
        return "refund"
    if "return" in text:
        return "return"
    if any(word in text for word in ["defect", "broken", "stopped", "cracked"]):
        return "defect"
    return "unknown"


class SupportTools:
    def __init__(self, store: DataStore, state: TicketState):
        self.store = store
        self.state = state

    def _append_error(self, message: str) -> None:
        if "errors" not in self.state:
            self.state["errors"] = []
        self.state["errors"].append(message)

    def _retry_budget(self) -> int:
        raw = os.getenv("TOOL_RETRY_BUDGET", str(DEFAULT_RETRY_BUDGET))
        try:
            return max(0, int(raw))
        except ValueError:
            return DEFAULT_RETRY_BUDGET

    def _tool_failure_scenario(self, tool_name: str, identifier: str, attempt: int) -> str | None:
        if attempt != 1:
            return None

        ticket_id = str(self.state.get("ticket", {}).get("ticket_id", "unknown"))
        forced_scenarios = {
            ("TKT-003", "GET_ORDER"): "timeout",
            ("TKT-014", "SEARCH_KNOWLEDGE_BASE"): "malformed",
            ("TKT-018", "CHECK_REFUND_ELIGIBILITY"): "partial",
        }
        forced = forced_scenarios.get((ticket_id, tool_name))
        if forced:
            return forced

        digest = hashlib.sha256(f"{ticket_id}|{tool_name}|{identifier}".encode("utf-8")).hexdigest()
        bucket = int(digest[:2], 16)

        if bucket < 5:
            return "timeout"
        if bucket < 9:
            return "malformed"
        if bucket < 12:
            return "partial"
        return None

    def _apply_failure_scenario(self, scenario: str | None, payload: Any, tool_name: str) -> Any:
        if scenario is None:
            return payload
        if scenario == "timeout":
            raise ToolTimeoutError(f"{tool_name} timed out")
        if scenario == "malformed":
            return {"error": "malformed", "tool": tool_name}
        if scenario == "partial":
            if isinstance(payload, dict):
                partial = dict(payload)
                if partial:
                    partial.pop(next(iter(partial)))
                return partial
            if isinstance(payload, list):
                return payload[:1]
            return None
        return payload

    def _validate_dict_payload(self, payload: Any, required_keys: set[str], tool_name: str) -> None:
        if not isinstance(payload, dict):
            raise ToolMalformedResponseError(f"{tool_name} returned non-dict payload")

        missing = required_keys - set(payload.keys())
        if missing:
            if payload:
                raise ToolPartialResponseError(
                    f"{tool_name} missing fields: {', '.join(sorted(missing))}"
                )
            raise ToolMalformedResponseError(f"{tool_name} returned empty payload")

    def _validate_customer_payload(self, payload: Any) -> None:
        self._validate_dict_payload(payload, {"customer_id", "email", "tier"}, "GET_CUSTOMER")

    def _validate_order_payload(self, payload: Any) -> None:
        self._validate_dict_payload(
            payload,
            {"order_id", "customer_id", "product_id", "status", "amount"},
            "GET_ORDER",
        )

    def _validate_product_payload(self, payload: Any) -> None:
        self._validate_dict_payload(
            payload,
            {"product_id", "category", "return_window_days", "warranty_months"},
            "GET_PRODUCT",
        )

    def _validate_knowledge_payload(self, payload: Any) -> None:
        if not isinstance(payload, list):
            raise ToolMalformedResponseError("SEARCH_KNOWLEDGE_BASE returned non-list payload")
        if payload and not all(isinstance(item, str) for item in payload):
            raise ToolPartialResponseError("SEARCH_KNOWLEDGE_BASE returned mixed item types")

    def _validate_eligibility_payload(self, payload: Any) -> None:
        self._validate_dict_payload(
            payload,
            {"eligible", "recommended_action", "reasons", "escalation_reasons", "confidence_hint"},
            "CHECK_REFUND_ELIGIBILITY",
        )

    def _execute_with_retry(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        identifier: str,
        operation: Any,
        validator: Any | None = None,
    ) -> Any:
        retry_budget = self._retry_budget()
        last_error: Exception | None = None

        retry_counters = self.state.setdefault("retry_counters", {})
        counter_key = f"{tool_name}:{identifier}"

        for attempt in range(1, retry_budget + 2):
            retry_counters[counter_key] = attempt
            scenario = self._tool_failure_scenario(tool_name, identifier, attempt)

            try:
                raw_result = operation()
                result = self._apply_failure_scenario(scenario, raw_result, tool_name)
                if validator is not None and result is not None:
                    validator(result)

                output_summary = {
                    "status": "success",
                    "attempt": attempt,
                    "scenario": scenario,
                    "result": result,
                }
                if isinstance(result, dict):
                    output_summary["keys"] = sorted(result.keys())
                elif isinstance(result, list):
                    output_summary["count"] = len(result)
                else:
                    output_summary["type"] = type(result).__name__

                record_tool_call(
                    self.state,
                    tool_name,
                    {**tool_input, "attempt": attempt},
                    output_summary,
                )
                return result

            except (ToolTimeoutError, ToolMalformedResponseError, ToolPartialResponseError) as exc:
                last_error = exc
                can_retry = attempt <= retry_budget
                delay_seconds = round(0.1 * (2 ** (attempt - 1)), 3)

                record_tool_call(
                    self.state,
                    f"{tool_name}_RETRY",
                    {**tool_input, "attempt": attempt},
                    {
                        "status": "retrying" if can_retry else "failed",
                        "error": str(exc),
                        "retry_budget": retry_budget,
                        "next_backoff_seconds": delay_seconds if can_retry else None,
                    },
                )

                self._append_error(f"{tool_name} attempt {attempt} failed: {exc}")

                if can_retry:
                    time.sleep(delay_seconds)
                    continue
                break

            except Exception as exc:
                last_error = exc
                record_tool_call(
                    self.state,
                    f"{tool_name}_ERROR",
                    {**tool_input, "attempt": attempt},
                    {
                        "status": "failed",
                        "error": str(exc),
                        "retryable": False,
                    },
                )
                self._append_error(f"{tool_name} failed with non-retryable error: {exc}")
                break

        raise ToolExecutionError(f"{tool_name} exhausted retries: {last_error}")

    def get_customer(self, email: str) -> dict[str, Any] | None:
        email_key = (email or "").strip().lower()
        if not email_key:
            return None

        def _op() -> dict[str, Any] | None:
            return self.store.get_customer_by_email(email_key)

        result = self._execute_with_retry(
            tool_name="GET_CUSTOMER",
            tool_input={"email": email_key},
            identifier=email_key,
            operation=_op,
            validator=self._validate_customer_payload,
        )
        return result

    def get_order(self, order_id: str | None) -> dict[str, Any] | None:
        if not order_id:
            return None

        normalized_order_id = order_id.upper()

        def _op() -> dict[str, Any] | None:
            return self.store.get_order_by_id(normalized_order_id)

        result = self._execute_with_retry(
            tool_name="GET_ORDER",
            tool_input={"order_id": normalized_order_id},
            identifier=normalized_order_id,
            operation=_op,
            validator=self._validate_order_payload,
        )
        return result

    def get_product(self, product_id: str | None) -> dict[str, Any] | None:
        if not product_id:
            return None

        normalized_product_id = product_id.upper()

        def _op() -> dict[str, Any] | None:
            return self.store.get_product_by_id(normalized_product_id)

        result = self._execute_with_retry(
            tool_name="GET_PRODUCT",
            tool_input={"product_id": normalized_product_id},
            identifier=normalized_product_id,
            operation=_op,
            validator=self._validate_product_payload,
        )
        return result

    def search_knowledge_base(self, query: str) -> list[str]:
        query_key = (query or "").strip()

        def _op() -> list[str]:
            return self.store.search_knowledge(query_key, top_k=3)

        result = self._execute_with_retry(
            tool_name="SEARCH_KNOWLEDGE_BASE",
            tool_input={"query": query_key},
            identifier=query_key[:80],
            operation=_op,
            validator=self._validate_knowledge_payload,
        )
        return result

    def lookup_user(self, email: str) -> dict[str, Any] | None:
        return self.get_customer(email)

    def lookup_order(
        self,
        order_id: str | None,
        customer_id: str | None,
    ) -> dict[str, Any] | None:
        if order_id:
            return self.get_order(order_id)
        if customer_id:
            latest = self.store.get_latest_order_for_customer(customer_id)
            if latest is None:
                return None
            return self.get_order(latest.get("order_id"))
        return None

    def lookup_product(self, product_id: str | None) -> dict[str, Any] | None:
        return self.get_product(product_id)

    def search_knowledge(self, query: str) -> list[str]:
        return self.search_knowledge_base(query)

    def check_refund_eligibility(
        self,
        order_id: str | None,
        ticket: dict[str, Any] | None = None,
        customer: dict[str, Any] | None = None,
        order: dict[str, Any] | None = None,
        product: dict[str, Any] | None = None,
    ) -> EligibilityResult:
        ticket_payload = ticket or self.state.get("ticket", {})
        customer_payload = customer or self.state.get("customer")
        order_payload = order or self.state.get("order")
        product_payload = product or self.state.get("product")

        if order_payload is None and order_id:
            order_payload = self.get_order(order_id)

        def _op() -> EligibilityResult:
            return self._compute_refund_eligibility(
                ticket=ticket_payload,
                customer=customer_payload,
                order=order_payload,
                product=product_payload,
            )

        result = self._execute_with_retry(
            tool_name="CHECK_REFUND_ELIGIBILITY",
            tool_input={
                "ticket_id": ticket_payload.get("ticket_id"),
                "order_id": order_id,
                "customer_id": customer_payload.get("customer_id") if customer_payload else None,
            },
            identifier=str(order_id or ticket_payload.get("ticket_id") or "unknown"),
            operation=_op,
            validator=self._validate_eligibility_payload,
        )

        return result

    def _compute_refund_eligibility(
        self,
        ticket: dict[str, Any],
        customer: dict[str, Any] | None,
        order: dict[str, Any] | None,
        product: dict[str, Any] | None,
    ) -> EligibilityResult:
        subject = ticket.get("subject", "")
        body = ticket.get("body", "")
        text = f"{subject} {body}".lower()

        result: EligibilityResult = {
            "eligible": False,
            "recommended_action": "ask_clarification",
            "reasons": [],
            "escalation_reasons": [],
            "within_return_window": None,
            "days_past_deadline": None,
            "warranty_active": False,
            "detected_intent": detect_intent(subject, body),
            "confidence_hint": 0.55,
        }

        if not customer:
            result["reasons"].append("Customer email is not found in records.")
            result["recommended_action"] = "ask_clarification"
            result["confidence_hint"] = 0.8
            return result

        if not order:
            result["reasons"].append("No matching order found. Ask for order ID and registered email.")
            result["recommended_action"] = "ask_clarification"
            result["confidence_hint"] = 0.78
            return result

        if order.get("customer_id") != customer.get("customer_id"):
            result["reasons"].append("Order ownership conflict between customer and order records.")
            result["escalation_reasons"].append("conflicting_data")
            result["recommended_action"] = "escalate_human"
            result["confidence_hint"] = 0.82
            return result

        created_at = _parse_iso_datetime(ticket.get("created_at"))
        created_date = created_at.date() if created_at else None
        return_deadline = _parse_iso_date(order.get("return_deadline"))

        if created_date and return_deadline:
            result["within_return_window"] = created_date <= return_deadline
            if created_date > return_deadline:
                result["days_past_deadline"] = (created_date - return_deadline).days

        if product and product.get("warranty_months", 0) > 0:
            delivery_date = _parse_iso_date(order.get("delivery_date"))
            if delivery_date and created_date:
                warranty_end = _add_months(delivery_date, int(product["warranty_months"]))
                result["warranty_active"] = created_date <= warranty_end

        if order.get("refund_status") == "refunded":
            result["reasons"].append("Refund is already processed for this order.")
            result["recommended_action"] = "provide_status_update"
            result["confidence_hint"] = 0.9
            return result

        claimed_premium = "premium member" in text
        tier = (customer.get("tier") or "standard").lower()
        if claimed_premium and tier not in {"premium", "vip"}:
            result["reasons"].append("Customer claimed premium tier not present in records.")
            result["escalation_reasons"].append("social_engineering")

        if "lawyer" in text or "dispute" in text or "chargeback" in text:
            result["escalation_reasons"].append("threatening_or_legal_language")

        if result["detected_intent"] == "policy_question":
            result["recommended_action"] = "provide_policy_info"
            result["confidence_hint"] = 0.87
            return result

        if result["detected_intent"] == "order_status":
            result["recommended_action"] = "provide_status_update"
            result["confidence_hint"] = 0.86
            return result

        if result["detected_intent"] == "cancel_order":
            status = (order.get("status") or "").lower()
            if status == "processing":
                result["eligible"] = True
                result["reasons"].append("Order is in processing and can be cancelled.")
                result["recommended_action"] = "cancel_order"
                result["confidence_hint"] = 0.9
            elif status == "shipped":
                result["reasons"].append("Order already shipped and cannot be cancelled.")
                result["recommended_action"] = "deny_refund"
                result["confidence_hint"] = 0.86
            else:
                result["reasons"].append("Delivered orders cannot be cancelled; return flow applies.")
                result["recommended_action"] = "deny_refund"
                result["confidence_hint"] = 0.82
            return result

        damaged_or_defect = any(
            phrase in text
            for phrase in ["damaged", "defect", "broken", "stopped working", "cracked"]
        )
        wrong_item = any(
            phrase in text
            for phrase in ["wrong item", "wrong size", "wrong colour", "wrong color"]
        )
        wants_replacement = "replacement" in text

        if wants_replacement and damaged_or_defect:
            result["reasons"].append("Customer requested replacement for damaged/defective product.")
            result["escalation_reasons"].append("replacement_requested")
            result["recommended_action"] = "escalate_human"
            result["confidence_hint"] = 0.88

        elif damaged_or_defect and result["within_return_window"]:
            result["eligible"] = True
            result["reasons"].append("Damaged or defective item is eligible for full refund.")
            result["recommended_action"] = "approve_refund"
            result["confidence_hint"] = 0.9

        elif wrong_item:
            result["eligible"] = True
            result["reasons"].append("Wrong item/variant delivered. Exchange or refund is eligible.")
            result["recommended_action"] = "approve_exchange"
            result["confidence_hint"] = 0.88

        elif result["within_return_window"]:
            result["eligible"] = True
            if result["detected_intent"] == "refund":
                result["recommended_action"] = "approve_refund"
            else:
                result["recommended_action"] = "approve_return"
            result["reasons"].append("Within return window for this order.")
            result["confidence_hint"] = 0.86

        else:
            customer_notes = (customer.get("notes") or "").lower()
            tier = (customer.get("tier") or "standard").lower()
            days_late = result.get("days_past_deadline") or 0

            if tier == "vip" and "extended return" in customer_notes:
                result["eligible"] = True
                result["recommended_action"] = "approve_return"
                result["reasons"].append("VIP extended return exception found in customer notes.")
                result["confidence_hint"] = 0.84

            elif tier == "premium" and 0 < days_late <= 3:
                result["reasons"].append("Premium customer slightly outside return window.")
                result["escalation_reasons"].append("supervisor_approval_required")
                result["recommended_action"] = "escalate_human"
                result["confidence_hint"] = 0.74

            elif damaged_or_defect and result["warranty_active"]:
                result["reasons"].append("Return window expired but warranty is active.")
                result["escalation_reasons"].append("warranty_claim")
                result["recommended_action"] = "escalate_human"
                result["confidence_hint"] = 0.85

            else:
                result["recommended_action"] = "deny_refund"
                result["reasons"].append("Return window expired and no policy exception applies.")
                result["confidence_hint"] = 0.82

        if order.get("amount", 0) and float(order["amount"]) > 200:
            result["escalation_reasons"].append("refund_amount_over_200")

        if order.get("notes") and "registered online" in str(order["notes"]).lower():
            result["reasons"].append("Item was registered online and is marked non-returnable.")
            result["recommended_action"] = "deny_refund"
            result["eligible"] = False
            result["confidence_hint"] = 0.9

        return result

    def issue_refund(self, order_id: str, amount: float) -> dict[str, Any]:
        eligibility = self.state.get("eligibility", {})
        if not eligibility.get("eligible"):
            raise ToolExecutionError("Refund blocked: eligibility is false")

        recommended_action = eligibility.get("recommended_action")
        if recommended_action != "approve_refund":
            raise ToolExecutionError(
                f"Refund blocked: recommended_action is {recommended_action}, not approve_refund"
            )

        def _op() -> dict[str, Any]:
            order = self.store.get_order_by_id(order_id)
            if not order:
                raise ToolExecutionError("Refund blocked: order not found")
            if order.get("refund_status") == "refunded":
                raise ToolExecutionError("Refund already issued previously")

            order["refund_status"] = "refunded"
            receipt = {
                "refund_id": f"RF-{uuid.uuid4().hex[:10].upper()}",
                "order_id": order_id,
                "amount": round(float(amount), 2),
                "status": "processed",
            }
            self.state["last_refund"] = receipt
            return receipt

        result = self._execute_with_retry(
            tool_name="ISSUE_REFUND",
            tool_input={"order_id": order_id, "amount": round(float(amount), 2)},
            identifier=order_id,
            operation=_op,
            validator=lambda payload: self._validate_dict_payload(
                payload,
                {"refund_id", "order_id", "amount", "status"},
                "ISSUE_REFUND",
            ),
        )
        return result

    def send_reply(self, ticket_id: str, message: str) -> dict[str, Any]:
        payload_message = (message or "").strip()

        def _op() -> dict[str, Any]:
            if not payload_message:
                raise ToolExecutionError("Cannot send empty reply")
            event = {
                "ticket_id": ticket_id,
                "message_id": f"MSG-{uuid.uuid4().hex[:10].upper()}",
                "status": "sent",
                "message_preview": payload_message[:120],
            }
            outbox = self.state.setdefault("reply_outbox", [])
            outbox.append(event)
            return event

        result = self._execute_with_retry(
            tool_name="SEND_REPLY",
            tool_input={"ticket_id": ticket_id},
            identifier=ticket_id,
            operation=_op,
            validator=lambda payload: self._validate_dict_payload(
                payload,
                {"ticket_id", "message_id", "status", "message_preview"},
                "SEND_REPLY",
            ),
        )
        return result

    def escalate(self, ticket_id: str, summary: str, priority: str) -> dict[str, Any]:
        escalation_summary = summary.strip() or "No summary provided."

        def _op() -> dict[str, Any]:
            event = {
                "ticket_id": ticket_id,
                "escalation_id": f"ESC-{uuid.uuid4().hex[:10].upper()}",
                "priority": priority,
                "summary": escalation_summary,
                "status": "queued_for_human",
            }
            queue = self.state.setdefault("escalation_queue", [])
            queue.append(event)
            return event

        result = self._execute_with_retry(
            tool_name="ESCALATE",
            tool_input={"ticket_id": ticket_id, "priority": priority},
            identifier=ticket_id,
            operation=_op,
            validator=lambda payload: self._validate_dict_payload(
                payload,
                {"ticket_id", "escalation_id", "priority", "summary", "status"},
                "ESCALATE",
            ),
        )
        return result


def normalize_action(action: str, fallback: ActionType) -> ActionType:
    valid_actions: set[str] = {
        "approve_refund",
        "deny_refund",
        "approve_return",
        "approve_exchange",
        "cancel_order",
        "provide_status_update",
        "ask_clarification",
        "provide_policy_info",
        "escalate_human",
    }
    if action in valid_actions:
        return action  # type: ignore[return-value]
    return fallback
