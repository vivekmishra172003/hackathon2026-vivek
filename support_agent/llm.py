from __future__ import annotations

import asyncio
import json
from typing import Any

try:
    import google.generativeai as genai
except ImportError:  # pragma: no cover
    genai = None

from support_agent.models import Decision, TicketState
from support_agent.tools import normalize_action


class GeminiDecider:
    def __init__(self, api_key: str | None, model_name: str, temperature: float = 0.1):
        self.model_name = model_name
        self.temperature = temperature
        self.enabled = bool(api_key) and genai is not None
        self._model = None

        if self.enabled and api_key and genai is not None:
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel(model_name)

    async def decide(self, state: TicketState) -> Decision:
        heuristic = self._heuristic_decision(state)

        if not self.enabled or self._model is None:
            return heuristic

        prompt = self._build_prompt(state)

        try:
            response = await asyncio.to_thread(
                self._model.generate_content,
                prompt,
                generation_config={"temperature": self.temperature},
            )
            parsed = self._parse_json_response(getattr(response, "text", ""))
            if not parsed:
                return heuristic
            return self._normalize_decision(parsed, heuristic)
        except Exception:
            return heuristic

    def _build_prompt(self, state: TicketState) -> str:
        context = {
            "ticket": state.get("ticket"),
            "customer": state.get("customer"),
            "order": state.get("order"),
            "product": state.get("product"),
            "knowledge_snippets": state.get("knowledge_snippets", []),
            "eligibility": state.get("eligibility", {}),
        }

        return (
            "You are a customer support triage agent. Respond with JSON only.\n"
            "Use policy-aligned reasoning and keep customer response concise and empathetic.\n"
            "If confidence is below 0.65, set needs_escalation=true.\n"
            "Escalate for warranty claims, replacement requests, fraud/social engineering, "
            "conflicting data, or refund amount > 200.\n"
            "Output schema:\n"
            "{\n"
            '  "action": "approve_refund|deny_refund|approve_return|approve_exchange|cancel_order|'
            'provide_status_update|ask_clarification|provide_policy_info|escalate_human",\n'
            '  "confidence": 0.0,\n'
            '  "needs_escalation": false,\n'
            '  "priority": "low|medium|high|urgent",\n'
            '  "reasoning": "short reasoning",\n'
            '  "customer_message": "final response to customer",\n'
            '  "escalation_summary": "summary for human agent if escalated"\n'
            "}\n"
            f"Context:\n{json.dumps(context, indent=2, default=str)}"
        )

    def _parse_json_response(self, text: str) -> dict[str, Any] | None:
        cleaned = text.strip()
        if not cleaned:
            return None

        if cleaned.startswith("```"):
            lines = [line for line in cleaned.splitlines() if not line.startswith("```")]
            cleaned = "\n".join(lines).strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return None
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return None

    def _heuristic_decision(self, state: TicketState) -> Decision:
        eligibility = state.get("eligibility", {})
        fallback_action = eligibility.get("recommended_action", "ask_clarification")
        action = normalize_action(fallback_action, "ask_clarification")

        escalation_reasons = eligibility.get("escalation_reasons", [])
        confidence = float(eligibility.get("confidence_hint", 0.68))

        needs_escalation = bool(escalation_reasons) or action == "escalate_human"
        if confidence < 0.65:
            needs_escalation = True

        priority = "medium"
        if "social_engineering" in escalation_reasons or "threatening_or_legal_language" in escalation_reasons:
            priority = "urgent"
        elif "warranty_claim" in escalation_reasons or "refund_amount_over_200" in escalation_reasons:
            priority = "high"

        customer_messages = {
            "approve_refund": "Your request qualifies for a refund. I will process it now, and it should reflect in 5-7 business days.",
            "deny_refund": "I reviewed your request and this order is outside the return policy, so I cannot approve a refund right now.",
            "approve_return": "Your return request is approved. I can help you start the return process immediately.",
            "approve_exchange": "You are eligible for an exchange. I can arrange replacement with the correct item/variant.",
            "cancel_order": "Your order is eligible for cancellation and I will process it right away.",
            "provide_status_update": "I checked the order status and shared the latest update below.",
            "ask_clarification": "I can help with this. Please share your order ID and a few more details so I can proceed.",
            "provide_policy_info": "Here are the return and exchange policy details relevant to your question.",
            "escalate_human": "I am escalating this to a specialist team so they can handle this case accurately and quickly.",
        }

        reasons = eligibility.get("reasons", [])
        reasoning = reasons[0] if reasons else "Decision based on policy checks and available order data."

        return {
            "action": action,
            "confidence": max(0.0, min(1.0, confidence)),
            "needs_escalation": needs_escalation,
            "priority": priority,
            "reasoning": reasoning,
            "customer_message": customer_messages.get(action, customer_messages["ask_clarification"]),
            "escalation_summary": "; ".join(escalation_reasons) if escalation_reasons else "",
        }

    def _normalize_decision(self, raw: dict[str, Any], fallback: Decision) -> Decision:
        action = normalize_action(str(raw.get("action", "")), fallback["action"])
        confidence = fallback["confidence"]

        try:
            confidence = float(raw.get("confidence", confidence))
        except (TypeError, ValueError):
            confidence = fallback["confidence"]

        confidence = max(0.0, min(1.0, confidence))
        needs_escalation = bool(raw.get("needs_escalation", False)) or action == "escalate_human"
        if confidence < 0.65:
            needs_escalation = True

        priority = str(raw.get("priority", fallback["priority"])).lower()
        if priority not in {"low", "medium", "high", "urgent"}:
            priority = fallback["priority"]

        return {
            "action": action,
            "confidence": confidence,
            "needs_escalation": needs_escalation,
            "priority": priority,
            "reasoning": str(raw.get("reasoning", fallback["reasoning"])),
            "customer_message": str(raw.get("customer_message", fallback["customer_message"])),
            "escalation_summary": str(raw.get("escalation_summary", fallback["escalation_summary"])),
        }
