from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from support_agent.audit import apply_confidence_to_audit, record_tool_call
from support_agent.data_store import DataStore
from support_agent.llm import GeminiDecider
from support_agent.models import TicketState
from support_agent.tools import SupportTools, ToolExecutionError, extract_order_id


def build_support_graph(
    store: DataStore,
    decider: GeminiDecider,
    confidence_threshold: float = 0.65,
):
    def append_error(state: TicketState, message: str) -> None:
        if "errors" not in state:
            state["errors"] = []
        state["errors"].append(message)

    def build_escalation_summary(state: TicketState) -> str:
        ticket = state.get("ticket", {})
        decision = state.get("decision", {})
        eligibility = state.get("eligibility", {})
        order = state.get("order") or {}
        product = state.get("product") or {}
        reasons = "; ".join(eligibility.get("reasons", []))
        escalation_reasons = "; ".join(eligibility.get("escalation_reasons", []))

        return (
            f"ticket_id={ticket.get('ticket_id')}; "
            f"customer_email={ticket.get('customer_email')}; "
            f"order_id={order.get('order_id')}; "
            f"product_id={product.get('product_id')}; "
            f"recommended_action={eligibility.get('recommended_action')}; "
            f"decision_action={decision.get('action')}; "
            f"confidence={decision.get('confidence')}; "
            f"policy_reasons={reasons}; "
            f"escalation_reasons={escalation_reasons}"
        )

    def parse_ticket_node(state: TicketState) -> TicketState:
        ticket = state.get("ticket", {})
        subject = ticket.get("subject", "")
        body = ticket.get("body", "")
        order_id = extract_order_id(f"{subject} {body}")
        state["order_id"] = order_id

        record_tool_call(
            state,
            "PARSE_TICKET",
            {"ticket_id": ticket.get("ticket_id")},
            {
                "order_id": order_id,
                "customer_email": ticket.get("customer_email"),
            },
        )
        return state

    def lookup_user_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        email = state.get("ticket", {}).get("customer_email", "")
        try:
            state["customer"] = tools.get_customer(email)
        except ToolExecutionError as exc:
            append_error(state, f"GET_CUSTOMER failed: {exc}")
            state["customer"] = None
        return state

    def lookup_order_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        customer = state.get("customer")
        customer_id = customer.get("customer_id") if customer else None
        try:
            state["order"] = tools.lookup_order(state.get("order_id"), customer_id)
        except ToolExecutionError as exc:
            append_error(state, f"GET_ORDER failed: {exc}")
            state["order"] = None
        return state

    def lookup_product_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        order = state.get("order")
        product_id = order.get("product_id") if order else None
        try:
            state["product"] = tools.get_product(product_id)
        except ToolExecutionError as exc:
            append_error(state, f"GET_PRODUCT failed: {exc}")
            state["product"] = None
        return state

    def knowledge_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        ticket = state.get("ticket", {})
        query = f"{ticket.get('subject', '')} {ticket.get('body', '')}"
        try:
            state["knowledge_snippets"] = tools.search_knowledge_base(query)
        except ToolExecutionError as exc:
            append_error(state, f"SEARCH_KNOWLEDGE_BASE failed: {exc}")
            state["knowledge_snippets"] = []
        return state

    def eligibility_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        try:
            state["eligibility"] = tools.check_refund_eligibility(
                state.get("order_id"),
                ticket=state.get("ticket", {}),
                customer=state.get("customer"),
                order=state.get("order"),
                product=state.get("product"),
            )
        except ToolExecutionError as exc:
            append_error(state, f"CHECK_REFUND_ELIGIBILITY failed: {exc}")
            state["eligibility"] = {
                "eligible": False,
                "recommended_action": "escalate_human",
                "reasons": ["Eligibility tool failed after retries."],
                "escalation_reasons": ["eligibility_tool_failure"],
                "confidence_hint": 0.4,
            }
            record_tool_call(
                state,
                "CHECK_REFUND_ELIGIBILITY_FALLBACK",
                {"ticket_id": state.get("ticket", {}).get("ticket_id")},
                state["eligibility"],
                confidence=0.4,
            )
        return state

    async def decide_node(state: TicketState) -> TicketState:
        decision = await decider.decide(state)
        state["decision"] = decision

        record_tool_call(
            state,
            "DECIDE",
            {
                "ticket_id": state.get("ticket", {}).get("ticket_id"),
                "recommended_action": state.get("eligibility", {}).get("recommended_action"),
            },
            decision,
            confidence=decision.get("confidence"),
        )
        return state

    def resolve_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        ticket = state.get("ticket", {})
        decision = state.get("decision", {})
        order = state.get("order") or {}

        ticket_id = ticket.get("ticket_id")
        action = decision.get("action")
        confidence = decision.get("confidence", 0.0)
        priority = decision.get("priority", "medium")
        message = decision.get("customer_message", "")

        action_events: dict[str, object] = {}
        dead_lettered = False
        escalated = False
        escalation_summary = ""

        if action == "approve_refund":
            order_id = order.get("order_id") or state.get("order_id")
            amount = float(order.get("amount") or 0.0)
            try:
                if order_id:
                    action_events["refund"] = tools.issue_refund(str(order_id), amount)
                else:
                    raise ToolExecutionError("Missing order_id for refund")
            except ToolExecutionError as exc:
                append_error(state, f"ISSUE_REFUND failed: {exc}")
                dead_lettered = True
                escalated = True
                action = "escalate_human"
                priority = "high"
                escalation_summary = (
                    f"Refund execution failed after retries for ticket {ticket_id}: {exc}"
                )
                try:
                    action_events["escalation"] = tools.escalate(
                        str(ticket_id),
                        escalation_summary,
                        str(priority),
                    )
                except ToolExecutionError as escalate_exc:
                    append_error(state, f"ESCALATE fallback failed: {escalate_exc}")

        try:
            action_events["reply"] = tools.send_reply(str(ticket_id), str(message))
        except ToolExecutionError as exc:
            append_error(state, f"SEND_REPLY failed: {exc}")
            dead_lettered = True
            if not escalated:
                escalated = True
                action = "escalate_human"
                priority = "high"
                escalation_summary = f"Customer reply dispatch failed for ticket {ticket_id}: {exc}"
                try:
                    action_events["escalation"] = tools.escalate(
                        str(ticket_id),
                        escalation_summary,
                        str(priority),
                    )
                except ToolExecutionError as escalate_exc:
                    append_error(state, f"ESCALATE fallback failed: {escalate_exc}")

        state["escalated"] = escalated
        state["dead_lettered"] = dead_lettered

        if escalated:
            status = "escalated"
        else:
            status = "resolved"

        state["final_response"] = {
            "ticket_id": ticket.get("ticket_id"),
            "status": status,
            "action": action,
            "priority": priority,
            "confidence": confidence,
            "message": message,
            "reasoning": decision.get("reasoning", ""),
            "escalation_summary": escalation_summary,
            "action_events": action_events,
        }
        return state

    def escalate_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        ticket = state.get("ticket", {})
        decision = state.get("decision", {})
        summary = decision.get("escalation_summary") or build_escalation_summary(state)
        priority = decision.get("priority", "high")

        action_events: dict[str, object] = {}
        dead_lettered = False

        try:
            action_events["escalation"] = tools.escalate(
                str(ticket.get("ticket_id")),
                str(summary),
                str(priority),
            )
        except ToolExecutionError as exc:
            append_error(state, f"ESCALATE failed: {exc}")
            dead_lettered = True

        customer_message = decision.get(
            "customer_message",
            "Your request is being reviewed by a specialist team.",
        )
        try:
            action_events["reply"] = tools.send_reply(
                str(ticket.get("ticket_id")),
                str(customer_message),
            )
        except ToolExecutionError as exc:
            append_error(state, f"SEND_REPLY failed on escalation path: {exc}")
            dead_lettered = True

        state["escalated"] = True
        state["dead_lettered"] = dead_lettered
        state["final_response"] = {
            "ticket_id": ticket.get("ticket_id"),
            "status": "escalated",
            "action": "escalate_human",
            "priority": priority,
            "confidence": decision.get("confidence", 0.0),
            "message": customer_message,
            "reasoning": decision.get("reasoning", ""),
            "escalation_summary": summary,
            "recommended_path": "human_specialist_review",
            "action_events": action_events,
        }
        return state

    def finalize_node(state: TicketState) -> TicketState:
        decision_confidence = float(state.get("decision", {}).get("confidence", 0.0))
        apply_confidence_to_audit(state, decision_confidence)

        if state.get("tool_call_count", 0) < 3:
            record_tool_call(
                state,
                "MIN_TOOL_CALL_GUARD",
                {"previous_count": state.get("tool_call_count", 0)},
                {"status": "Tool-count guard triggered"},
                confidence=decision_confidence,
            )

        if state.get("dead_lettered", False):
            record_tool_call(
                state,
                "DEAD_LETTER_MARKED",
                {"ticket_id": state.get("ticket", {}).get("ticket_id")},
                {
                    "reason": "One or more write actions failed after retries",
                    "errors": state.get("errors", []),
                },
                confidence=decision_confidence,
            )

        return state

    def route_after_decide(state: TicketState) -> str:
        decision = state.get("decision", {})
        confidence = float(decision.get("confidence", 0.0))
        if decision.get("needs_escalation") or confidence < confidence_threshold:
            return "escalate"
        return "resolve"

    graph_builder = StateGraph(TicketState)

    graph_builder.add_node("parse_ticket", parse_ticket_node)
    graph_builder.add_node("lookup_user", lookup_user_node)
    graph_builder.add_node("lookup_order", lookup_order_node)
    graph_builder.add_node("lookup_product", lookup_product_node)
    graph_builder.add_node("search_knowledge", knowledge_node)
    graph_builder.add_node("check_refund_eligibility", eligibility_node)
    graph_builder.add_node("decide", decide_node)
    graph_builder.add_node("resolve", resolve_node)
    graph_builder.add_node("escalate", escalate_node)
    graph_builder.add_node("finalize", finalize_node)

    graph_builder.add_edge(START, "parse_ticket")
    graph_builder.add_edge("parse_ticket", "lookup_user")
    graph_builder.add_edge("lookup_user", "lookup_order")
    graph_builder.add_edge("lookup_order", "lookup_product")
    graph_builder.add_edge("lookup_product", "search_knowledge")
    graph_builder.add_edge("search_knowledge", "check_refund_eligibility")
    graph_builder.add_edge("check_refund_eligibility", "decide")

    graph_builder.add_conditional_edges(
        "decide",
        route_after_decide,
        {
            "resolve": "resolve",
            "escalate": "escalate",
        },
    )

    graph_builder.add_edge("resolve", "finalize")
    graph_builder.add_edge("escalate", "finalize")
    graph_builder.add_edge("finalize", END)

    return graph_builder.compile()
