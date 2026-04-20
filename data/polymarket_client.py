from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from data.market_discovery import GammaMarketDiscoveryClient


@dataclass(slots=True)
class PolymarketHistoryClient:
    host: str = "https://clob.polymarket.com"
    timeout: int = 30

    def fetch_price_history(
        self,
        *,
        token_id: str,
        interval: str = "max",
        fidelity: int = 60,
        start_ts: int | None = None,
        end_ts: int | None = None,
        include_market_metadata: bool = True,
    ) -> list[dict[str, float | str]]:
        params: dict[str, Any] = {
            "market": token_id,
            "interval": interval,
            "fidelity": fidelity,
        }
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts

        response = requests.get(
            f"{self.host}/prices-history",
            params=params,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        history = payload.get("history", [])
        market_metadata = self.fetch_market_metadata(token_id=token_id) if include_market_metadata else {}

        return [
            {
                "timestamp": datetime.fromtimestamp(point["t"], tz=timezone.utc).isoformat(),
                "open": float(point["p"]),
                "high": float(point["p"]),
                "low": float(point["p"]),
                "close": float(point["p"]),
                "volume": 0.0,
                "best_bid": "",
                "best_ask": "",
                **market_metadata,
            }
            for point in history
        ]

    def fetch_market_metadata(self, *, token_id: str) -> dict[str, float | str]:
        market = GammaMarketDiscoveryClient(timeout=self.timeout).find_market_by_token_id(token_id, limit=1000)
        if not market:
            return {}

        best_bid = self._safe_float(market.get("bestBid"))
        best_ask = self._safe_float(market.get("bestAsk"))
        spread_bps = None
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else None
            if mid:
                spread_bps = ((best_ask - best_bid) / mid) * 10_000
        if spread_bps is None:
            spread_bps = self._safe_float(market.get("spread"))

        metadata: dict[str, float | str] = {
            "market_id": str(market.get("id") or ""),
            "market_slug": str(market.get("slug") or ""),
            "market_question": str(market.get("question") or ""),
            "market_category": str(market.get("category") or ""),
            "market_description": str(market.get("description") or ""),
            "market_liquidity": str(market.get("liquidity") or market.get("liquidityNum") or ""),
            "market_volume": str(market.get("volume") or market.get("volumeNum") or ""),
            "market_best_bid": "" if best_bid is None else str(best_bid),
            "market_best_ask": "" if best_ask is None else str(best_ask),
            "market_current_spread_bps": "" if spread_bps is None else str(spread_bps),
            "market_metadata_quote_note": "current_gamma_snapshot_only_not_historical",
        }
        for key in ("endDate", "createdAt"):
            if market.get(key) not in {None, ""}:
                metadata[key] = str(market[key])
        return metadata

    def fetch_last_trade_price(self, *, token_id: str) -> float:
        response = requests.get(
            f"{self.host}/last-trade-price",
            params={"tokenID": token_id},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return float(payload["price"])

    @staticmethod
    def _safe_float(value: object) -> float | None:
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
