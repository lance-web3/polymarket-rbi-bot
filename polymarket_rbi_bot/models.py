from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SignalSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(slots=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(slots=True)
class TradeTick:
    timestamp: datetime
    price: float
    size: float
    side: str


@dataclass(slots=True)
class MarketSnapshot:
    candle: Candle
    trades: list[TradeTick] = field(default_factory=list)
    best_bid: float | None = None
    best_ask: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def mid_price(self) -> float:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.candle.close


@dataclass(slots=True)
class StrategySignal:
    strategy_name: str
    side: SignalSide
    confidence: float
    price: float | None = None
    size: float | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderIntent:
    token_id: str
    side: SignalSide
    price: float
    size: float
    tick_size: str = "0.01"
    neg_risk: bool = False
    order_type: str = "GTC"
    post_only: bool = True


@dataclass(slots=True)
class Position:
    token_id: str
    quantity: float = 0.0
    average_price: float = 0.0
    opened_at: datetime | None = None


@dataclass(slots=True)
class Fill:
    timestamp: datetime
    token_id: str
    side: SignalSide
    price: float
    size: float
    fees: float = 0.0


@dataclass(slots=True)
class BacktestTrade:
    timestamp: datetime
    strategy_name: str
    side: SignalSide
    price: float
    size: float
    pnl_after_trade: float
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BacktestResult:
    starting_cash: float
    ending_cash: float
    realized_pnl: float
    mark_to_market_equity: float
    max_drawdown: float
    trades: list[BacktestTrade]
    metadata: dict[str, Any] = field(default_factory=dict)

