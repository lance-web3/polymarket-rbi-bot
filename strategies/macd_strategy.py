from __future__ import annotations

from statistics import fmean

from polymarket_rbi_bot.models import MarketSnapshot, SignalSide, StrategySignal
from strategies.base import BaseStrategy


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    multiplier = 2 / (period + 1)
    ema_values = [values[0]]
    for value in values[1:]:
        ema_values.append((value - ema_values[-1]) * multiplier + ema_values[-1])
    return ema_values


class MACDStrategy(BaseStrategy):
    name = "macd"

    def __init__(self, fast_period: int = 12, slow_period: int = 26, signal_period: int = 9) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period

    def generate_signal(self, history: list[MarketSnapshot]) -> StrategySignal:
        if len(history) < self.slow_period + self.signal_period:
            return StrategySignal(self.name, SignalSide.HOLD, 0.0, reason="Not enough data")

        closes = [item.candle.close for item in history]
        fast = _ema(closes, self.fast_period)
        slow = _ema(closes, self.slow_period)
        macd_line = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
        signal_line = _ema(macd_line, self.signal_period)

        current_macd = macd_line[-1]
        current_signal = signal_line[-1]
        spread = current_macd - current_signal
        confidence = min(abs(spread) * 100, 1.0)

        if spread > 0 and current_macd > 0:
            return StrategySignal(
                self.name,
                SignalSide.BUY,
                confidence,
                price=history[-1].mid_price,
                reason=f"Bullish MACD crossover ({spread:.4f})",
                metadata={"macd": current_macd, "signal": current_signal, "bias": fmean(macd_line[-5:])},
            )
        if spread < 0 and current_macd < 0:
            return StrategySignal(
                self.name,
                SignalSide.SELL,
                confidence,
                price=history[-1].mid_price,
                reason=f"Bearish MACD crossover ({spread:.4f})",
                metadata={"macd": current_macd, "signal": current_signal, "bias": fmean(macd_line[-5:])},
            )
        return StrategySignal(
            self.name,
            SignalSide.HOLD,
            confidence / 2,
            price=history[-1].mid_price,
            reason="MACD signal is mixed",
            metadata={"macd": current_macd, "signal": current_signal},
        )
