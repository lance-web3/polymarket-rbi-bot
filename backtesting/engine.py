from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from polymarket_rbi_bot.models import BacktestResult, BacktestTrade, MarketSnapshot, SignalSide
from polymarket_rbi_bot.microstructure import (
    compute_microstructure_metrics,
    compute_time_to_resolution_hours,
)
from strategies.base import BaseStrategy


@dataclass(slots=True)
class BacktestEngine:
    strategies: list[BaseStrategy]
    starting_cash: float = 1_000.0
    per_trade_size: float = 10.0
    slippage_bps: float = 25.0
    fallback_half_spread_bps: float = 50.0
    fee_bps: float = 0.0
    adverse_selection_horizon_bars: int = 1
    max_spread_bps: float = 1_500.0
    missing_quote_fill_ratio: float = 1.0
    wide_spread_fill_ratio: float = 0.5
    strict_mode: bool = False
    min_entry_confidence: float = 0.50
    min_buy_score: float = 1.1
    min_buy_sell_score_gap: float = 0.35
    min_buy_signal_count: int = 1
    strict_require_confirmers: bool = False
    strict_min_confirmers: int = 0
    strict_confirmer_buy_bonus: float = 0.08
    strict_confirmer_sell_penalty: float = 0.12
    strict_min_entry_score: float = 0.55
    min_hold_bars: int = 3
    cooldown_bars_after_exit: int = 2
    strict_max_hold_bars: int = 12
    strict_fail_exit_bars: int = 6
    strict_fail_exit_pnl_bps: float = -35.0
    strict_take_profit_bars: int = 4
    strict_take_profit_pnl_bps: float = 80.0
    strict_profit_giveback_bps: float = 45.0
    strict_extended_hold_bars: int = 8
    strict_extended_hold_exit_gap: float = 0.15
    estimated_round_trip_cost_bps: float = 80.0
    min_expected_edge_bps: float = 120.0
    edge_cost_buffer_bps: float = 30.0
    # New: maturity + microstructure gating (used only if strict_mode)
    enable_maturity_gating: bool = True
    enable_microstructure_gating: bool = True
    strict_min_time_to_resolution_hours: float | None = None
    strict_max_time_to_resolution_hours: float | None = None
    strict_min_time_since_open_hours: float | None = None
    strict_quote_lookback_bars: int = 24
    strict_min_quote_observations: int = 3
    strict_min_quote_availability_ratio: float = 0.25
    strict_max_avg_spread_bps: float = 450.0
    strict_max_current_spread_bps: float = 450.0
    strict_max_wide_spread_rate: float = 0.65
    strict_wide_spread_bps: float = 700.0
    microstructure_proxy_policy: str = "auto"
    strict_long_entry_led: bool = True
    strict_exit_style: str = "upgraded"
    market_filter: Any = None
    market_metadata: dict[str, Any] | None = None

    def run(self, history: list[MarketSnapshot]) -> BacktestResult:
        family_filter_skip = self._maybe_family_filter_skip()
        if family_filter_skip is not None:
            return family_filter_skip
        if len(history) < 2:
            return BacktestResult(
                starting_cash=self.starting_cash,
                ending_cash=self.starting_cash,
                realized_pnl=0.0,
                mark_to_market_equity=self.starting_cash,
                max_drawdown=0.0,
                trades=[],
                metadata={
                    "ending_inventory": 0.0,
                    "note": "Need at least 2 snapshots for next-bar execution.",
                    "metrics": self._empty_metrics(),
                    "cost_attribution": self._build_cost_attribution(trades=[], realized_pnl=0.0),
                    "execution_model": self._execution_model_metadata(),
                },
            )

        cash = self.starting_cash
        inventory = 0.0
        average_entry_price = 0.0
        realized_pnl = 0.0
        peak_equity = self.starting_cash
        max_drawdown = 0.0
        trades: list[BacktestTrade] = []
        closing_trade_pnls: list[float] = []
        total_inventory = 0.0
        total_inventory_notional = 0.0
        bars_with_inventory = 0
        max_inventory = 0.0
        bars_since_entry: int | None = None
        peak_price_since_entry: float | None = None
        cooldown_remaining = 0
        blocked_entry_reasons: dict[str, int] = {
            "long_entry_missing": 0,
            "long_entry_not_buy": 0,
            "long_entry_confidence": 0,
            "confirmers": 0,
            "entry_score": 0,
            "edge_vs_cost": 0,
            "cooldown": 0,
            # maturity/microstructure new
            "maturity_too_soon": 0,
            "maturity_too_far": 0,
            "market_too_new": 0,
            "spread_too_wide": 0,
            "avg_spread_too_wide": 0,
            "insufficient_quotes": 0,
            "wide_spread_rate": 0,
        }
        blocked_exit_reasons: dict[str, int] = {
            "min_hold": 0,
            "weak_exit": 0,
            "max_hold": 0,
            "fail_exit": 0,
            "profit_protect": 0,
            "extended_hold_weak_exit": 0,
        }

        for index in range(len(history) - 1):
            window = history[: index + 1]
            snapshot = window[-1]
            next_snapshot = history[index + 1]
            mark_price = snapshot.mid_price
            summary = self._summarize_signals(window)
            decision = self._resolve_decision(summary=summary, snapshot=snapshot, inventory=inventory)

            if inventory > 0:
                if peak_price_since_entry is None:
                    peak_price_since_entry = mark_price
                else:
                    peak_price_since_entry = max(peak_price_since_entry, mark_price)

            if decision == SignalSide.BUY and inventory == 0 and cooldown_remaining > 0:
                blocked_entry_reasons["cooldown"] += 1
                decision = SignalSide.HOLD

            if decision == SignalSide.BUY and inventory == 0:
                execution = self._resolve_execution(next_snapshot, SignalSide.BUY)
                fill_size = min(self.per_trade_size * execution["fill_ratio"], self.per_trade_size)
                if execution["price"] is not None and fill_size > 0:
                    affordable_size = cash / execution["price"] if execution["price"] > 0 else 0.0
                    fill_size = min(fill_size, affordable_size)
                if execution["price"] is not None and fill_size > 0:
                    notional = execution["price"] * fill_size
                    cash -= notional
                    average_entry_price = execution["price"]
                    inventory += fill_size
                    bars_since_entry = 0
                    peak_price_since_entry = snapshot.mid_price
                    post_fill_move_bps = self._post_fill_move_bps(
                        history=history,
                        fill_index=index + 1,
                        fill_price=execution["price"],
                        side=SignalSide.BUY,
                    )
                    trades.append(
                        BacktestTrade(
                            timestamp=next_snapshot.candle.timestamp,
                            strategy_name="ensemble",
                            side=decision,
                            price=execution["price"],
                            size=fill_size,
                            pnl_after_trade=realized_pnl,
                            metadata={
                                "fill_ratio": execution["fill_ratio"],
                                "quote_source": execution["quote_source"],
                                "spread_bps": execution["spread_bps"],
                                "decision_summary": summary,
                                "mid_at_fill": execution["mid_at_fill"],
                                "spread_cost_bps": execution["spread_cost_bps"],
                                "slippage_cost_bps": execution["slippage_cost_bps"],
                                "fee_cost_bps": execution["fee_cost_bps"],
                                "post_fill_move_bps": post_fill_move_bps,
                                "notional": notional,
                            },
                        )
                    )
            elif inventory > 0:
                exit_decision = (
                    self._resolve_strict_exit_decision(
                        summary=summary,
                        snapshot=snapshot,
                        bars_since_entry=bars_since_entry,
                        average_entry_price=average_entry_price,
                        peak_price_since_entry=peak_price_since_entry,
                    )
                    if self.strict_mode and self.strict_exit_style == "upgraded"
                    else self._resolve_legacy_strict_exit_decision(summary=summary, bars_since_entry=bars_since_entry)
                    if self.strict_mode
                    else self._resolve_non_strict_exit_decision(summary)
                )

                if exit_decision["should_exit"]:
                    execution = self._resolve_execution(next_snapshot, SignalSide.SELL)
                    fill_size = min(inventory, self.per_trade_size * execution["fill_ratio"])
                    if execution["price"] is not None and fill_size > 0:
                        notional = execution["price"] * fill_size
                        cash += notional
                        inventory -= fill_size
                        trade_pnl = (execution["price"] - average_entry_price) * fill_size
                        realized_pnl += trade_pnl
                        closing_trade_pnls.append(trade_pnl)
                        if inventory == 0:
                            average_entry_price = 0.0
                            bars_since_entry = None
                            peak_price_since_entry = None
                            cooldown_remaining = self.cooldown_bars_after_exit
                        post_fill_move_bps = self._post_fill_move_bps(
                            history=history,
                            fill_index=index + 1,
                            fill_price=execution["price"],
                            side=SignalSide.SELL,
                        )
                        trades.append(
                            BacktestTrade(
                                timestamp=next_snapshot.candle.timestamp,
                                strategy_name="ensemble",
                                side=SignalSide.SELL,
                                price=execution["price"],
                                size=fill_size,
                                pnl_after_trade=realized_pnl,
                                metadata={
                                    "fill_ratio": execution["fill_ratio"],
                                    "quote_source": execution["quote_source"],
                                    "spread_bps": execution["spread_bps"],
                                    "realized_pnl_delta": trade_pnl,
                                    "decision_summary": summary,
                                    "exit_reason": exit_decision["reason"],
                                    "exit_context": exit_decision["context"],
                                    "mid_at_fill": execution["mid_at_fill"],
                                    "spread_cost_bps": execution["spread_cost_bps"],
                                    "slippage_cost_bps": execution["slippage_cost_bps"],
                                    "fee_cost_bps": execution["fee_cost_bps"],
                                    "post_fill_move_bps": post_fill_move_bps,
                                    "notional": notional,
                                },
                            )
                        )
                elif exit_decision["blocked_reason"]:
                    blocked_exit_reasons[exit_decision["blocked_reason"]] += 1

            equity = cash + inventory * mark_price
            peak_equity = max(peak_equity, equity)
            if peak_equity > 0:
                max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity)

            total_inventory += inventory
            total_inventory_notional += inventory * mark_price
            if inventory > 0:
                bars_with_inventory += 1
                if bars_since_entry is None:
                    bars_since_entry = 0
                else:
                    bars_since_entry += 1
            max_inventory = max(max_inventory, inventory)
            if cooldown_remaining > 0 and inventory == 0:
                cooldown_remaining -= 1

            self._merge_blocked_reasons(summary, blocked_entry_reasons)

        ending_cash = cash
        final_mark = history[-1].mid_price if history else 0.0
        mark_to_market_equity = cash + inventory * final_mark
        bar_count = max(len(history) - 1, 1)
        metrics = self._build_metrics(
            trades=trades,
            closing_trade_pnls=closing_trade_pnls,
            average_inventory=total_inventory / bar_count,
            average_inventory_notional=total_inventory_notional / bar_count,
            exposure_ratio=bars_with_inventory / bar_count,
            max_inventory=max_inventory,
        )
        cost_attribution = self._build_cost_attribution(trades=trades, realized_pnl=realized_pnl)
        return BacktestResult(
            starting_cash=self.starting_cash,
            ending_cash=ending_cash,
            realized_pnl=realized_pnl,
            mark_to_market_equity=mark_to_market_equity,
            max_drawdown=max_drawdown,
            trades=trades,
            metadata={
                "ending_inventory": inventory,
                "average_entry_price": average_entry_price,
                "metrics": metrics,
                "cost_attribution": cost_attribution,
                "execution_model": self._execution_model_metadata(),
                "strict_mode": {
                    "enabled": self.strict_mode,
                    "min_entry_confidence": self.min_entry_confidence,
                    "min_buy_score": self.min_buy_score,
                    "min_buy_sell_score_gap": self.min_buy_sell_score_gap,
                    "min_buy_signal_count": self.min_buy_signal_count,
                    "strict_require_confirmers": self.strict_require_confirmers,
                    "strict_min_confirmers": self.strict_min_confirmers,
                    "strict_confirmer_buy_bonus": self.strict_confirmer_buy_bonus,
                    "strict_confirmer_sell_penalty": self.strict_confirmer_sell_penalty,
                    "strict_min_entry_score": self.strict_min_entry_score,
                    "min_hold_bars": self.min_hold_bars,
                    "cooldown_bars_after_exit": self.cooldown_bars_after_exit,
                    "strict_max_hold_bars": self.strict_max_hold_bars,
                    "strict_fail_exit_bars": self.strict_fail_exit_bars,
                    "strict_fail_exit_pnl_bps": self.strict_fail_exit_pnl_bps,
                    "strict_take_profit_bars": self.strict_take_profit_bars,
                    "strict_take_profit_pnl_bps": self.strict_take_profit_pnl_bps,
                    "strict_profit_giveback_bps": self.strict_profit_giveback_bps,
                    "strict_extended_hold_bars": self.strict_extended_hold_bars,
                    "strict_extended_hold_exit_gap": self.strict_extended_hold_exit_gap,
                    "estimated_round_trip_cost_bps": self.estimated_round_trip_cost_bps,
                    "min_expected_edge_bps": self.min_expected_edge_bps,
                    "edge_cost_buffer_bps": self.edge_cost_buffer_bps,
                    "blocked_entries": blocked_entry_reasons,
                    "blocked_exits": blocked_exit_reasons,
                    "strict_long_entry_led": self.strict_long_entry_led,
                    "strict_exit_style": self.strict_exit_style,
                    "gating": {
                        "enable_maturity_gating": self.enable_maturity_gating,
                        "enable_microstructure_gating": self.enable_microstructure_gating,
                        "strict_min_time_to_resolution_hours": self.strict_min_time_to_resolution_hours,
                        "strict_max_time_to_resolution_hours": self.strict_max_time_to_resolution_hours,
                        "strict_min_time_since_open_hours": self.strict_min_time_since_open_hours,
                        "strict_quote_lookback_bars": self.strict_quote_lookback_bars,
                        "strict_min_quote_availability_ratio": self.strict_min_quote_availability_ratio,
                        "strict_max_avg_spread_bps": self.strict_max_avg_spread_bps,
                        "strict_max_current_spread_bps": self.strict_max_current_spread_bps,
                        "strict_max_wide_spread_rate": self.strict_max_wide_spread_rate,
                        "strict_wide_spread_bps": self.strict_wide_spread_bps,
                        "microstructure_proxy_policy": self.microstructure_proxy_policy,
                    },
                },
                "microstructure_run_summary": self._summarize_microstructure_run(history),
            },
        )

    def _summarize_microstructure_run(self, history: list[MarketSnapshot]) -> dict[str, Any]:
        metrics = compute_microstructure_metrics(
            history,
            lookback_bars=len(history),
            wide_spread_bps=self.strict_wide_spread_bps,
            proxy_policy=self.microstructure_proxy_policy,
        )
        return {
            "source_mode": metrics.source_mode,
            "proxy_policy": self.microstructure_proxy_policy,
            "proxy_enabled": metrics.proxy_enabled,
            "lookback_bars": metrics.lookback_bars,
            "quote_count": metrics.quote_count,
            "real_quote_count": metrics.real_quote_count,
            "proxy_quote_count": metrics.proxy_quote_count,
            "quote_availability_ratio": metrics.quote_availability_ratio,
            "real_quote_availability_ratio": metrics.real_quote_availability_ratio,
            "avg_spread_bps": metrics.avg_spread_bps,
            "observed_spread_bps": metrics.observed_spread_bps,
            "wide_spread_rate": metrics.wide_spread_rate,
            "notes": metrics.notes,
        }

    def _resolve_non_strict_exit_decision(self, summary: dict[str, Any]) -> dict[str, Any]:
        if float(summary["sell_score"]) > float(summary["buy_score"]):
            return {"should_exit": True, "reason": "sell_signal", "context": {}, "blocked_reason": None}
        return {"should_exit": False, "reason": None, "context": {}, "blocked_reason": None}

    def _resolve_legacy_strict_exit_decision(self, *, summary: dict[str, Any], bars_since_entry: int | None) -> dict[str, Any]:
        bars_held = int(bars_since_entry or 0)
        context = {
            "bars_held": bars_held,
            "sell_minus_buy": float(summary["sell_score"]) - float(summary["buy_score"]),
            "required_exit_gap": self.min_buy_sell_score_gap,
        }
        if bars_held < self.min_hold_bars:
            return {"should_exit": False, "reason": None, "context": context, "blocked_reason": "min_hold"}
        if float(summary["sell_score"]) - float(summary["buy_score"]) >= self.min_buy_sell_score_gap:
            return {"should_exit": True, "reason": "legacy_sell_gap", "context": context, "blocked_reason": None}
        if float(summary["sell_score"]) > float(summary["buy_score"]):
            return {"should_exit": True, "reason": "legacy_sell_signal", "context": context, "blocked_reason": None}
        return {"should_exit": False, "reason": None, "context": context, "blocked_reason": "weak_exit"}

    def _resolve_strict_exit_decision(
        self,
        *,
        summary: dict[str, Any],
        snapshot: MarketSnapshot,
        bars_since_entry: int | None,
        average_entry_price: float,
        peak_price_since_entry: float | None,
    ) -> dict[str, Any]:
        bars_held = int(bars_since_entry or 0)
        sell_minus_buy = float(summary["sell_score"]) - float(summary["buy_score"])
        pnl_bps = self._price_move_bps(snapshot.mid_price, average_entry_price)
        peak_pnl_bps = self._price_move_bps((peak_price_since_entry or snapshot.mid_price), average_entry_price)
        giveback_bps = max(0.0, peak_pnl_bps - pnl_bps)
        required_gap = max(self.min_buy_sell_score_gap, 0.15)
        if bars_held >= self.strict_extended_hold_bars:
            required_gap = min(required_gap, self.strict_extended_hold_exit_gap)

        context = {
            "bars_held": bars_held,
            "pnl_bps": pnl_bps,
            "peak_pnl_bps": peak_pnl_bps,
            "profit_giveback_bps": giveback_bps,
            "sell_minus_buy": sell_minus_buy,
            "required_exit_gap": required_gap,
        }

        if bars_held < self.min_hold_bars:
            return {"should_exit": False, "reason": None, "context": context, "blocked_reason": "min_hold"}
        if self.strict_max_hold_bars > 0 and bars_held >= self.strict_max_hold_bars:
            return {"should_exit": True, "reason": "max_hold", "context": context, "blocked_reason": None}
        if self.strict_fail_exit_bars > 0 and bars_held >= self.strict_fail_exit_bars and pnl_bps <= self.strict_fail_exit_pnl_bps:
            return {"should_exit": True, "reason": "fail_exit", "context": context, "blocked_reason": None}
        if (
            self.strict_take_profit_bars > 0
            and bars_held >= self.strict_take_profit_bars
            and peak_pnl_bps >= self.strict_take_profit_pnl_bps
            and giveback_bps >= self.strict_profit_giveback_bps
        ):
            return {"should_exit": True, "reason": "profit_protect", "context": context, "blocked_reason": None}
        if sell_minus_buy >= required_gap:
            reason = "sell_signal"
            if bars_held >= self.strict_extended_hold_bars and required_gap < max(self.min_buy_sell_score_gap, 0.15):
                reason = "extended_hold_sell_signal"
            return {"should_exit": True, "reason": reason, "context": context, "blocked_reason": None}

        blocked_reason = "extended_hold_weak_exit" if bars_held >= self.strict_extended_hold_bars else "weak_exit"
        return {"should_exit": False, "reason": None, "context": context, "blocked_reason": blocked_reason}

    @staticmethod
    def _price_move_bps(current_price: float, entry_price: float) -> float:
        if entry_price <= 0:
            return 0.0
        return ((current_price - entry_price) / entry_price) * 10_000

    def _summarize_signals(self, history: list[MarketSnapshot]) -> dict[str, float | int | dict | None]:
        buy_score = 0.0
        sell_score = 0.0
        buy_signal_count = 0
        sell_signal_count = 0
        strongest_buy_confidence = 0.0
        expected_edge_bps = 0.0
        long_entry_side: SignalSide | None = None
        long_entry_confidence: float = 0.0
        long_entry_expected_edge_bps: float = 0.0
        buy_confirmer_count = 0
        sell_confirmer_count = 0

        for strategy in self.strategies:
            signal = strategy.generate_signal(history)
            weighted_confidence = signal.confidence
            if signal.side == SignalSide.BUY:
                buy_score += weighted_confidence
                buy_signal_count += 1
                strongest_buy_confidence = max(strongest_buy_confidence, signal.confidence)
                expected_edge_bps = max(expected_edge_bps, float(signal.metadata.get("expected_edge_bps") or 0.0))
            elif signal.side == SignalSide.SELL:
                sell_score += weighted_confidence
                sell_signal_count += 1

            if getattr(strategy, "name", "") == "long_entry":
                long_entry_side = signal.side
                long_entry_confidence = signal.confidence
                long_entry_expected_edge_bps = float(signal.metadata.get("expected_edge_bps") or 0.0)
            elif signal.side == SignalSide.BUY:
                buy_confirmer_count += 1
            elif signal.side == SignalSide.SELL:
                sell_confirmer_count += 1

        snapshot = history[-1]
        observed_spread_bps = 0.0
        if snapshot.best_bid is not None and snapshot.best_ask is not None and snapshot.mid_price > 0:
            observed_spread_bps = ((snapshot.best_ask - snapshot.best_bid) / snapshot.mid_price) * 10_000

        # New: compute maturity + microstructure metrics
        ms = compute_microstructure_metrics(
            history,
            lookback_bars=self.strict_quote_lookback_bars,
            wide_spread_bps=self.strict_wide_spread_bps,
            proxy_policy=self.microstructure_proxy_policy,
        )
        ttr = compute_time_to_resolution_hours(now=snapshot.candle.timestamp, snapshot=snapshot)

        required_edge_bps = max(
            self.min_expected_edge_bps,
            self.estimated_round_trip_cost_bps + self.edge_cost_buffer_bps + observed_spread_bps,
        )
        return {
            "buy_score": buy_score,
            "sell_score": sell_score,
            "buy_signal_count": buy_signal_count,
            "sell_signal_count": sell_signal_count,
            "lead_confidence": strongest_buy_confidence,
            "expected_edge_bps": expected_edge_bps,
            "required_edge_bps": required_edge_bps,
            "long_entry_side": (long_entry_side.value if long_entry_side is not None else None),
            "long_entry_confidence": long_entry_confidence,
            "long_entry_expected_edge_bps": long_entry_expected_edge_bps,
            "buy_confirmer_count": buy_confirmer_count,
            "sell_confirmer_count": sell_confirmer_count,
            "blocked_reason": None,
            "microstructure": {
                "observed_spread_bps": ms.observed_spread_bps,
                "avg_spread_bps": ms.avg_spread_bps,
                "quote_availability_ratio": ms.quote_availability_ratio,
                "real_quote_availability_ratio": ms.real_quote_availability_ratio,
                "quote_count": ms.quote_count,
                "real_quote_count": ms.real_quote_count,
                "proxy_quote_count": ms.proxy_quote_count,
                "source_mode": ms.source_mode,
                "proxy_enabled": ms.proxy_enabled,
                "wide_spread_rate": ms.wide_spread_rate,
                "lookback_bars": ms.lookback_bars,
                "wide_spread_threshold_bps": self.strict_wide_spread_bps,
                "notes": ms.notes,
            },
            "maturity": ttr,
        }

    def _resolve_decision(self, *, summary: dict, snapshot: MarketSnapshot, inventory: float) -> SignalSide:
        buy_score = float(summary["buy_score"])
        sell_score = float(summary["sell_score"])
        if buy_score <= 0 and sell_score <= 0:
            return SignalSide.HOLD

        if inventory == 0:
            if self.strict_mode:
                # Strict: first gate on maturity/microstructure if enabled
                # Maturity gate
                if self.enable_maturity_gating:
                    maturity = summary.get("maturity") or {}
                    ttr_hours = maturity.get("time_to_resolution_hours")
                    since_open = maturity.get("time_since_open_hours")
                    if ttr_hours is not None:
                        if self.strict_min_time_to_resolution_hours is not None and ttr_hours < self.strict_min_time_to_resolution_hours:
                            summary["blocked_reason"] = "maturity_too_soon"
                            return SignalSide.HOLD
                        if self.strict_max_time_to_resolution_hours is not None and ttr_hours > self.strict_max_time_to_resolution_hours:
                            summary["blocked_reason"] = "maturity_too_far"
                            return SignalSide.HOLD
                    if since_open is not None and self.strict_min_time_since_open_hours is not None and since_open < self.strict_min_time_since_open_hours:
                        summary["blocked_reason"] = "market_too_new"
                        return SignalSide.HOLD

                # Microstructure gate
                if self.enable_microstructure_gating:
                    ms = summary.get("microstructure") or {}
                    obs_spread = ms.get("observed_spread_bps")
                    avg_spread = ms.get("avg_spread_bps")
                    avail = float(ms.get("quote_availability_ratio") or 0.0)
                    quote_count = int(ms.get("quote_count") or 0)
                    wide_rate = float(ms.get("wide_spread_rate") or 0.0)
                    if quote_count >= self.strict_min_quote_observations:
                        if obs_spread is not None and obs_spread > self.strict_max_current_spread_bps:
                            summary["blocked_reason"] = "spread_too_wide"
                            return SignalSide.HOLD
                        if avg_spread is not None and avg_spread > self.strict_max_avg_spread_bps:
                            summary["blocked_reason"] = "avg_spread_too_wide"
                            return SignalSide.HOLD
                        if avail < self.strict_min_quote_availability_ratio:
                            summary["blocked_reason"] = "insufficient_quotes"
                            return SignalSide.HOLD
                        if wide_rate > self.strict_max_wide_spread_rate:
                            summary["blocked_reason"] = "wide_spread_rate"
                            return SignalSide.HOLD

                if not self.strict_long_entry_led:
                    if int(summary.get("buy_signal_count") or 0) < self.min_buy_signal_count:
                        summary["blocked_reason"] = "confirmers"
                        return SignalSide.HOLD
                    if buy_score < self.min_buy_score or (buy_score - sell_score) < self.min_buy_sell_score_gap:
                        summary["blocked_reason"] = "entry_score"
                        return SignalSide.HOLD
                    return SignalSide.BUY

                long_entry_side = summary.get("long_entry_side")
                if long_entry_side is None:
                    summary["blocked_reason"] = "long_entry_missing"
                    return SignalSide.HOLD
                if long_entry_side != SignalSide.BUY.value:
                    summary["blocked_reason"] = "long_entry_not_buy"
                    return SignalSide.HOLD
                long_entry_confidence = float(summary.get("long_entry_confidence") or 0.0)
                if long_entry_confidence < self.min_entry_confidence:
                    summary["blocked_reason"] = "long_entry_confidence"
                    return SignalSide.HOLD
                buy_confirmers = int(summary.get("buy_confirmer_count") or 0)
                sell_confirmers = int(summary.get("sell_confirmer_count") or 0)
                if self.strict_require_confirmers and buy_confirmers < self.strict_min_confirmers:
                    summary["blocked_reason"] = "confirmers"
                    return SignalSide.HOLD
                strict_entry_score = (
                    long_entry_confidence
                    + buy_confirmers * self.strict_confirmer_buy_bonus
                    - sell_confirmers * self.strict_confirmer_sell_penalty
                )
                summary["strict_entry_score"] = strict_entry_score
                if strict_entry_score < self.strict_min_entry_score:
                    summary["blocked_reason"] = "entry_score"
                    return SignalSide.HOLD
                long_expected = float(summary.get("long_entry_expected_edge_bps") or 0.0)
                required_edge = float(summary.get("required_edge_bps") or 0.0)
                if long_expected < required_edge:
                    summary["blocked_reason"] = "edge_vs_cost"
                    return SignalSide.HOLD
                return SignalSide.BUY
            if buy_score > sell_score:
                return SignalSide.BUY

        if sell_score > buy_score and inventory > 0:
            return SignalSide.SELL
        return SignalSide.HOLD

    def _merge_blocked_reasons(self, summary: dict, blocked_entry_reasons: dict[str, int]) -> None:
        reason = summary.get("blocked_reason")
        if reason in blocked_entry_reasons:
            blocked_entry_reasons[reason] += 1

    def _resolve_execution(self, snapshot: MarketSnapshot, side: SignalSide) -> dict[str, float | str | None]:
        mid = snapshot.mid_price
        bid = snapshot.best_bid
        ask = snapshot.best_ask
        spread_bps: float | None = None
        if bid is not None and ask is not None and mid > 0:
            spread_bps = ((ask - bid) / mid) * 10_000

        same_side_quote = ask if side == SignalSide.BUY else bid
        quote_source = "quote"
        fill_ratio = 1.0
        base_price: float | None = same_side_quote

        if base_price is None:
            quote_source = "mid_fallback"
            fill_ratio = max(0.0, min(1.0, self.missing_quote_fill_ratio))
            if fill_ratio > 0:
                half_spread = mid * (self.fallback_half_spread_bps / 10_000)
                base_price = mid + half_spread if side == SignalSide.BUY else mid - half_spread
        elif spread_bps is not None and spread_bps > self.max_spread_bps:
            quote_source = "wide_spread_quote"
            fill_ratio = max(0.0, min(1.0, self.wide_spread_fill_ratio))

        if base_price is None or fill_ratio <= 0:
            return {
                "price": None,
                "fill_ratio": 0.0,
                "quote_source": quote_source,
                "spread_bps": spread_bps,
                "mid_at_fill": mid,
                "spread_cost_bps": 0.0,
                "slippage_cost_bps": 0.0,
                "fee_cost_bps": 0.0,
            }

        spread_cost_bps = 0.0
        if mid > 0:
            if side == SignalSide.BUY:
                spread_cost_bps = max(0.0, (base_price - mid) / mid * 10_000)
            else:
                spread_cost_bps = max(0.0, (mid - base_price) / mid * 10_000)

        slippage_multiplier = 1 + (self.slippage_bps / 10_000)
        fee_multiplier = 1 + (self.fee_bps / 10_000)
        if side == SignalSide.BUY:
            fill_price = base_price * slippage_multiplier * fee_multiplier
        else:
            fill_price = base_price / slippage_multiplier / fee_multiplier
        fill_price = min(max(fill_price, 0.01), 0.99)
        return {
            "price": fill_price,
            "fill_ratio": fill_ratio,
            "quote_source": quote_source,
            "spread_bps": spread_bps,
            "mid_at_fill": mid,
            "spread_cost_bps": spread_cost_bps,
            "slippage_cost_bps": self.slippage_bps,
            "fee_cost_bps": self.fee_bps,
        }

    def _build_metrics(
        self,
        trades: list[BacktestTrade],
        closing_trade_pnls: list[float],
        average_inventory: float,
        average_inventory_notional: float,
        exposure_ratio: float,
        max_inventory: float,
    ) -> dict[str, float | int | None]:
        wins = [pnl for pnl in closing_trade_pnls if pnl > 0]
        losses = [pnl for pnl in closing_trade_pnls if pnl < 0]
        round_trip_count = len(closing_trade_pnls)
        win_rate = (len(wins) / round_trip_count) if round_trip_count else None
        average_win = (sum(wins) / len(wins)) if wins else None
        average_loss = (sum(losses) / len(losses)) if losses else None
        expectancy = (sum(closing_trade_pnls) / round_trip_count) if round_trip_count else None
        buy_trade_count = sum(1 for trade in trades if trade.side == SignalSide.BUY)
        sell_trade_count = sum(1 for trade in trades if trade.side == SignalSide.SELL)
        filled_volume = sum(trade.size for trade in trades)
        return {
            "trade_count": len(trades),
            "buy_trade_count": buy_trade_count,
            "sell_trade_count": sell_trade_count,
            "round_trip_count": round_trip_count,
            "filled_volume": filled_volume,
            "win_rate": win_rate,
            "average_win": average_win,
            "average_loss": average_loss,
            "expectancy": expectancy,
            "average_inventory": average_inventory,
            "average_inventory_notional": average_inventory_notional,
            "max_inventory": max_inventory,
            "time_in_market_ratio": exposure_ratio,
        }

    def _empty_metrics(self) -> dict[str, float | int | None]:
        return {
            "trade_count": 0,
            "buy_trade_count": 0,
            "sell_trade_count": 0,
            "round_trip_count": 0,
            "filled_volume": 0.0,
            "win_rate": None,
            "average_win": None,
            "average_loss": None,
            "expectancy": None,
            "average_inventory": 0.0,
            "average_inventory_notional": 0.0,
            "max_inventory": 0.0,
            "time_in_market_ratio": 0.0,
        }

    def _maybe_family_filter_skip(self) -> BacktestResult | None:
        if self.market_filter is None or not self.market_metadata:
            return None
        result = self.market_filter.evaluate_family_only(self.market_metadata)
        if result.eligible:
            return None
        family_info = (result.metrics or {}).get("market_family") or {}
        return BacktestResult(
            starting_cash=self.starting_cash,
            ending_cash=self.starting_cash,
            realized_pnl=0.0,
            mark_to_market_equity=self.starting_cash,
            max_drawdown=0.0,
            trades=[],
            metadata={
                "ending_inventory": 0.0,
                "skipped_by_family_filter": True,
                "family_filter_reason": result.reason,
                "market_family": family_info.get("family"),
                "metrics": self._empty_metrics(),
                "cost_attribution": self._build_cost_attribution(trades=[], realized_pnl=0.0),
                "execution_model": self._execution_model_metadata(),
            },
        )

    def _execution_model_metadata(self) -> dict[str, float | str | bool | int]:
        return {
            "fill_timing": "next_bar",
            "price_source": "same_side_quote_then_mid_fallback",
            "slippage_bps": self.slippage_bps,
            "fallback_half_spread_bps": self.fallback_half_spread_bps,
            "fee_bps": self.fee_bps,
            "adverse_selection_horizon_bars": self.adverse_selection_horizon_bars,
            "max_spread_bps": self.max_spread_bps,
            "missing_quote_fill_ratio": self.missing_quote_fill_ratio,
            "wide_spread_fill_ratio": self.wide_spread_fill_ratio,
            "strict_mode": self.strict_mode,
            "microstructure_proxy_policy": self.microstructure_proxy_policy,
            "min_hold_bars": self.min_hold_bars,
            "cooldown_bars_after_exit": self.cooldown_bars_after_exit,
        }

    def _post_fill_move_bps(
        self,
        history: list[MarketSnapshot],
        fill_index: int,
        fill_price: float,
        side: SignalSide,
    ) -> float | None:
        target_index = fill_index + self.adverse_selection_horizon_bars
        if target_index >= len(history) or fill_price <= 0:
            return None
        future_mid = history[target_index].mid_price
        if future_mid is None or future_mid <= 0:
            return None
        if side == SignalSide.BUY:
            return (future_mid - fill_price) / fill_price * 10_000
        return (fill_price - future_mid) / fill_price * 10_000

    def _build_cost_attribution(
        self,
        trades: list[BacktestTrade],
        realized_pnl: float,
    ) -> dict[str, float | int | None]:
        entry_trades = [t for t in trades if t.side == SignalSide.BUY]
        exit_trades = [t for t in trades if t.side == SignalSide.SELL]

        def _avg(xs: list[float]) -> float | None:
            return sum(xs) / len(xs) if xs else None

        def _sum_cost_usd(ts: list[BacktestTrade], key: str) -> float:
            total = 0.0
            for t in ts:
                bps = t.metadata.get(key) or 0.0
                notional = t.metadata.get("notional") or (t.price * t.size)
                total += (bps / 10_000) * notional
            return total

        entry_spread_bps = [t.metadata.get("spread_cost_bps") for t in entry_trades if t.metadata.get("spread_cost_bps") is not None]
        exit_spread_bps = [t.metadata.get("spread_cost_bps") for t in exit_trades if t.metadata.get("spread_cost_bps") is not None]
        post_fill_moves = [t.metadata.get("post_fill_move_bps") for t in trades if t.metadata.get("post_fill_move_bps") is not None]

        spread_usd = _sum_cost_usd(entry_trades, "spread_cost_bps") + _sum_cost_usd(exit_trades, "spread_cost_bps")
        slippage_usd = _sum_cost_usd(entry_trades, "slippage_cost_bps") + _sum_cost_usd(exit_trades, "slippage_cost_bps")
        fees_usd = _sum_cost_usd(entry_trades, "fee_cost_bps") + _sum_cost_usd(exit_trades, "fee_cost_bps")
        total_trade_costs_usd = spread_usd + slippage_usd + fees_usd
        gross_pnl = realized_pnl + total_trade_costs_usd

        adverse_count = sum(1 for m in post_fill_moves if m is not None and m < 0)
        return {
            "gross_pnl": gross_pnl,
            "net_pnl": realized_pnl,
            "total_trade_costs_usd": total_trade_costs_usd,
            "spread_cost_usd": spread_usd,
            "slippage_cost_usd": slippage_usd,
            "fees_usd": fees_usd,
            "average_entry_spread_bps": _avg([float(x) for x in entry_spread_bps]),
            "average_exit_spread_bps": _avg([float(x) for x in exit_spread_bps]),
            "slippage_bps": self.slippage_bps,
            "fee_bps": self.fee_bps,
            "average_post_fill_move_bps": _avg([float(x) for x in post_fill_moves]),
            "adverse_fill_count": adverse_count,
            "measured_post_fill_count": len(post_fill_moves),
            "adverse_fill_ratio": (adverse_count / len(post_fill_moves)) if post_fill_moves else None,
            "adverse_selection_horizon_bars": self.adverse_selection_horizon_bars,
        }
