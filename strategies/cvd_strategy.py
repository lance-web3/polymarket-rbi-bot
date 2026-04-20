from __future__ import annotations

from polymarket_rbi_bot.models import MarketSnapshot, SignalSide, StrategySignal
from strategies.base import BaseStrategy


class CVDStrategy(BaseStrategy):
    name = "cvd"

    def __init__(self, lookback: int = 20, threshold: float = 50.0) -> None:
        self.lookback = lookback
        self.threshold = threshold

    def generate_signal(self, history: list[MarketSnapshot]) -> StrategySignal:
        if len(history) < self.lookback:
            return StrategySignal(self.name, SignalSide.HOLD, 0.0, reason="Not enough data")

        recent = history[-self.lookback :]
        trade_count = sum(len(snapshot.trades) for snapshot in recent)
        if trade_count == 0:
            return StrategySignal(
                self.name,
                SignalSide.HOLD,
                0.0,
                price=history[-1].mid_price,
                reason="No trade-flow data available for CVD",
                metadata={"cvd": 0.0, "trade_count": 0},
            )

        cvd = 0.0
        for snapshot in recent:
            for trade in snapshot.trades:
                signed_size = trade.size if trade.side.upper() == "BUY" else -trade.size
                cvd += signed_size

        confidence = min(abs(cvd) / max(self.threshold, 1), 1.0)
        if cvd >= self.threshold:
            return StrategySignal(
                self.name,
                SignalSide.BUY,
                confidence,
                price=history[-1].mid_price,
                reason=f"Positive cumulative volume delta {cvd:.2f}",
                metadata={"cvd": cvd, "trade_count": trade_count},
            )
        if cvd <= -self.threshold:
            return StrategySignal(
                self.name,
                SignalSide.SELL,
                confidence,
                price=history[-1].mid_price,
                reason=f"Negative cumulative volume delta {cvd:.2f}",
                metadata={"cvd": cvd, "trade_count": trade_count},
            )
        return StrategySignal(
            self.name,
            SignalSide.HOLD,
            confidence / 2,
            price=history[-1].mid_price,
            reason=f"CVD neutral at {cvd:.2f}",
            metadata={"cvd": cvd, "trade_count": trade_count},
        )
