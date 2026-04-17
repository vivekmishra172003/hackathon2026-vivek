from __future__ import annotations

import calendar
import re
from datetime import date, datetime
from typing import Any

from support_agent.audit import record_tool_call
from support_agent.data_store import DataStore
from support_agent.models import ActionType, EligibilityResult, TicketState

ORDER_ID_REGEX = re.compile(r"\bORD-\d{4}\b", re.IGNORECASE)


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

    def lookup_user(self, email: str) -> dict[str, Any] | None:
        customer = self.store.get_customer_by_email(email)
        record_tool_call(
            self.state,
            "LOOKUP_USER",
            {"email": email},
            {
                "found": customer is not None,
                "customer_id": customer.get("customer_id") if customer else None,
                "tier": customer.get("tier") if customer else None,
            },
        )
        return customer

    def lookup_order(
        self,
        order_id: str | None,
        customer_id: str | None,
    ) -> dict[str, Any] | None:
        order = None

        if order_id:
            order = self.store.get_order_by_id(order_id)
        elif customer_id:
            order = self.store.get_latest_order_for_customer(customer_id)

        record_tool_call(
            self.state,
            "LOOKUP_ORDER",
            {"order_id": order_id, "customer_id": customer_id},
            {
                "found": order is not None,
                "order_id": order.get("order_id") if order else None,
                "status": order.get("status") if order else None,
                "amount": order.get("amount") if order else None,
            },
        )
        return order

    def lookup_product(self, product_id: str | None) -> dict[str, Any] | None:
        product = self.store.get_product_by_id(product_id) if product_id else None
        record_tool_call(
            self.state,
            "LOOKUP_PRODUCT",
            {"product_id": product_id},
            {
                "found": product is not None,
                "product_id": product.get("product_id") if product else None,
                "category": product.get("category") if product else None,
                "return_window_days": product.get("return_window_days") if product else None,
                "warranty_months": product.get("warranty_months") if product else None,
            },
        )
        return product

    def search_knowledge(self, query: str) -> list[str]:
        snippets = self.store.search_knowledge(query, top_k=3)
        record_tool_call(
            self.state,
            "SEARCH_KNOWLEDGE",
            {"query": query},
            {
                "matches": len(snippets),
                "preview": snippets[:1],
            },
        )
        return snippets

    def check_refund_eligibility(
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
            record_tool_call(
                self.state,
                "CHECK_REFUND_ELIGIBILITY",
                {"ticket_id": ticket.get("ticket_id")},
                result,
            )
            return result

        if not order:
            result["reasons"].append("No matching order found. Ask for order ID and registered email.")
            result["recommended_action"] = "ask_clarification"
            result["confidence_hint"] = 0.78
            record_tool_call(
                self.state,
                "CHECK_REFUND_ELIGIBILITY",
                {"ticket_id": ticket.get("ticket_id"), "customer_id": customer.get("customer_id")},
                result,
            )
            return result

        if order.get("customer_id") != customer.get("customer_id"):
            result["reasons"].append("Order ownership conflict between customer and order records.")
            result["escalation_reasons"].append("conflicting_data")
            result["recommended_action"] = "escalate_human"
            result["confidence_hint"] = 0.82
            record_tool_call(
                self.state,
                "CHECK_REFUND_ELIGIBILITY",
                {"ticket_id": ticket.get("ticket_id"), "order_id": order.get("order_id")},
                result,
            )
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
            record_tool_call(
                self.state,
                "CHECK_REFUND_ELIGIBILITY",
                {"ticket_id": ticket.get("ticket_id"), "order_id": order.get("order_id")},
                result,
            )
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
            record_tool_call(
                self.state,
                "CHECK_REFUND_ELIGIBILITY",
                {"ticket_id": ticket.get("ticket_id"), "order_id": order.get("order_id")},
                result,
            )
            return result

        if result["detected_intent"] == "order_status":
            result["recommended_action"] = "provide_status_update"
            result["confidence_hint"] = 0.86
            record_tool_call(
                self.state,
                "CHECK_REFUND_ELIGIBILITY",
                {"ticket_id": ticket.get("ticket_id"), "order_id": order.get("order_id")},
                result,
            )
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
            record_tool_call(
                self.state,
                "CHECK_REFUND_ELIGIBILITY",
                {"ticket_id": ticket.get("ticket_id"), "order_id": order.get("order_id")},
                result,
            )
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

        record_tool_call(
            self.state,
            "CHECK_REFUND_ELIGIBILITY",
            {
                "ticket_id": ticket.get("ticket_id"),
                "order_id": order.get("order_id"),
                "product_id": product.get("product_id") if product else None,
            },
            result,
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
