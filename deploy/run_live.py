from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.market_filter import MarketFilter
from bot.paper_log import append_paper_log
from bot.risk_manager import RiskManager
from bot.trader import PolymarketTrader
from data.market_discovery import GammaMarketDiscoveryClient
from data.polymarket_client import PolymarketHistoryClient
from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.data import load_snapshots_from_csv, rows_to_snapshots
from polymarket_rbi_bot.models import Candle, MarketSnapshot, Position
from strategies.cvd_strategy import CVDStrategy
from strategies.long_entry_strategy import LongEntryStrategy
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def log_if_paper(
    args: argparse.Namespace,
    payload: dict,
    *,
    history_source: str,
    question: str | None = None,
) -> None:
    if args.execute:
        return
    entry = {
        "schema_version": 1,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        **payload,
        "condition_id": args.condition_id,
        "token_id": args.token_id,
        "mid_price": args.mid_price,
        "best_bid": args.best_bid,
        "best_ask": args.best_ask,
        "history_source": history_source,
        "eligibility_checked": not args.skip_eligibility,
    }
    if args.best_bid is not None and args.best_ask is not None and args.mid_price and args.mid_price > 0:
        entry["observed_spread_bps"] = (args.best_ask - args.best_bid) / args.mid_price * 10_000
    if args.decision_ts:
        entry["decision_ts"] = args.decision_ts
    if question:
        entry["question"] = question
    append_paper_log(entry)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a limit order for a Polymarket market.")
    parser.add_argument("--condition-id", required=True, help="Polymarket condition ID.")
    parser.add_argument("--token-id", required=True, help="Outcome token ID.")
    parser.add_argument("--mid-price", type=float, required=True, help="Reference mid price for the decision.")
    parser.add_argument("--best-bid", type=float, default=None, help="Optional best bid.")
    parser.add_argument("--best-ask", type=float, default=None, help="Optional best ask.")
    parser.add_argument("--decision-ts", help="Trusted timestamp for the observed quotes/snapshot (ISO-8601, ideally UTC).")
    parser.add_argument("--history-csv", help="Optional history CSV to warm up indicators.")
    parser.add_argument("--skip-eligibility", action="store_true", help="Bypass market eligibility checks.")
    parser.add_argument("--execute", action="store_true", help="Actually send the order to Polymarket.")
    parser.add_argument("--skip-reconcile", action="store_true", help="Skip exchange-state refresh before decisioning.")
    args = parser.parse_args()

    decision_ts = parse_timestamp(args.decision_ts)
    now = datetime.now(tz=timezone.utc)

    config = BotConfig.from_env()
    trader = PolymarketTrader(
        config=config,
        strategies=[
            LongEntryStrategy(
                strict_mode=config.strict_strategy_mode,
                strict_min_price=config.strict_min_price,
                strict_max_price=config.strict_max_price,
            ),
            MACDStrategy(),
            RSIStrategy(),
            CVDStrategy(),
        ],
    )
    risk = RiskManager(config)
    risk.daily_realized_pnl = trader.state_store.realized_pnl if trader.state_store is not None else 0.0

    market_question: str | None = None
    market_metadata_for_snapshot: dict[str, object] = {}
    history_source = "csv" if args.history_csv else "synthetic_warmup"

    if not args.skip_eligibility:
        discovery = GammaMarketDiscoveryClient(host=config.gamma_host)
        market = discovery.find_market_by_condition_id(args.condition_id) or discovery.find_market_by_token_id(args.token_id)
        if market is None:
            payload = {
                "status": "blocked",
                "reason": "Could not find active market metadata for eligibility check",
            }
            print(json.dumps(payload, indent=2))
            log_if_paper(args, payload, history_source=history_source)
            return

        market_question = str(market.get("question") or "") or None
        market_metadata_for_snapshot = {
            "endDate": market.get("endDate") or market.get("end_date_iso") or market.get("endTime") or market.get("closeTime") or market.get("resolutionTime"),
            "createdAt": market.get("createdAt") or market.get("openTime") or market.get("startTime"),
        }
        market_filter = MarketFilter(
            min_liquidity=config.min_market_liquidity,
            min_history_points=config.min_market_history_points,
            min_price=config.min_price,
            max_price=config.max_price,
            min_abs_return_bps_24h=config.min_abs_return_bps_24h,
            excluded_keywords=config.excluded_keywords,
            strict_mode=config.strict_strategy_mode,
            strict_min_price=config.strict_min_price,
            strict_max_price=config.strict_max_price,
            strict_excluded_keywords=config.strict_excluded_keywords,
            market_family_mode=config.market_family_mode,
            allowed_market_families=config.allowed_market_families,
            blocked_market_families=config.blocked_market_families,
            family_allow_keywords=config.family_allow_keywords,
            family_block_keywords=config.family_block_keywords,
            llm_market_classifier_path=config.llm_market_classifier_path,
            enable_maturity_gating=config.enable_maturity_gating,
            enable_microstructure_gating=config.enable_microstructure_gating,
            strict_min_time_to_resolution_hours=config.strict_min_time_to_resolution_hours,
            strict_max_time_to_resolution_hours=config.strict_max_time_to_resolution_hours,
            strict_min_time_since_open_hours=config.strict_min_time_since_open_hours,
            strict_max_current_spread_bps=config.strict_max_current_spread_bps,
        )
        eligibility = market_filter.evaluate(market, args.token_id)
        if not eligibility.eligible:
            payload = {
                "status": "blocked",
                "reason": f"Market failed eligibility check: {eligibility.reason}",
                "eligibility": eligibility.metrics,
                "question": market_question,
            }
            print(json.dumps(payload, indent=2))
            log_if_paper(args, payload, history_source=history_source, question=market_question)
            return

    reconciliation: dict | None = None
    if not args.skip_reconcile:
        reconciliation = trader.refresh_exchange_state(token_id=args.token_id, condition_id=args.condition_id)
        risk.daily_realized_pnl = trader.state_store.realized_pnl if trader.state_store is not None else 0.0
        if args.execute and not reconciliation.get("ok", False):
            payload = {
                "status": "blocked",
                "reason": "Exchange reconciliation failed; refusing live order submission",
                "reconciliation": reconciliation,
            }
            print(json.dumps(payload, indent=2))
            return

    snapshot = MarketSnapshot(
        candle=Candle(
            timestamp=decision_ts or now,
            open=args.mid_price,
            high=args.mid_price,
            low=args.mid_price,
            close=args.mid_price,
            volume=0.0,
        ),
        trades=[],
        best_bid=args.best_bid,
        best_ask=args.best_ask,
        metadata={
            "decision_ts": decision_ts.isoformat() if decision_ts else None,
            **{key: value for key, value in market_metadata_for_snapshot.items() if value not in {None, ""}},
        },
    )

    if args.history_csv:
        history = load_snapshots_from_csv(args.history_csv)
        history.append(snapshot)
    else:
        rows = PolymarketHistoryClient(host=config.host).fetch_price_history(token_id=args.token_id, interval="max", fidelity=60)
        history = rows_to_snapshots(rows)
        if history:
            history[-1] = snapshot
            history_source = "polymarket_history"
        else:
            history = trader.build_warmup_history(
                mid_price=args.mid_price,
                best_bid=args.best_bid,
                best_ask=args.best_ask,
            )
            history[-1] = snapshot
            history_source = "synthetic_warmup"

    position = trader.positions.get(args.token_id, Position(token_id=args.token_id))
    token_open_orders = []
    all_open_orders = []
    cooldowns = {}
    local_state_summary = None
    if trader.state_store is not None:
        all_open_orders = list(trader.state_store.open_orders.values())
        token_open_orders = [
            order for order in all_open_orders if str(order.get("asset_id") or order.get("token_id") or order.get("market")) == str(args.token_id)
        ]
        cooldowns = trader.state_store.cooldowns
        local_state_summary = {
            "path": str(trader.state_store.path),
            "realized_pnl": trader.state_store.realized_pnl,
            "open_orders": len(all_open_orders),
            "token_open_orders": len(token_open_orders),
            "position": {
                "token_id": position.token_id,
                "quantity": position.quantity,
                "average_price": position.average_price,
                "opened_at": position.opened_at.isoformat() if position.opened_at else None,
            },
            "cooldowns": cooldowns,
            "reconcile": trader.state_store.snapshot().get("reconcile", {}),
            "strict_strategy_mode": config.strict_strategy_mode,
        }
    intent, decision_reason, signal_summary = trader.build_order_decision(args.token_id, history, position)
    if intent is None:
        payload = {
            "status": "no_trade",
            "reason": decision_reason or "Strategies produced no actionable limit order",
            "signal_summary": {
                "buy_score": signal_summary.get("buy_score", 0.0),
                "sell_score": signal_summary.get("sell_score", 0.0),
                "buy_signal_count": signal_summary.get("buy_signal_count", 0),
                "sell_signal_count": signal_summary.get("sell_signal_count", 0),
                "buy_confirmer_count": signal_summary.get("buy_confirmer_count", 0),
                "sell_confirmer_count": signal_summary.get("sell_confirmer_count", 0),
                "strict_entry_score": signal_summary.get("strict_entry_score"),
                "expected_edge_bps": signal_summary.get("expected_edge_bps", 0.0),
                "required_edge_bps": signal_summary.get("required_edge_bps"),
                "maturity": signal_summary.get("maturity"),
                "microstructure": signal_summary.get("microstructure"),
                "signals": signal_summary.get("signals", []),
            },
            "decision_context": {
                "decision_ts": decision_ts.isoformat() if decision_ts else None,
                "freshness_trusted": decision_ts is not None,
                "best_bid": args.best_bid,
                "best_ask": args.best_ask,
                "strict_strategy_mode": config.strict_strategy_mode,
            },
            "reconciliation": reconciliation,
            "state": local_state_summary,
        }
        print(json.dumps(payload, indent=2))
        log_if_paper(args, payload, history_source=history_source, question=market_question)
        return

    try:
        market_meta = trader.fetch_market_metadata(args.condition_id)
    except Exception as exc:
        if args.execute:
            payload = {
                "status": "blocked",
                "reason": f"Could not fetch market metadata required for live order submission: {exc}",
                "intent": asdict(intent),
                "reconciliation": reconciliation,
                "state": local_state_summary,
            }
            print(json.dumps(payload, indent=2, default=str))
            return
        market_meta = {"tick_size": intent.tick_size, "neg_risk": intent.neg_risk, "warning": str(exc)}
    intent.tick_size = market_meta["tick_size"]
    intent.neg_risk = market_meta["neg_risk"]

    guard_result = risk.evaluate_execution_guards(
        intent=intent,
        position=position,
        snapshot=snapshot,
        decision_ts=decision_ts,
        now=now,
        open_orders=token_open_orders,
        all_open_orders=all_open_orders,
        cooldowns=cooldowns,
    )

    if args.execute and not guard_result.ok:
        payload = {
            "status": "blocked",
            "reason": guard_result.reason,
            "intent": asdict(intent),
            "guardrails": guard_result.metrics,
            "reconciliation": reconciliation,
            "state": local_state_summary,
        }
        print(json.dumps(payload, indent=2))
        return

    if not args.execute:
        payload = {
            "status": "dry_run",
            "reason": guard_result.reason if guard_result.ok else "Dry run only: live execution would currently be blocked",
            "intent": asdict(intent),
            "signal_summary": {
                "buy_score": signal_summary.get("buy_score", 0.0),
                "sell_score": signal_summary.get("sell_score", 0.0),
                "buy_signal_count": signal_summary.get("buy_signal_count", 0),
                "sell_signal_count": signal_summary.get("sell_signal_count", 0),
                "buy_confirmer_count": signal_summary.get("buy_confirmer_count", 0),
                "sell_confirmer_count": signal_summary.get("sell_confirmer_count", 0),
                "strict_entry_score": signal_summary.get("strict_entry_score"),
                "expected_edge_bps": signal_summary.get("expected_edge_bps", 0.0),
                "required_edge_bps": signal_summary.get("required_edge_bps"),
                "expected_edge_after_cost_bps": signal_summary.get("expected_edge_after_cost_bps"),
                "maturity": signal_summary.get("maturity"),
                "microstructure": signal_summary.get("microstructure"),
                "signals": signal_summary.get("signals", []),
            },
            "guardrails": {
                **guard_result.metrics,
                "would_execute": guard_result.ok,
                "live_block_reason": None if guard_result.ok else guard_result.reason,
            },
            "reconciliation": reconciliation,
            "state": local_state_summary,
        }
        print(json.dumps(payload, indent=2))
        log_if_paper(args, payload, history_source=history_source, question=market_question)
        return

    response = trader.place_limit_order(intent)
    submitted_state = trader.state_store.snapshot() if trader.state_store is not None else None
    print(
        json.dumps(
            {
                "status": "submitted",
                "response": response,
                "guardrails": guard_result.metrics,
                "signal_summary": signal_summary,
                "reconciliation": reconciliation,
                "state": submitted_state,
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
