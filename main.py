from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from support_agent.audit import record_tool_call
from support_agent.data_store import DataStore
from support_agent.graph import build_support_graph
from support_agent.llm import GeminiDecider
from support_agent.models import TicketState


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ShopWave Agentic Support Backend")
    parser.add_argument("--tickets", default="tickets.json", help="Path to tickets JSON file")
    parser.add_argument("--out-dir", default="outputs", help="Output directory path")
    parser.add_argument("--model", default=os.getenv("GEMINI_MODEL", "gemini-1.5-flash"))
    parser.add_argument("--max-concurrency", type=int, default=10)
    parser.add_argument("--confidence-threshold", type=float, default=0.65)
    parser.add_argument("--api-key", default=os.getenv("GEMINI_API_KEY"))
    return parser.parse_args()


def load_tickets(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def initial_state(ticket: dict[str, Any]) -> TicketState:
    return {
        "ticket": ticket,
        "order_id": None,
        "customer": None,
        "order": None,
        "product": None,
        "knowledge_snippets": [],
        "eligibility": {},
        "decision": {
            "action": "ask_clarification",
            "confidence": 0.0,
            "needs_escalation": True,
            "priority": "medium",
            "reasoning": "Decision not computed yet.",
            "customer_message": "",
            "escalation_summary": "",
        },
        "final_response": {},
        "escalated": False,
        "tool_call_count": 0,
        "audit": [],
        "errors": [],
    }


async def process_ticket(ticket: dict[str, Any], graph: Any, semaphore: asyncio.Semaphore) -> TicketState:
    state = initial_state(ticket)

    async with semaphore:
        try:
            result: TicketState = await graph.ainvoke(state)
            return result
        except Exception as exc:  # pragma: no cover
            state["errors"].append(str(exc))
            state["decision"] = {
                "action": "escalate_human",
                "confidence": 0.0,
                "needs_escalation": True,
                "priority": "high",
                "reasoning": "Pipeline exception, safe escalation applied.",
                "customer_message": "Your case is being reviewed by a specialist team.",
                "escalation_summary": str(exc),
            }
            state["escalated"] = True
            state["final_response"] = {
                "ticket_id": ticket.get("ticket_id"),
                "status": "escalated",
                "action": "escalate_human",
                "priority": "high",
                "confidence": 0.0,
                "message": "Your case is being reviewed by a specialist team.",
                "reasoning": "Pipeline exception, safe escalation applied.",
                "escalation_summary": str(exc),
            }
            record_tool_call(
                state,
                "PIPELINE_ERROR",
                {"ticket_id": ticket.get("ticket_id")},
                {"error": str(exc)},
                confidence=0.0,
            )
            return state


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)


def build_exports(results: list[TicketState]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    sorted_results = sorted(results, key=lambda item: item.get("ticket", {}).get("ticket_id", ""))

    resolutions: list[dict[str, Any]] = []
    audit_log: list[dict[str, Any]] = []
    escalations: list[dict[str, Any]] = []

    for result in sorted_results:
        ticket = result.get("ticket", {})
        final_response = result.get("final_response", {})
        decision = result.get("decision", {})

        resolution_entry = {
            "ticket_id": ticket.get("ticket_id"),
            "customer_email": ticket.get("customer_email"),
            "status": final_response.get("status", "unknown"),
            "action": final_response.get("action", decision.get("action")),
            "confidence": final_response.get("confidence", decision.get("confidence", 0.0)),
            "priority": final_response.get("priority", decision.get("priority", "medium")),
            "escalated": result.get("escalated", False),
            "tool_call_count": result.get("tool_call_count", 0),
            "message": final_response.get("message", decision.get("customer_message", "")),
            "errors": result.get("errors", []),
        }

        resolutions.append(resolution_entry)

        audit_log.append(
            {
                "ticket_id": ticket.get("ticket_id"),
                "tool_call_count": result.get("tool_call_count", 0),
                "decision": decision,
                "audit_trail": result.get("audit", []),
            }
        )

        if result.get("escalated", False):
            escalations.append(
                {
                    "ticket_id": ticket.get("ticket_id"),
                    "priority": final_response.get("priority", "high"),
                    "summary": final_response.get("escalation_summary", decision.get("escalation_summary", "")),
                    "reasoning": final_response.get("reasoning", decision.get("reasoning", "")),
                }
            )

    summary = {
        "total_tickets": len(resolutions),
        "resolved": sum(1 for item in resolutions if item["status"] == "resolved"),
        "escalated": sum(1 for item in resolutions if item["status"] == "escalated"),
        "avg_confidence": round(
            sum(float(item.get("confidence", 0.0)) for item in resolutions) / max(len(resolutions), 1),
            4,
        ),
        "min_tool_calls": min((item.get("tool_call_count", 0) for item in resolutions), default=0),
    }

    return resolutions, audit_log, summary, escalations


async def run(args: argparse.Namespace) -> None:
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")

    store = DataStore(root)
    decider = GeminiDecider(api_key=args.api_key, model_name=args.model)
    graph = build_support_graph(
        store=store,
        decider=decider,
        confidence_threshold=args.confidence_threshold,
    )

    tickets_path = Path(args.tickets)
    if not tickets_path.is_absolute():
        tickets_path = root / tickets_path

    tickets = load_tickets(tickets_path)
    semaphore = asyncio.Semaphore(max(1, args.max_concurrency))

    tasks = [process_ticket(ticket, graph, semaphore) for ticket in tickets]
    results = await asyncio.gather(*tasks)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = root / out_dir

    resolutions, audit_log, summary, escalations = build_exports(results)

    write_json(out_dir / "resolutions.json", resolutions)
    write_json(out_dir / "audit_log.json", audit_log)
    write_json(out_dir / "summary.json", summary)
    write_json(out_dir / "escalations.json", escalations)

    print("Run completed.")
    print(json.dumps(summary, indent=2))
    print(f"Outputs written to: {out_dir}")


if __name__ == "__main__":
    cli_args = parse_args()
    asyncio.run(run(cli_args))
