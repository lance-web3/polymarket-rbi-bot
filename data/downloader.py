from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import ccxt


@dataclass(slots=True)
class OHLCVDownloader:
    exchange_id: str = "binance"

    def _exchange(self) -> Any:
        exchange_class = getattr(ccxt, self.exchange_id)
        return exchange_class({"enableRateLimit": True})

    def fetch_ohlcv(
        self,
        *,
        symbol: str,
        timeframe: str = "5m",
        since_ms: int | None = None,
        limit: int = 500,
        max_batches: int = 10,
    ) -> list[dict[str, float | str]]:
        exchange = self._exchange()
        exchange.load_markets()
        if not exchange.has.get("fetchOHLCV"):
            raise ValueError(f"{self.exchange_id} does not support fetchOHLCV")

        rows: list[list[float]] = []
        cursor = since_ms
        for _ in range(max_batches):
            batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit)
            if not batch:
                break

            if rows and batch[0][0] <= rows[-1][0]:
                batch = [candle for candle in batch if candle[0] > rows[-1][0]]
            if not batch:
                break

            rows.extend(batch)
            cursor = int(batch[-1][0]) + 1
            time.sleep(exchange.rateLimit / 1000)

        return [self._normalize_row(row) for row in rows]

    @staticmethod
    def _normalize_row(row: list[float]) -> dict[str, float | str]:
        timestamp_ms, open_, high, low, close, volume = row
        return {
            "timestamp": datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat(),
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": float(volume),
        }

