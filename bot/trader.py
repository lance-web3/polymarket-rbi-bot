from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.models import Candle, MarketSnapshot, OrderIntent, Position, SignalSide, StrategySignal
from bot.state import LiveStateStore
from strategies.base import BaseStrategy
from polymarket_rbi_bot.microstructure import compute_microstructure_metrics, compute_time_to_resolution_hours

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, PartialCreateOrderOptions
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:  # pragma: no cover - import is validated at runtime
    ClobClient = None
    ApiCreds = None
    OrderArgs = None
    OrderType = None
    PartialCreateOrderOptions = None
    BUY = "BUY"
    SELL = "SELL"


@dataclass(slots=True)
class PolymarketTrader:
    config: BotConfig
    strategies: list[BaseStrategy]
    client: Any = None
    public_client: Any = None
    state_store: LiveStateStore | None = None

    def __post_init__(self) -> None:
        if self.state_store is None:
            self.state_store = LiveStateStore(self.config.state_path)

    @property
    def positions(self) -> dict[str, Position]:
        return self.state_store.positions if self.state_store is not None else {}

    def connect_public(self) -> Any:
        if ClobClient is None:
            raise RuntimeError("py-clob-client is not installed. Run `pip install -r requirements.txt` first.")
        if self.public_client is None:
            self.public_client = ClobClient(self.config.host, chain_id=self.config.chain_id)
        return self.public_client

    def connect(self) -> Any:
        if ClobClient is None:
            raise RuntimeError("py-clob-client is not installed. Run `pip install -r requirements.txt` first.")

        if self.client is not None:
            return self.client

        if self.config.private_key:
            temp_client = ClobClient(
                self.config.host,
                key=self.config.private_key,
                chain_id=self.config.chain_id,
                signature_type=self.config.signature_type,
                funder=self.config.funder_address,
            )
            api_creds = temp_client.create_or_derive_api_creds()
        elif self.config.api_key and self.config.api_secret and self.config.api_passphrase:
            api_creds = ApiCreds(
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.api_passphrase,
            )
        else:
            raise RuntimeError("Trading requires PRIVATE_KEY or pre-derived API credentials.")

        self.client = ClobClient(
            self.config.host,
            key=self.config.private_key,
            chain_id=self.config.chain_id,
            creds=api_creds,
            signature_type=self.config.signature_type,
            funder=self.config.funder_address,
        )
        return self.client

    def _strategy_weight(self, strategy_name: str) -> float:
        return {
            "long_entry": self.config.long_entry_weight,
            "macd": self.config.macd_weight,
            "rsi": self.config.rsi_weight,
            "cvd": self.config.cvd_weight,
        }.get(strategy_name, 1.0)

    def summarize_signals(self, history: list[MarketSnapshot]) -> dict[str, Any]:
        if not history:
            return {
                "buy_score": 0.0,
                "sell_score": 0.0,
                "buy_signal_count": 0,
                "sell_signal_count": 0,
                "strongest_signal": None,
                "signals": [],
                "expected_edge_bps": 0.0,
                "observed_spread_bps": None,
            }

        buy_score = 0.0
        sell_score = 0.0
        buy_signal_count = 0
        sell_signal_count = 0
        strongest_signal: StrategySignal | None = None
        strongest_buy_signal: StrategySignal | None = None
        strongest_sell_signal: StrategySignal | None = None
        long_entry_signal: StrategySignal | None = None
        signals: list[dict[str, Any]] = []
        strongest_expected_edge_bps = 0.0
        long_entry_expected_edge_bps = 0.0
        snapshot = history[-1]
        observed_spread_bps = None
        if snapshot.best_bid is not None and snapshot.best_ask is not None and snapshot.mid_price > 0:
            observed_spread_bps = ((snapshot.best_ask - snapshot.best_bid) / snapshot.mid_price) * 10_000

        for strategy in self.strategies:
            signal = strategy.generate_signal(history)
            weight = self._strategy_weight(strategy.name)
            weighted_confidence = signal.confidence * weight
            expected_edge_bps = float(signal.metadata.get("expected_edge_bps") or 0.0)
            signals.append(
                {
                    "strategy": strategy.name,
                    "side": signal.side.value,
                    "confidence": signal.confidence,
                    "weight": weight,
                    "weighted_confidence": weighted_confidence,
                    "reason": signal.reason,
                    "expected_edge_bps": expected_edge_bps,
                    "metadata": signal.metadata,
                }
            )
            if strongest_signal is None or weighted_confidence > (strongest_signal.confidence * self._strategy_weight(strongest_signal.strategy_name)):
                strongest_signal = signal
            if strategy.name == "long_entry":
                long_entry_signal = signal
                long_entry_expected_edge_bps = float(signal.metadata.get("expected_edge_bps") or 0.0)
            if signal.side == SignalSide.BUY:
                buy_score += weighted_confidence
                buy_signal_count += 1
                strongest_expected_edge_bps = max(strongest_expected_edge_bps, expected_edge_bps)
                if strongest_buy_signal is None or weighted_confidence > (strongest_buy_signal.confidence * self._strategy_weight(strongest_buy_signal.strategy_name)):
                    strongest_buy_signal = signal
            elif signal.side == SignalSide.SELL:
                sell_score += weighted_confidence
                sell_signal_count += 1
                if strongest_sell_signal is None or weighted_confidence > (strongest_sell_signal.confidence * self._strategy_weight(strongest_sell_signal.strategy_name)):
                    strongest_sell_signal = signal

        buy_confirmers = sum(
            1
            for signal in signals
            if signal["strategy"] != "long_entry" and signal["side"] == SignalSide.BUY.value
        )
        sell_confirmers = sum(
            1
            for signal in signals
            if signal["strategy"] != "long_entry" and signal["side"] == SignalSide.SELL.value
        )
        # Compute maturity + microstructure metrics for transparency/gating
        ms = compute_microstructure_metrics(
            history,
            lookback_bars=self.config.strict_quote_lookback_bars,
            wide_spread_bps=self.config.strict_wide_spread_bps,
        )
        from datetime import datetime, timezone
        now = snapshot.candle.timestamp
        maturity = compute_time_to_resolution_hours(now=now, snapshot=snapshot)

        return {
            "buy_score": buy_score,
            "sell_score": sell_score,
            "buy_signal_count": buy_signal_count,
            "sell_signal_count": sell_signal_count,
            "buy_confirmer_count": buy_confirmers,
            "sell_confirmer_count": sell_confirmers,
            "long_entry_signal": long_entry_signal,
            "strongest_signal": strongest_signal,
            "strongest_buy_signal": strongest_buy_signal,
            "strongest_sell_signal": strongest_sell_signal,
            "signals": signals,
            "expected_edge_bps": long_entry_expected_edge_bps,
            "observed_spread_bps": observed_spread_bps,
            "microstructure": {
                "observed_spread_bps": ms.observed_spread_bps,
                "avg_spread_bps": ms.avg_spread_bps,
                "quote_availability_ratio": ms.quote_availability_ratio,
                "wide_spread_rate": ms.wide_spread_rate,
                "lookback_bars": ms.lookback_bars,
                "wide_spread_threshold_bps": self.config.strict_wide_spread_bps,
            },
            "maturity": maturity,
        }

    def build_order_decision(self, token_id: str, history: list[MarketSnapshot], position: Position | None = None) -> tuple[OrderIntent | None, str | None, dict[str, Any]]:
        if not history:
            return None, "No history available", {"buy_score": 0.0, "sell_score": 0.0, "signals": []}

        snapshot = history[-1]
        position = position or Position(token_id=token_id)
        summary = self.summarize_signals(history)
        buy_score = float(summary["buy_score"])
        sell_score = float(summary["sell_score"])
        strongest_signal = summary["strongest_signal"]
        strongest_buy_signal = summary.get("strongest_buy_signal")
        strongest_sell_signal = summary.get("strongest_sell_signal")
        long_entry_signal = summary.get("long_entry_signal")

        if strongest_signal is None:
            return None, "No strategy signal available", summary

        edge = self.config.price_edge_bps / 10_000
        reference_signal = strongest_buy_signal or strongest_signal
        reference_price = reference_signal.price or snapshot.mid_price

        if self.config.strict_strategy_mode and position.quantity > 0:
            exit_decision = self._resolve_live_strict_exit(summary=summary, history=history, position=position)
            summary["strict_exit"] = exit_decision
            if exit_decision["should_exit"]:
                sell_reference = strongest_sell_signal or strongest_signal
                sell_reference_price = sell_reference.price or snapshot.mid_price
                price = max(min(sell_reference_price + edge, 0.99), 0.01)
                return (
                    OrderIntent(
                        token_id=token_id,
                        side=SignalSide.SELL,
                        price=price,
                        size=min(self.config.default_order_size, position.quantity),
                    ),
                    None,
                    summary,
                )
            return None, f"Strict mode: exit blocked ({exit_decision['reason']})", summary

        # Strict-mode: long-entry-led, independent of aggregate ensemble
        if self.config.strict_strategy_mode:
            # Gate on maturity/microstructure before evaluating strict long-entry
            if self.config.enable_maturity_gating:
                maturity = summary.get("maturity") or {}
                ttr_hours = maturity.get("time_to_resolution_hours")
                since_open = maturity.get("time_since_open_hours")
                if ttr_hours is not None:
                    if self.config.strict_min_time_to_resolution_hours is not None and ttr_hours < self.config.strict_min_time_to_resolution_hours:
                        return None, "Strict mode: maturity too soon (time-to-resolution)", summary
                    if self.config.strict_max_time_to_resolution_hours is not None and ttr_hours > self.config.strict_max_time_to_resolution_hours:
                        return None, "Strict mode: maturity too far (time-to-resolution)", summary
                if since_open is not None and self.config.strict_min_time_since_open_hours is not None and since_open < self.config.strict_min_time_since_open_hours:
                    return None, "Strict mode: market too new (since open)", summary

            if self.config.enable_microstructure_gating:
                ms = summary.get("microstructure") or {}
                obs_spread = ms.get("observed_spread_bps")
                avg_spread = ms.get("avg_spread_bps")
                avail = float(ms.get("quote_availability_ratio") or 0.0)
                quote_count = int(ms.get("quote_count") or 0)
                wide_rate = float(ms.get("wide_spread_rate") or 0.0)
                if quote_count >= self.config.strict_min_quote_observations:
                    if obs_spread is not None and obs_spread > self.config.strict_max_current_spread_bps:
                        return None, f"Strict mode: spread too wide now ({obs_spread:.1f} bps)", summary
                    if avg_spread is not None and avg_spread > self.config.strict_max_avg_spread_bps:
                        return None, f"Strict mode: avg spread too wide ({avg_spread:.1f} bps)", summary
                    if avail < self.config.strict_min_quote_availability_ratio:
                        return None, f"Strict mode: insufficient quote availability ({avail:.2f})", summary
                    if wide_rate > self.config.strict_max_wide_spread_rate:
                        return None, f"Strict mode: wide-spread rate too high ({wide_rate:.2f})", summary
            if long_entry_signal is None:
                return None, "Strict mode: long-entry signal unavailable", summary
            if long_entry_signal.side != SignalSide.BUY:
                return None, f"Strict mode: long-entry is {long_entry_signal.side.value}", summary
            if (long_entry_signal.confidence or 0.0) < self.config.min_entry_confidence:
                return None, f"Strict mode: long-entry confidence {long_entry_signal.confidence:.2f} below threshold", summary

            buy_confirmers = int(summary.get("buy_confirmer_count") or 0)
            sell_confirmers = int(summary.get("sell_confirmer_count") or 0)
            strict_entry_score = (
                float(long_entry_signal.confidence or 0.0)
                + buy_confirmers * self.config.strict_confirmer_buy_bonus
                - sell_confirmers * self.config.strict_confirmer_sell_penalty
            )
            summary["strict_entry_score"] = strict_entry_score
            summary["strict_long_entry_confidence"] = float(long_entry_signal.confidence or 0.0)
            summary["strict_long_entry_reason"] = long_entry_signal.reason
            if self.config.strict_require_confirmers and buy_confirmers < self.config.strict_min_confirmers:
                return None, f"Strict mode: only {buy_confirmers} confirmer(s)", summary
            if strict_entry_score < self.config.strict_min_entry_score:
                return None, f"Strict mode: entry score {strict_entry_score:.2f} below threshold", summary

            observed_spread_bps = float(summary.get("observed_spread_bps") or 0.0)
            expected_edge_bps = float(summary.get("expected_edge_bps") or 0.0)
            required_edge_bps = max(
                self.config.min_expected_edge_bps,
                self.config.estimated_round_trip_cost_bps + self.config.edge_cost_buffer_bps + observed_spread_bps,
            )
            summary["required_edge_bps"] = required_edge_bps
            summary["expected_edge_after_cost_bps"] = expected_edge_bps - required_edge_bps
            if expected_edge_bps < required_edge_bps:
                return None, (
                    f"Strict mode: expected edge {expected_edge_bps:.1f} bps below required {required_edge_bps:.1f} bps"
                ), summary

            reference_signal = long_entry_signal
            reference_price = reference_signal.price or snapshot.mid_price
            price = max(min(reference_price - edge, 0.99), 0.01)
            return (
                OrderIntent(
                    token_id=token_id,
                    side=SignalSide.BUY,
                    price=price,
                    size=self.config.default_order_size,
                ),
                None,
                summary,
            )

        buy_threshold = sell_score * self.config.buy_bias_multiplier
        if buy_score > 0 and (buy_score >= sell_score or (self.config.buy_only_mode and buy_score >= buy_threshold)):
            price = max(min(reference_price - edge, 0.99), 0.01)
            return (
                OrderIntent(
                    token_id=token_id,
                    side=SignalSide.BUY,
                    price=price,
                    size=self.config.default_order_size,
                ),
                None,
                summary,
            )

        if sell_score > buy_score and sell_score > 0:
            if self.config.buy_only_mode and position.quantity <= 0:
                strongest = strongest_sell_signal.strategy_name if strongest_sell_signal else "sell"
                return None, f"Sell signal suppressed by buy-only mode while flat ({strongest}-led)", summary
            sell_reference = strongest_sell_signal or strongest_signal
            sell_reference_price = sell_reference.price or snapshot.mid_price
            price = max(min(sell_reference_price + edge, 0.99), 0.01)
            return (
                OrderIntent(
                    token_id=token_id,
                    side=SignalSide.SELL,
                    price=price,
                    size=self.config.default_order_size,
                ),
                None,
                summary,
            )

        return None, "No dominant signal", summary

    def _resolve_live_strict_exit(self, *, summary: dict[str, Any], history: list[MarketSnapshot], position: Position) -> dict[str, Any]:
        if position.quantity <= 0 or position.average_price <= 0:
            return {"should_exit": False, "reason": "flat", "context": {}}

        snapshot = history[-1]
        entry_time = position.opened_at
        bars_held = 0
        since_entry = history
        if entry_time is not None:
            since_entry = [item for item in history if item.candle.timestamp >= entry_time]
            bars_held = max(len(since_entry) - 1, 0)
        peak_price = max((item.mid_price for item in since_entry), default=snapshot.mid_price)
        pnl_bps = self._price_move_bps(snapshot.mid_price, position.average_price)
        peak_pnl_bps = self._price_move_bps(peak_price, position.average_price)
        giveback_bps = max(0.0, peak_pnl_bps - pnl_bps)
        sell_minus_buy = float(summary.get("sell_score") or 0.0) - float(summary.get("buy_score") or 0.0)
        required_gap = max(self.config.min_buy_sell_score_gap, 0.15)
        if bars_held >= self.config.strict_extended_hold_bars:
            required_gap = min(required_gap, self.config.strict_extended_hold_exit_gap)

        context = {
            "bars_held": bars_held,
            "opened_at": entry_time.isoformat() if entry_time else None,
            "entry_price": position.average_price,
            "current_mid_price": snapshot.mid_price,
            "pnl_bps": pnl_bps,
            "peak_pnl_bps": peak_pnl_bps,
            "profit_giveback_bps": giveback_bps,
            "sell_minus_buy": sell_minus_buy,
            "required_exit_gap": required_gap,
        }

        if bars_held < self.config.min_hold_bars:
            return {"should_exit": False, "reason": "min_hold", "context": context}
        if self.config.strict_max_hold_bars > 0 and bars_held >= self.config.strict_max_hold_bars:
            return {"should_exit": True, "reason": "max_hold", "context": context}
        if self.config.strict_fail_exit_bars > 0 and bars_held >= self.config.strict_fail_exit_bars and pnl_bps <= self.config.strict_fail_exit_pnl_bps:
            return {"should_exit": True, "reason": "fail_exit", "context": context}
        if (
            self.config.strict_take_profit_bars > 0
            and bars_held >= self.config.strict_take_profit_bars
            and peak_pnl_bps >= self.config.strict_take_profit_pnl_bps
            and giveback_bps >= self.config.strict_profit_giveback_bps
        ):
            return {"should_exit": True, "reason": "profit_protect", "context": context}
        if sell_minus_buy >= required_gap:
            reason = "extended_hold_sell_signal" if bars_held >= self.config.strict_extended_hold_bars and required_gap < max(self.config.min_buy_sell_score_gap, 0.15) else "sell_signal"
            return {"should_exit": True, "reason": reason, "context": context}
        return {"should_exit": False, "reason": "weak_exit", "context": context}

    @staticmethod
    def _price_move_bps(current_price: float, entry_price: float) -> float:
        if entry_price <= 0:
            return 0.0
        return ((current_price - entry_price) / entry_price) * 10_000

    def build_order_intent(self, token_id: str, history: list[MarketSnapshot], position: Position | None = None) -> OrderIntent | None:
        intent, _, _ = self.build_order_decision(token_id, history, position)
        return intent

    def build_warmup_history(
        self,
        *,
        mid_price: float,
        best_bid: float | None = None,
        best_ask: float | None = None,
        periods: int = 50,
        interval_minutes: int = 5,
    ) -> list[MarketSnapshot]:
        now = datetime.now(tz=timezone.utc)
        history: list[MarketSnapshot] = []
        for offset in range(periods):
            timestamp = now - timedelta(minutes=(periods - offset) * interval_minutes)
            candle = Candle(
                timestamp=timestamp,
                open=mid_price,
                high=mid_price,
                low=mid_price,
                close=mid_price,
                volume=0.0,
            )
            history.append(
                MarketSnapshot(
                    candle=candle,
                    trades=[],
                    best_bid=best_bid,
                    best_ask=best_ask,
                    metadata={"synthetic": True},
                )
            )
        return history

    def fetch_market_metadata(self, condition_id: str) -> dict[str, Any]:
        client = self.connect_public()
        market = client.get_market(condition_id)
        return {
            "tick_size": str(market.get("minimum_tick_size", "0.01")),
            "neg_risk": bool(market.get("neg_risk", False)),
        }

    def refresh_exchange_state(self, *, token_id: str | None = None, condition_id: str | None = None) -> dict[str, Any]:
        if self.state_store is None:
            return {"ok": False, "message": "state store unavailable"}

        try:
            client = self.connect()
            orders = client.get_orders()
            if token_id is not None:
                token_id = str(token_id)
                orders = [order for order in orders if str(order.get("asset_id") or order.get("token_id") or order.get("market")) == token_id]
            self.state_store.replace_open_orders(orders)

            trades = client.get_trades()
            if token_id is not None:
                token_id = str(token_id)
                trades = [trade for trade in trades if str(trade.get("asset_id") or trade.get("token_id") or trade.get("market")) == token_id]
            applied_fills = 0
            for trade in trades:
                if self.state_store.apply_exchange_fill(trade):
                    applied_fills += 1

            message = f"reconciled {len(orders)} open orders and {applied_fills} new fills"
            self.state_store.mark_reconcile(status="ok", message=message, success=True)
            return {
                "ok": True,
                "message": message,
                "open_orders": len(orders),
                "new_fills": applied_fills,
                "positions": {
                    k: {
                        "token_id": v.token_id,
                        "quantity": v.quantity,
                        "average_price": v.average_price,
                        "opened_at": v.opened_at.isoformat() if v.opened_at else None,
                    }
                    for k, v in self.positions.items()
                },
            }
        except Exception as exc:
            message = f"exchange reconciliation failed: {exc}"
            self.state_store.mark_reconcile(status="error", message=message, success=False)
            return {"ok": False, "message": message}

    def place_limit_order(self, intent: OrderIntent) -> dict[str, Any]:
        client = self.connect()
        if OrderArgs is None or OrderType is None or PartialCreateOrderOptions is None:
            raise RuntimeError("py-clob-client order helpers are unavailable.")

        side = BUY if intent.side == SignalSide.BUY else SELL
        signed_order = client.create_order(
            OrderArgs(
                token_id=intent.token_id,
                price=intent.price,
                size=intent.size,
                side=side,
            ),
            PartialCreateOrderOptions(tick_size=intent.tick_size, neg_risk=intent.neg_risk),
        )
        response = client.post_order(signed_order, OrderType.GTC, post_only=intent.post_only)
        if self.state_store is not None:
            self.state_store.record_submitted_order(intent, response)
        return response
