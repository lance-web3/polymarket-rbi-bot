from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(slots=True)
class GammaMarketDiscoveryClient:
    host: str = "https://gamma-api.polymarket.com"
    timeout: int = 30

    def list_markets(self, *, limit: int = 100, closed: bool = False, archived: bool = False) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.host}/markets",
            params={"limit": limit, "closed": str(closed).lower(), "archived": str(archived).lower()},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, list) else payload.get("data", [])

    def find_market_by_condition_id(self, condition_id: str, *, limit: int = 500) -> dict[str, Any] | None:
        for market in self.list_markets(limit=limit, closed=False, archived=False):
            if str(market.get("conditionId") or market.get("condition_id") or "") == condition_id:
                return market
        return None

    def find_market_by_token_id(self, token_id: str, *, limit: int = 500) -> dict[str, Any] | None:
        for market in self.list_markets(limit=limit, closed=False, archived=False):
            token_ids = parse_jsonish_list(market.get("clobTokenIds"))
            if any(str(candidate) == token_id for candidate in token_ids):
                return market
        return None


def parse_jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def extract_yes_token(market: dict[str, Any]) -> dict[str, Any] | None:
    token_ids = parse_jsonish_list(market.get("clobTokenIds"))
    outcomes = parse_jsonish_list(market.get("outcomes"))
    outcome_prices = parse_jsonish_list(market.get("outcomePrices"))
    if len(token_ids) < 2 or len(outcomes) < 2:
        return None

    pairs = []
    for idx, token_id in enumerate(token_ids):
        outcome = str(outcomes[idx]) if idx < len(outcomes) else f"outcome_{idx}"
        price = float(outcome_prices[idx]) if idx < len(outcome_prices) else None
        pairs.append({"token_id": str(token_id), "outcome": outcome, "price": price})

    for pair in pairs:
        if pair["outcome"].strip().lower() == "yes":
            return pair
    return pairs[0] if pairs else None
