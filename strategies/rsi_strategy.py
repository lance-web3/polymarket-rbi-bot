from __future__ import annotations

from polymarket_rbi_bot.models import MarketSnapshot, SignalSide, StrategySignal
from strategies.base import BaseStrategy


class RSIStrategy(BaseStrategy):
    name = "rsi"

    def __init__(self, period: int = 14, oversold: float = 35.0, overbought: float = 65.0) -> None:
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def generate_signal(self, history: list[MarketSnapshot]) -> StrategySignal:
        if len(history) < self.period + 1:
            return StrategySignal(self.name, SignalSide.HOLD, 0.0, reason="Not enough data")

        closes = [item.candle.close for item in history]
        deltas = [current - previous for previous, current in zip(closes[:-1], closes[1:])]
        gains = [max(delta, 0) for delta in deltas[-self.period :]]
        losses = [abs(min(delta, 0)) for delta in deltas[-self.period :]]
        average_gain = sum(gains) / self.period
        average_loss = sum(losses) / self.period
        rsi = 100.0 if average_loss == 0 else 100 - (100 / (1 + (average_gain / average_loss)))

        if rsi <= self.oversold:
            confidence = min((self.oversold - rsi) / max(self.oversold, 1), 1.0)
            return StrategySignal(
                self.name,
                SignalSide.BUY,
                confidence,
                price=history[-1].mid_price,
                reason=f"RSI oversold at {rsi:.2f}",
                metadata={"rsi": rsi},
            )
        if rsi >= self.overbought:
            confidence = min((rsi - self.overbought) / max(100 - self.overbought, 1), 1.0)
            return StrategySignal(
                self.name,
                SignalSide.SELL,
                confidence,
                price=history[-1].mid_price,
                reason=f"RSI overbought at {rsi:.2f}",
                metadata={"rsi": rsi},
            )
        return StrategySignal(
            self.name,
            SignalSide.HOLD,
            0.1,
            price=history[-1].mid_price,
            reason=f"RSI neutral at {rsi:.2f}",
            metadata={"rsi": rsi},
        )
