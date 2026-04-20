from __future__ import annotations

from abc import ABC, abstractmethod

from polymarket_rbi_bot.models import MarketSnapshot, StrategySignal


class BaseStrategy(ABC):
    name: str

    @abstractmethod
    def generate_signal(self, history: list[MarketSnapshot]) -> StrategySignal:
        raise NotImplementedError
