from __future__ import annotations

from statistics import fmean, pstdev

from polymarket_rbi_bot.models import MarketSnapshot, SignalSide, StrategySignal
from strategies.base import BaseStrategy


class LongEntryStrategy(BaseStrategy):
    name = "long_entry"

    def __init__(
        self,
        lookback: int = 24,
        min_return_bps: float = 75.0,
        max_return_bps: float = 2500.0,
        min_price: float = 0.12,
        max_price: float = 0.75,
        max_pullback_from_high_bps: float = 600.0,
        strict_mode: bool = False,
        strict_min_return_bps: float = 180.0,
        strict_max_price: float = 0.68,
        strict_min_price: float = 0.18,
        strict_max_pullback_bps: float = 250.0,
        min_positive_closes: int = 3,
        min_above_mean_bps: float = 35.0,
        max_realized_volatility_bps: float = 350.0,
        breakout_fraction: float = 0.35,
        fast_momentum_window: int = 6,
        medium_momentum_window: int = 12,
        slow_momentum_window: int = 24,
        min_fast_momentum_bps: float = 35.0,
        min_medium_momentum_bps: float = 90.0,
        min_slow_momentum_bps: float = 180.0,
        min_baseline_persistence: float = 0.52,
        min_trend_efficiency: float = 0.28,
        max_one_bar_jump_bps: float = 220.0,
        max_jump_share: float = 0.75,
        max_volatility_burst_ratio: float = 2.4,
        min_breakout_distance_bps: float = 5.0,
        signal_version: str = "v2",
    ) -> None:
        self.lookback = max(lookback, slow_momentum_window)
        self.min_return_bps = min_return_bps
        self.max_return_bps = max_return_bps
        self.min_price = min_price
        self.max_price = max_price
        self.max_pullback_from_high_bps = max_pullback_from_high_bps
        self.strict_mode = strict_mode
        self.strict_min_return_bps = strict_min_return_bps
        self.strict_max_price = strict_max_price
        self.strict_min_price = strict_min_price
        self.strict_max_pullback_bps = strict_max_pullback_bps
        self.min_positive_closes = min_positive_closes
        self.min_above_mean_bps = min_above_mean_bps
        self.max_realized_volatility_bps = max_realized_volatility_bps
        self.breakout_fraction = breakout_fraction
        self.fast_momentum_window = max(2, fast_momentum_window)
        self.medium_momentum_window = max(self.fast_momentum_window, medium_momentum_window)
        self.slow_momentum_window = max(self.medium_momentum_window, slow_momentum_window)
        self.min_fast_momentum_bps = min_fast_momentum_bps
        self.min_medium_momentum_bps = min_medium_momentum_bps
        self.min_slow_momentum_bps = min_slow_momentum_bps
        self.min_baseline_persistence = min_baseline_persistence
        self.min_trend_efficiency = min_trend_efficiency
        self.max_one_bar_jump_bps = max_one_bar_jump_bps
        self.max_jump_share = max_jump_share
        self.max_volatility_burst_ratio = max_volatility_burst_ratio
        self.min_breakout_distance_bps = min_breakout_distance_bps
        self.signal_version = (signal_version or "v2").strip().lower()

    def generate_signal(self, history: list[MarketSnapshot]) -> StrategySignal:
        required_points = max(self.lookback, self.slow_momentum_window) + 1
        if len(history) < required_points:
            return StrategySignal(self.name, SignalSide.HOLD, 0.0, reason='Not enough data')

        closes = [item.candle.close for item in history]
        recent = closes[-self.lookback :]
        current = recent[-1]
        start = recent[0]
        recent_high = max(recent)
        recent_low = min(recent)
        recent_mean = fmean(recent)
        baseline_window = recent[-self.medium_momentum_window :]
        baseline = fmean(baseline_window)

        active_min_price = self.strict_min_price if self.strict_mode else self.min_price
        active_max_price = self.strict_max_price if self.strict_mode else self.max_price
        active_min_return_bps = self.strict_min_return_bps if self.strict_mode else self.min_return_bps
        active_max_pullback_bps = self.strict_max_pullback_bps if self.strict_mode else self.max_pullback_from_high_bps

        if current < active_min_price or current > active_max_price:
            return StrategySignal(
                self.name,
                SignalSide.HOLD,
                0.0,
                price=current,
                reason=f'Price {current:.3f} outside buy zone',
                metadata={'price': current, 'strict_mode': self.strict_mode},
            )

        if start <= 0:
            return StrategySignal(self.name, SignalSide.HOLD, 0.0, price=current, reason='Invalid starting price')

        return_bps = ((current - start) / start) * 10_000
        pullback_bps = ((recent_high - current) / recent_high) * 10_000 if recent_high > 0 else 0.0
        above_mean_bps = ((current - recent_mean) / recent_mean) * 10_000 if recent_mean > 0 else 0.0
        breakout_distance_bps = ((current - baseline) / baseline) * 10_000 if baseline > 0 else 0.0
        recent_range = max(recent_high - recent_low, 1e-9)
        breakout_position = (current - recent_low) / recent_range

        returns_bps = [((curr - prev) / prev) * 10_000 for prev, curr in zip(recent[:-1], recent[1:]) if prev > 0]
        realized_volatility_bps = pstdev(returns_bps) if len(returns_bps) >= 2 else 0.0
        positive_closes = sum(1 for prev, curr in zip(recent[:-1], recent[1:]) if curr > prev)

        fast_slice = closes[-(self.fast_momentum_window + 1) :]
        medium_slice = closes[-(self.medium_momentum_window + 1) :]
        slow_slice = closes[-(self.slow_momentum_window + 1) :]
        fast_momentum_bps = self._window_return_bps(fast_slice)
        medium_momentum_bps = self._window_return_bps(medium_slice)
        slow_momentum_bps = self._window_return_bps(slow_slice)

        momentum_alignment = sum(
            1 for value in (fast_momentum_bps, medium_momentum_bps, slow_momentum_bps) if value > 0
        ) / 3.0
        if medium_momentum_bps >= fast_momentum_bps:
            momentum_alignment += 0.15
        if slow_momentum_bps >= medium_momentum_bps:
            momentum_alignment += 0.15
        momentum_alignment = min(momentum_alignment, 1.0)

        baseline_persistence = sum(1 for price in baseline_window if price >= baseline) / max(len(baseline_window), 1)
        path_distance_bps = sum(abs(move) for move in returns_bps)
        trend_efficiency = abs(return_bps) / path_distance_bps if path_distance_bps > 0 else 0.0
        largest_up_jump_bps = max([move for move in returns_bps if move > 0], default=0.0)
        jump_share = largest_up_jump_bps / max(return_bps, 1e-9) if return_bps > 0 else 1.0
        short_returns = returns_bps[-self.fast_momentum_window :] if returns_bps else []
        short_volatility_bps = pstdev(short_returns) if len(short_returns) >= 2 else 0.0
        volatility_burst_ratio = short_volatility_bps / max(realized_volatility_bps, 1.0)

        common_meta = {
            'return_bps': return_bps,
            'pullback_bps': pullback_bps,
            'above_mean_bps': above_mean_bps,
            'realized_volatility_bps': realized_volatility_bps,
            'positive_closes': positive_closes,
            'breakout_position': breakout_position,
            'breakout_distance_bps': breakout_distance_bps,
            'fast_momentum_bps': fast_momentum_bps,
            'medium_momentum_bps': medium_momentum_bps,
            'slow_momentum_bps': slow_momentum_bps,
            'momentum_alignment': round(momentum_alignment, 3),
            'baseline_persistence': round(baseline_persistence, 3),
            'trend_efficiency': round(trend_efficiency, 3),
            'largest_up_jump_bps': round(largest_up_jump_bps, 1),
            'jump_share': round(jump_share, 3),
            'volatility_burst_ratio': round(volatility_burst_ratio, 3),
            'strict_mode': self.strict_mode,
        }

        if return_bps < active_min_return_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.1, price=current, reason=f'Momentum too weak at {return_bps:.1f} bps', metadata=common_meta)
        if return_bps > self.max_return_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Move too extended at {return_bps:.1f} bps', metadata=common_meta)
        if pullback_bps > active_max_pullback_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Pullback too deep at {pullback_bps:.1f} bps', metadata=common_meta)

        if self.strict_mode and fast_momentum_bps < self.min_fast_momentum_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: fast momentum too weak at {fast_momentum_bps:.1f} bps', metadata=common_meta)
        if self.strict_mode and medium_momentum_bps < self.min_medium_momentum_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: medium momentum too weak at {medium_momentum_bps:.1f} bps', metadata=common_meta)
        if self.strict_mode and slow_momentum_bps < self.min_slow_momentum_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: slow momentum too weak at {slow_momentum_bps:.1f} bps', metadata=common_meta)
        if self.strict_mode and momentum_alignment < 0.55:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: momentum alignment only {momentum_alignment:.2f}', metadata=common_meta)
        if self.strict_mode and positive_closes < self.min_positive_closes:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: only {positive_closes} positive closes in lookback', metadata=common_meta)
        if self.strict_mode and above_mean_bps < self.min_above_mean_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: price only {above_mean_bps:.1f} bps above mean', metadata=common_meta)
        if self.strict_mode and baseline_persistence < self.min_baseline_persistence:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: baseline persistence only {baseline_persistence:.2f}', metadata=common_meta)
        if self.strict_mode and trend_efficiency < self.min_trend_efficiency:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: trend too choppy at efficiency {trend_efficiency:.2f}', metadata=common_meta)
        if self.strict_mode and breakout_position < self.breakout_fraction:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: breakout position {breakout_position:.2f} too weak', metadata=common_meta)
        if self.strict_mode and breakout_distance_bps < self.min_breakout_distance_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.2, price=current, reason=f'Strict mode: breakout distance only {breakout_distance_bps:.1f} bps', metadata=common_meta)
        if self.strict_mode and realized_volatility_bps > self.max_realized_volatility_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.15, price=current, reason=f'Strict mode: volatility too high at {realized_volatility_bps:.1f} bps', metadata=common_meta)
        if self.strict_mode and largest_up_jump_bps > self.max_one_bar_jump_bps:
            return StrategySignal(self.name, SignalSide.HOLD, 0.15, price=current, reason=f'Strict mode: one-bar jump too large at {largest_up_jump_bps:.1f} bps', metadata=common_meta)
        if self.strict_mode and jump_share > self.max_jump_share:
            return StrategySignal(self.name, SignalSide.HOLD, 0.15, price=current, reason=f'Strict mode: move too dependent on one jump ({jump_share:.2f} share)', metadata=common_meta)
        if self.strict_mode and volatility_burst_ratio > self.max_volatility_burst_ratio:
            return StrategySignal(self.name, SignalSide.HOLD, 0.15, price=current, reason=f'Strict mode: short-term volatility burst {volatility_burst_ratio:.2f}x baseline', metadata=common_meta)

        if self.signal_version == 'legacy':
            base_confidence = 0.45 + min(max(return_bps / max(active_min_return_bps * 2.0, 1.0), 0.0), 0.35)
            pullback_penalty = min(max(pullback_bps / max(active_max_pullback_bps * 1.5, 1.0), 0.0), 0.20)
            confidence = min(max(base_confidence - pullback_penalty, 0.2), 0.9)
            expected_edge_bps = max(0.55 * return_bps - 0.35 * pullback_bps, 0.0)
            return StrategySignal(
                self.name,
                SignalSide.BUY,
                confidence,
                price=current,
                reason=f'LongEntry legacy: return {return_bps:.1f} bps, pullback {pullback_bps:.1f} bps',
                metadata={
                    **common_meta,
                    'recent_mean': recent_mean,
                    'price': current,
                    'expected_edge_bps': round(expected_edge_bps, 1),
                    'signal_version': 'legacy',
                },
            )

        momentum_score = min(max(slow_momentum_bps / max(self.min_slow_momentum_bps * 2.0, 1.0), 0.0), 1.0)
        alignment_score = momentum_alignment
        pullback_score = min(max(1.0 - (pullback_bps / max(active_max_pullback_bps, 1.0)), 0.0), 1.0)
        mean_score = min(max(above_mean_bps / max(self.min_above_mean_bps * 2.0, 1.0), 0.0), 1.0)
        breakout_score = min(max((breakout_position - self.breakout_fraction) / max(1.0 - self.breakout_fraction, 1e-9), 0.0), 1.0)
        breakout_distance_score = min(max(breakout_distance_bps / max(self.min_breakout_distance_bps * 4.0, 1.0), 0.0), 1.0)
        persistence_score = min(max((baseline_persistence - self.min_baseline_persistence) / max(1.0 - self.min_baseline_persistence, 1e-9), 0.0), 1.0)
        trend_score = min(max(trend_efficiency / max(self.min_trend_efficiency * 1.8, 1e-9), 0.0), 1.0)
        volatility_score = min(max(1.0 - (realized_volatility_bps / max(self.max_realized_volatility_bps, 1.0)), 0.0), 1.0)
        jump_penalty = min(max(largest_up_jump_bps / max(self.max_one_bar_jump_bps * 1.5, 1.0), 0.0), 1.0)
        burst_penalty = min(max((volatility_burst_ratio - 1.0) / max(self.max_volatility_burst_ratio - 1.0, 1e-9), 0.0), 1.0)

        if self.strict_mode:
            confidence = (
                0.22 * momentum_score
                + 0.18 * alignment_score
                + 0.14 * persistence_score
                + 0.12 * trend_score
                + 0.10 * pullback_score
                + 0.08 * mean_score
                + 0.08 * breakout_score
                + 0.08 * breakout_distance_score
                + 0.10 * volatility_score
                - 0.06 * jump_penalty
                - 0.04 * burst_penalty
            )
            confidence = min(max(confidence, 0.0), 0.98)
        else:
            confidence = min(max((0.65 * momentum_score + 0.2 * pullback_score + 0.15 * alignment_score), 0.2), 0.9)

        smooth_move_bonus = 0.22 * slow_momentum_bps + 0.12 * medium_momentum_bps
        smooth_move_penalty = 0.60 * pullback_bps + 0.55 * largest_up_jump_bps + 0.35 * max(realized_volatility_bps - 160.0, 0.0)
        persistence_bonus = 55.0 * baseline_persistence + 80.0 * trend_efficiency + 35.0 * breakout_score
        cheap_tail_penalty = max((self.strict_min_price - current) * 900.0, 0.0) if self.strict_mode else 0.0
        fragile_breakout_penalty = 45.0 * max(0.0, 0.45 - breakout_score)
        weak_alignment_penalty = 35.0 * max(0.0, 0.65 - alignment_score)
        expected_edge_bps = max(
            smooth_move_bonus
            + persistence_bonus
            - smooth_move_penalty
            - cheap_tail_penalty
            - fragile_breakout_penalty
            - weak_alignment_penalty,
            0.0,
        )

        return StrategySignal(
            self.name,
            SignalSide.BUY,
            confidence,
            price=current,
            reason=(
                f'LongEntry v2: slow {slow_momentum_bps:.1f} bps, alignment {momentum_alignment:.2f}, '
                f'persistence {baseline_persistence:.2f}, jump {largest_up_jump_bps:.1f} bps'
            ),
            metadata={
                **common_meta,
                'recent_mean': recent_mean,
                'price': current,
                'expected_edge_bps': round(expected_edge_bps, 1),
                'signal_version': 'long_entry_v2',
            },
        )

    def _window_return_bps(self, values: list[float]) -> float:
        if len(values) < 2 or values[0] <= 0:
            return 0.0
        return ((values[-1] - values[0]) / values[0]) * 10_000
