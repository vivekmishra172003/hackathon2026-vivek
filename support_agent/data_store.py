from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class DataStore:
    def __init__(self, base_path: Path):
        self.base_path = base_path

        self.customers: list[dict[str, Any]] = self._load_json("customers.json")
        self.orders: list[dict[str, Any]] = self._load_json("orders.json")
        self.products: list[dict[str, Any]] = self._load_json("products.json")
        self.knowledge_base = (self.base_path / "knowledge-base.md").read_text(encoding="utf-8")

        self.customers_by_email = {
            customer["email"].lower(): customer for customer in self.customers
        }
        self.customers_by_id = {
            customer["customer_id"]: customer for customer in self.customers
        }
        self.orders_by_id = {
            order["order_id"].upper(): order for order in self.orders
        }
        self.products_by_id = {
            product["product_id"].upper(): product for product in self.products
        }

        self.knowledge_sections = self._split_knowledge_sections(self.knowledge_base)

    def _load_json(self, filename: str) -> list[dict[str, Any]]:
        path = self.base_path / filename
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _split_knowledge_sections(self, markdown_text: str) -> list[tuple[str, str]]:
        sections: list[tuple[str, str]] = []
        current_title = "General"
        current_lines: list[str] = []

        for line in markdown_text.splitlines():
            if line.startswith("## "):
                if current_lines:
                    sections.append((current_title, "\n".join(current_lines).strip()))
                current_title = line[3:].strip()
                current_lines = [line]
            else:
                current_lines.append(line)

        if current_lines:
            sections.append((current_title, "\n".join(current_lines).strip()))

        return sections

    def get_customer_by_email(self, email: str) -> dict[str, Any] | None:
        return self.customers_by_email.get(email.lower())

    def get_order_by_id(self, order_id: str) -> dict[str, Any] | None:
        return self.orders_by_id.get(order_id.upper())

    def get_product_by_id(self, product_id: str) -> dict[str, Any] | None:
        return self.products_by_id.get(product_id.upper())

    def get_orders_for_customer(self, customer_id: str) -> list[dict[str, Any]]:
        return [order for order in self.orders if order["customer_id"] == customer_id]

    def get_latest_order_for_customer(self, customer_id: str) -> dict[str, Any] | None:
        orders = self.get_orders_for_customer(customer_id)
        if not orders:
            return None
        return sorted(orders, key=lambda item: item["order_date"], reverse=True)[0]

    def search_knowledge(self, query: str, top_k: int = 3) -> list[str]:
        query_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", query.lower())
            if len(token) > 2
        }
        if not query_tokens:
            return [section for _, section in self.knowledge_sections[:top_k]]

        scored: list[tuple[int, str]] = []
        for title, section in self.knowledge_sections:
            section_tokens = set(re.findall(r"[a-z0-9]+", section.lower()))
            title_tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
            score = len(query_tokens & section_tokens) + (2 * len(query_tokens & title_tokens))
            scored.append((score, section))

        ranked = sorted(scored, key=lambda item: item[0], reverse=True)
        selected = [section for score, section in ranked if score > 0][:top_k]

        if not selected:
            return [section for _, section in self.knowledge_sections[:top_k]]

        return selected
