from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.models import MarketSnapshot, OrderIntent, Position, SignalSide


@dataclass(slots=True)
class ExecutionGuardsResult:
    ok: bool
    reason: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RiskManager:
    config: BotConfig
    daily_realized_pnl: float = 0.0

    def validate_order(self, intent: OrderIntent, position: Position) -> tuple[bool, str]:
        notional = intent.price * intent.size
        if notional > self.config.max_notional_per_order:
            return False, "Order exceeds max notional per order"
        if self.daily_realized_pnl <= -abs(self.config.daily_loss_limit):
            return False, "Daily loss limit breached"

        projected_position = position.quantity
        if intent.side.value == "BUY":
            projected_position += intent.size
        elif intent.side.value == "SELL":
            projected_position -= intent.size

        if abs(projected_position) > self.config.max_position_size:
            return False, "Projected position exceeds max position size"
        if projected_position < 0:
            return False, "Order would create a net short position"
        if not (0 < intent.price < 1):
            return False, "Polymarket outcome prices must stay between 0 and 1"
        return True, "Order approved"

    def evaluate_execution_guards(
        self,
        *,
        intent: OrderIntent,
        position: Position,
        snapshot: MarketSnapshot,
        decision_ts: datetime | None,
        now: datetime | None = None,
        open_orders: list[dict[str, Any]] | None = None,
        all_open_orders: list[dict[str, Any]] | None = None,
        cooldowns: dict[str, Any] | None = None,
    ) -> ExecutionGuardsResult:
        now = now or datetime.now(tz=timezone.utc)
        metrics: dict[str, Any] = {
            "decision_ts": decision_ts.isoformat() if decision_ts else None,
            "decision_age_seconds": None,
            "freshness_trusted": decision_ts is not None,
            "spread_bps": None,
            "best_bid": snapshot.best_bid,
            "best_ask": snapshot.best_ask,
            "observed_mid_price": snapshot.mid_price,
            "intended_limit_price": intent.price,
            "price_deviation_bps_from_mid": None,
            "price_deviation_bps_from_same_side_quote": None,
            "open_orders_total": len(all_open_orders or []),
            "open_orders_for_token": len(open_orders or []),
            "last_submission_at": (cooldowns or {}).get("last_submission_at"),
            "last_fill_at": (cooldowns or {}).get("last_fill_at"),
            "submission_cooldown_remaining_seconds": None,
            "fill_cooldown_remaining_seconds": None,
            "projected_position": position.quantity + intent.size if intent.side == SignalSide.BUY else position.quantity - intent.size,
        }

        if decision_ts is None:
            if self.config.require_live_decision_ts:
                return ExecutionGuardsResult(False, "Live execution blocked: missing trusted decision timestamp", metrics)
        else:
            age_seconds = max((now - decision_ts).total_seconds(), 0.0)
            metrics["decision_age_seconds"] = age_seconds
            if age_seconds > self.config.max_decision_age_seconds:
                return ExecutionGuardsResult(
                    False,
                    f"Live execution blocked: decision snapshot is stale ({age_seconds:.1f}s > {self.config.max_decision_age_seconds:.1f}s)",
                    metrics,
                )

        if snapshot.best_bid is not None and snapshot.best_ask is not None and snapshot.mid_price > 0:
            spread_bps = ((snapshot.best_ask - snapshot.best_bid) / snapshot.mid_price) * 10_000
            metrics["spread_bps"] = spread_bps
            if spread_bps > self.config.max_spread_bps:
                return ExecutionGuardsResult(
                    False,
                    f"Live execution blocked: spread too wide ({spread_bps:.1f} bps > {self.config.max_spread_bps:.1f} bps)",
                    metrics,
                )

        if snapshot.mid_price > 0:
            deviation_mid_bps = abs(intent.price - snapshot.mid_price) / snapshot.mid_price * 10_000
            metrics["price_deviation_bps_from_mid"] = deviation_mid_bps
            if deviation_mid_bps > self.config.max_price_deviation_bps_from_mid:
                return ExecutionGuardsResult(
                    False,
                    f"Live execution blocked: limit price too far from observed mid ({deviation_mid_bps:.1f} bps > {self.config.max_price_deviation_bps_from_mid:.1f} bps)",
                    metrics,
                )

        same_side_quote = snapshot.best_bid if intent.side == SignalSide.BUY else snapshot.best_ask
        if same_side_quote is not None and same_side_quote > 0:
            deviation_quote_bps = abs(intent.price - same_side_quote) / same_side_quote * 10_000
            metrics["price_deviation_bps_from_same_side_quote"] = deviation_quote_bps
            if deviation_quote_bps > self.config.max_price_deviation_bps_from_quote:
                return ExecutionGuardsResult(
                    False,
                    f"Live execution blocked: limit price too far from same-side quote ({deviation_quote_bps:.1f} bps > {self.config.max_price_deviation_bps_from_quote:.1f} bps)",
                    metrics,
                )

        if metrics["open_orders_total"] >= self.config.max_open_orders_total:
            return ExecutionGuardsResult(
                False,
                f"Live execution blocked: open-order cap reached ({metrics['open_orders_total']} >= {self.config.max_open_orders_total})",
                metrics,
            )

        if metrics["open_orders_for_token"] >= self.config.max_open_orders_per_token:
            return ExecutionGuardsResult(
                False,
                f"Live execution blocked: token open-order cap reached ({metrics['open_orders_for_token']} >= {self.config.max_open_orders_per_token})",
                metrics,
            )

        if self.config.block_duplicate_token_orders and metrics["open_orders_for_token"] > 0:
            return ExecutionGuardsResult(
                False,
                f"Live execution blocked: duplicate-token suppression active with {metrics['open_orders_for_token']} existing open order(s)",
                metrics,
            )

        submission_remaining = self._cooldown_remaining_seconds((cooldowns or {}).get("last_submission_at"), self.config.submission_cooldown_seconds, now)
        fill_remaining = self._cooldown_remaining_seconds((cooldowns or {}).get("last_fill_at"), self.config.fill_cooldown_seconds, now)
        metrics["submission_cooldown_remaining_seconds"] = submission_remaining
        metrics["fill_cooldown_remaining_seconds"] = fill_remaining
        if submission_remaining > 0:
            return ExecutionGuardsResult(
                False,
                f"Live execution blocked: submission cooldown active ({submission_remaining:.1f}s remaining)",
                metrics,
            )
        if fill_remaining > 0:
            return ExecutionGuardsResult(
                False,
                f"Live execution blocked: fill cooldown active ({fill_remaining:.1f}s remaining)",
                metrics,
            )

        approved, reason = self.validate_order(intent, position)
        return ExecutionGuardsResult(approved, reason, metrics)

    @staticmethod
    def _cooldown_remaining_seconds(last_timestamp: str | None, cooldown_seconds: float, now: datetime) -> float:
        if not last_timestamp or cooldown_seconds <= 0:
            return 0.0
        try:
            ts = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        remaining = cooldown_seconds - (now - ts).total_seconds()
        return max(remaining, 0.0)
