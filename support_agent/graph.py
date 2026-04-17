from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from support_agent.audit import apply_confidence_to_audit, record_tool_call
from support_agent.data_store import DataStore
from support_agent.llm import GeminiDecider
from support_agent.models import TicketState
from support_agent.tools import SupportTools, extract_order_id


def build_support_graph(
    store: DataStore,
    decider: GeminiDecider,
    confidence_threshold: float = 0.65,
):
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
        state["customer"] = tools.lookup_user(email)
        return state

    def lookup_order_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        customer = state.get("customer")
        customer_id = customer.get("customer_id") if customer else None
        state["order"] = tools.lookup_order(state.get("order_id"), customer_id)
        return state

    def lookup_product_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        order = state.get("order")
        product_id = order.get("product_id") if order else None
        state["product"] = tools.lookup_product(product_id)
        return state

    def knowledge_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        ticket = state.get("ticket", {})
        query = f"{ticket.get('subject', '')} {ticket.get('body', '')}"
        state["knowledge_snippets"] = tools.search_knowledge(query)
        return state

    def eligibility_node(state: TicketState) -> TicketState:
        tools = SupportTools(store, state)
        state["eligibility"] = tools.check_refund_eligibility(
            state.get("ticket", {}),
            state.get("customer"),
            state.get("order"),
            state.get("product"),
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
        ticket = state.get("ticket", {})
        decision = state.get("decision", {})

        state["escalated"] = False
        state["final_response"] = {
            "ticket_id": ticket.get("ticket_id"),
            "status": "resolved",
            "action": decision.get("action"),
            "priority": decision.get("priority"),
            "confidence": decision.get("confidence", 0.0),
            "message": decision.get("customer_message", ""),
            "reasoning": decision.get("reasoning", ""),
        }
        return state

    def escalate_node(state: TicketState) -> TicketState:
        ticket = state.get("ticket", {})
        decision = state.get("decision", {})
        eligibility = state.get("eligibility", {})

        state["escalated"] = True
        state["final_response"] = {
            "ticket_id": ticket.get("ticket_id"),
            "status": "escalated",
            "action": "escalate_human",
            "priority": decision.get("priority", "high"),
            "confidence": decision.get("confidence", 0.0),
            "message": decision.get(
                "customer_message",
                "Your request is being reviewed by a specialist team.",
            ),
            "reasoning": decision.get("reasoning", ""),
            "escalation_summary": decision.get(
                "escalation_summary",
                "; ".join(eligibility.get("escalation_reasons", [])),
            ),
            "recommended_path": "human_specialist_review",
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
