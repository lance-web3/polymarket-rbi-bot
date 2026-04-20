from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtesting.engine import BacktestEngine
from data.storage import save_rows_to_csv
from data.polymarket_client import PolymarketHistoryClient
from polymarket_rbi_bot.data import load_snapshots_from_csv
from strategies.cvd_strategy import CVDStrategy
from strategies.long_entry_strategy import LongEntryStrategy
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Polymarket strategy backtest.")
    parser.add_argument("--csv", help="Path to OHLCV snapshot CSV.")
    parser.add_argument("--token-id", help="Polymarket token id to fetch from the price-history endpoint.")
    parser.add_argument("--interval", default="max", help="Polymarket price history interval.")
    parser.add_argument("--fidelity", type=int, default=60, help="Polymarket history fidelity.")
    parser.add_argument("--start-ts", type=int, help="Optional unix start timestamp.")
    parser.add_argument("--end-ts", type=int, help="Optional unix end timestamp.")
    parser.add_argument("--save-csv", help="Optional path to save fetched Polymarket history as CSV.")
    parser.add_argument("--cash", type=float, default=1_000.0, help="Starting cash for the backtest.")
    parser.add_argument("--size", type=float, default=10.0, help="Target fixed per-trade size.")
    parser.add_argument("--slippage-bps", type=float, default=25.0, help="Adverse slippage applied to fills in basis points.")
    parser.add_argument("--fee-bps", type=float, default=0.0, help="Per-fill taker/maker fee in basis points; applied symmetrically to entries and exits.")
    parser.add_argument("--adverse-selection-horizon-bars", type=int, default=1, help="Bars after fill to measure post-fill mid move (adverse-selection proxy).")
    parser.add_argument(
        "--fallback-half-spread-bps",
        type=float,
        default=50.0,
        help="Assumed half-spread in bps when bid/ask is missing but a fallback fill is still allowed.",
    )
    parser.add_argument(
        "--max-spread-bps",
        type=float,
        default=1500.0,
        help="If observed spread exceeds this threshold, use wide-spread fill behavior instead of full fill.",
    )
    parser.add_argument(
        "--missing-quote-fill-ratio",
        type=float,
        default=1.0,
        help="Fraction of target size allowed to fill when same-side quote is missing. 0 means skip.",
    )
    parser.add_argument(
        "--wide-spread-fill-ratio",
        type=float,
        default=0.5,
        help="Fraction of target size allowed to fill when spread is wider than --max-spread-bps. 0 means skip.",
    )
    parser.add_argument("--strict-mode", action="store_true", help="Use stricter entry gating, edge-vs-cost checks, and anti-churn exits.")
    parser.add_argument("--min-entry-confidence", type=float, default=0.50, help="Strict mode: minimum lead BUY confidence.")
    parser.add_argument("--min-buy-score", type=float, default=1.1, help="Strict mode: minimum aggregate BUY score.")
    parser.add_argument("--min-buy-sell-score-gap", type=float, default=0.35, help="Strict mode: minimum BUY minus SELL score gap.")
    parser.add_argument("--min-buy-signal-count", type=int, default=1, help="Legacy strict-mode aggregate BUY count knob; kept for older comparisons.")
    parser.add_argument("--strict-require-confirmers", action="store_true", help="Strict mode: require confirmers (non-long-entry BUYs) to proceed.")
    parser.add_argument("--strict-long-entry-led", dest="strict_long_entry_led", action="store_true", default=True, help="Strict mode: require long_entry to lead entries (default: on).")
    parser.add_argument("--no-strict-long-entry-led", dest="strict_long_entry_led", action="store_false", help="Strict mode: fall back to old aggregate strict entry logic instead of long-entry-led gating.")
    parser.add_argument("--long-entry-version", choices=["v2", "legacy"], default="v2", help="LongEntry signal recipe to use for comparison runs.")
    parser.add_argument("--strict-exit-style", choices=["upgraded", "legacy"], default="upgraded", help="Strict mode exit policy to use.")
    parser.add_argument("--strict-min-confirmers", type=int, default=0, help="Strict mode: minimum number of confirmers when required.")
    parser.add_argument("--strict-confirmer-buy-bonus", type=float, default=0.08, help="Strict mode: weak bonus added per BUY confirmer.")
    parser.add_argument("--strict-confirmer-sell-penalty", type=float, default=0.12, help="Strict mode: weak penalty subtracted per SELL confirmer.")
    parser.add_argument("--strict-min-entry-score", type=float, default=0.55, help="Strict mode: minimum long-entry-led score after confirmer adjustments.")
    parser.add_argument("--min-hold-bars", type=int, default=3, help="Strict mode: minimum bars to hold before allowing an exit.")
    parser.add_argument("--cooldown-bars-after-exit", type=int, default=2, help="Strict mode: cooldown bars before a fresh entry after flattening.")
    parser.add_argument("--strict-max-hold-bars", type=int, default=12, help="Strict mode: force an exit after this many held bars.")
    parser.add_argument("--strict-fail-exit-bars", type=int, default=6, help="Strict mode: after this many bars, cut lagging trades that are still underwater.")
    parser.add_argument("--strict-fail-exit-pnl-bps", type=float, default=-35.0, help="Strict mode: fail exit threshold in bps versus entry price.")
    parser.add_argument("--strict-take-profit-bars", type=int, default=4, help="Strict mode: earliest bar where profit-protect exits can activate.")
    parser.add_argument("--strict-take-profit-pnl-bps", type=float, default=80.0, help="Strict mode: minimum best unrealized gain before pullback profit-taking can activate.")
    parser.add_argument("--strict-profit-giveback-bps", type=float, default=45.0, help="Strict mode: exit after this much giveback from the best open profit.")
    parser.add_argument("--strict-extended-hold-bars", type=int, default=8, help="Strict mode: after this many bars, allow a softer sell-minus-buy exit gap.")
    parser.add_argument("--strict-extended-hold-exit-gap", type=float, default=0.15, help="Strict mode: softened sell-minus-buy gap after extended holds.")
    parser.add_argument("--estimated-round-trip-cost-bps", type=float, default=80.0, help="Strict mode: simple friction proxy used in edge gating.")
    parser.add_argument("--min-expected-edge-bps", type=float, default=120.0, help="Strict mode: minimum expected edge before a BUY is allowed.")
    parser.add_argument("--edge-cost-buffer-bps", type=float, default=30.0, help="Strict mode: extra safety margin over estimated cost.")
    # New: maturity + microstructure gating knobs
    parser.add_argument("--enable-maturity-gating", action="store_true", help="Enable maturity (time-to-resolution) gating in strict mode.")
    parser.add_argument("--enable-microstructure-gating", action="store_true", help="Enable microstructure gating in strict mode.")
    parser.add_argument("--strict-min-ttr-hours", type=float, help="Strict: minimum hours until resolution to allow entries.")
    parser.add_argument("--strict-max-ttr-hours", type=float, help="Strict: maximum hours until resolution to allow entries.")
    parser.add_argument("--strict-min-since-open-hours", type=float, help="Strict: require market to be open at least this many hours.")
    parser.add_argument("--strict-quote-lookback-bars", type=int, default=24, help="Strict: bars to look back for microstructure metrics.")
    parser.add_argument("--strict-min-quote-observations", type=int, default=3, help="Strict: minimum quoted bars required before enforcing quote-availability/spread gates.")
    parser.add_argument("--strict-min-quote-avail-ratio", type=float, default=0.25, help="Strict: minimum ratio of bars with both quotes present once enough quoted bars exist.")
    parser.add_argument("--strict-max-avg-spread-bps", type=float, default=450.0, help="Strict: maximum average spread over lookback in bps.")
    parser.add_argument("--strict-max-current-spread-bps", type=float, default=450.0, help="Strict: maximum current spread in bps.")
    parser.add_argument("--strict-max-wide-spread-rate", type=float, default=0.65, help="Strict: maximum fraction of quote bars that are wider than wide-spread threshold.")
    parser.add_argument("--strict-wide-spread-bps", type=float, default=700.0, help="Strict: bps threshold that defines a wide spread.")
    parser.add_argument(
        "--microstructure-proxy-policy",
        choices=["auto", "real-only"],
        default="auto",
        help="How strict-mode microstructure should behave when historical bid/ask is missing. 'auto' allows an explicit synthetic proxy; 'real-only' keeps the old behavior.",
    )
    parser.add_argument(
        "--with-cvd",
        action="store_true",
        help="Include the CVD strategy. Only use this when your snapshots contain real trade-flow data.",
    )
    args = parser.parse_args()

    if not args.csv and not args.token_id:
        parser.error("Provide either --csv or --token-id")

    if args.csv and args.token_id:
        parser.error("Use only one data source: --csv or --token-id")

    if args.csv:
        snapshots = load_snapshots_from_csv(args.csv)
        data_source = args.csv
    else:
        rows = PolymarketHistoryClient().fetch_price_history(
            token_id=args.token_id,
            interval=args.interval,
            fidelity=args.fidelity,
            start_ts=args.start_ts,
            end_ts=args.end_ts,
        )
        if args.save_csv:
            save_rows_to_csv(args.save_csv, rows)
        temp_csv = args.save_csv or (Path("data") / f"polymarket_{args.token_id}.csv")
        if not args.save_csv:
            save_rows_to_csv(temp_csv, rows)
        snapshots = load_snapshots_from_csv(temp_csv)
        data_source = str(temp_csv)

    strategies = [LongEntryStrategy(strict_mode=args.strict_mode, signal_version=args.long_entry_version), MACDStrategy(), RSIStrategy()]
    cvd_enabled = args.with_cvd and any(snapshot.trades for snapshot in snapshots)
    if args.with_cvd and not cvd_enabled:
        print(
            json.dumps(
                {
                    "warning": "CVD requested but no trade-flow data was found in the snapshots. Running price-only strategies instead."
                }
            )
        )
    if cvd_enabled:
        strategies.append(CVDStrategy())

    engine = BacktestEngine(
        strategies=strategies,
        starting_cash=args.cash,
        per_trade_size=args.size,
        slippage_bps=args.slippage_bps,
        fallback_half_spread_bps=args.fallback_half_spread_bps,
        fee_bps=args.fee_bps,
        adverse_selection_horizon_bars=args.adverse_selection_horizon_bars,
        max_spread_bps=args.max_spread_bps,
        missing_quote_fill_ratio=args.missing_quote_fill_ratio,
        wide_spread_fill_ratio=args.wide_spread_fill_ratio,
        strict_mode=args.strict_mode,
        min_entry_confidence=args.min_entry_confidence,
        min_buy_score=args.min_buy_score,
        min_buy_sell_score_gap=args.min_buy_sell_score_gap,
        min_buy_signal_count=args.min_buy_signal_count,
        strict_require_confirmers=args.strict_require_confirmers,
        strict_long_entry_led=args.strict_long_entry_led,
        strict_exit_style=args.strict_exit_style,
        strict_min_confirmers=args.strict_min_confirmers,
        strict_confirmer_buy_bonus=args.strict_confirmer_buy_bonus,
        strict_confirmer_sell_penalty=args.strict_confirmer_sell_penalty,
        strict_min_entry_score=args.strict_min_entry_score,
        min_hold_bars=args.min_hold_bars,
        cooldown_bars_after_exit=args.cooldown_bars_after_exit,
        strict_max_hold_bars=args.strict_max_hold_bars,
        strict_fail_exit_bars=args.strict_fail_exit_bars,
        strict_fail_exit_pnl_bps=args.strict_fail_exit_pnl_bps,
        strict_take_profit_bars=args.strict_take_profit_bars,
        strict_take_profit_pnl_bps=args.strict_take_profit_pnl_bps,
        strict_profit_giveback_bps=args.strict_profit_giveback_bps,
        strict_extended_hold_bars=args.strict_extended_hold_bars,
        strict_extended_hold_exit_gap=args.strict_extended_hold_exit_gap,
        estimated_round_trip_cost_bps=args.estimated_round_trip_cost_bps,
        min_expected_edge_bps=args.min_expected_edge_bps,
        edge_cost_buffer_bps=args.edge_cost_buffer_bps,
        enable_maturity_gating=args.enable_maturity_gating,
        enable_microstructure_gating=args.enable_microstructure_gating,
        strict_min_time_to_resolution_hours=args.strict_min_ttr_hours,
        strict_max_time_to_resolution_hours=args.strict_max_ttr_hours,
        strict_min_time_since_open_hours=args.strict_min_since_open_hours,
        strict_quote_lookback_bars=args.strict_quote_lookback_bars,
        strict_min_quote_observations=args.strict_min_quote_observations,
        strict_min_quote_availability_ratio=args.strict_min_quote_avail_ratio,
        strict_max_avg_spread_bps=args.strict_max_avg_spread_bps,
        strict_max_current_spread_bps=args.strict_max_current_spread_bps,
        strict_max_wide_spread_rate=args.strict_max_wide_spread_rate,
        strict_wide_spread_bps=args.strict_wide_spread_bps,
        microstructure_proxy_policy=args.microstructure_proxy_policy,
    )
    result = engine.run(snapshots)
    metrics = result.metadata.get("metrics", {})
    execution_model = result.metadata.get("execution_model", {})
    microstructure_run_summary = result.metadata.get("microstructure_run_summary", {})
    experiment_score = _build_experiment_score(result=result, metrics=metrics)
    print(
        json.dumps(
            {
                "data_source": data_source,
                "snapshot_count": len(snapshots),
                "strategies": [strategy.name for strategy in strategies],
                "cash": {
                    "starting": result.starting_cash,
                    "ending": result.ending_cash,
                    "realized_pnl": result.realized_pnl,
                    "mark_to_market_equity": result.mark_to_market_equity,
                },
                "risk": {
                    "max_drawdown": result.max_drawdown,
                    "ending_inventory": result.metadata.get("ending_inventory"),
                    "average_entry_price": result.metadata.get("average_entry_price"),
                },
                "metrics": metrics,
                "cost_attribution": result.metadata.get("cost_attribution", {}),
                "comparison": {
                    "experiment_score": experiment_score,
                    "headline": {
                        "net_return_pct": round(((result.mark_to_market_equity / result.starting_cash) - 1) * 100, 2)
                        if result.starting_cash
                        else None,
                        "expectancy_per_round_trip": metrics.get("expectancy"),
                        "round_trip_count": metrics.get("round_trip_count"),
                        "max_drawdown_pct": round(result.max_drawdown * 100, 2),
                    },
                },
                "execution_model": execution_model,
                "microstructure": microstructure_run_summary,
                "strict_mode": result.metadata.get("strict_mode", {}),
                "trades": {
                    "count": len(result.trades),
                    "first": _format_trade(result.trades[0]) if result.trades else None,
                    "last": _format_trade(result.trades[-1]) if result.trades else None,
                },
            },
            indent=2,
            default=str,
        )
    )


def _build_experiment_score(*, result, metrics: dict) -> dict[str, object]:
    round_trips = int(metrics.get("round_trip_count") or 0)
    expectancy = float(metrics.get("expectancy") or 0.0)
    trade_count = int(metrics.get("trade_count") or 0)
    net_return_pct = ((result.mark_to_market_equity / result.starting_cash) - 1) * 100 if result.starting_cash else 0.0
    drawdown_pct = result.max_drawdown * 100

    component_scores = {
        "net_return": {
            "score": round(max(0.0, min(35.0, net_return_pct * 3.5)), 1),
            "max_score": 35.0,
            "value": round(net_return_pct, 2),
            "explanation": "Positive return matters most, but the score is capped to avoid fake precision.",
        },
        "expectancy": {
            "score": round(max(0.0, min(25.0, expectancy * 12.5)), 1),
            "max_score": 25.0,
            "value": round(expectancy, 4),
            "explanation": "Average realized PnL per completed round trip.",
        },
        "sample_size": {
            "score": round(max(0.0, min(20.0, round_trips * 2.0)), 1),
            "max_score": 20.0,
            "value": round_trips,
            "explanation": "A decent number of completed trades is easier to trust than a one-off win.",
        },
        "drawdown_control": {
            "score": round(max(0.0, min(20.0, 20.0 - drawdown_pct * 2.0)), 1),
            "max_score": 20.0,
            "value": round(drawdown_pct, 2),
            "explanation": "Lower drawdown is better. Big pain should cost score.",
        },
    }
    total_score = round(sum(component["score"] for component in component_scores.values()), 1)

    return {
        "score": total_score,
        "tier": "strong" if total_score >= 70 else "promising" if total_score >= 50 else "weak",
        "trade_count": trade_count,
        "round_trip_count": round_trips,
        "components": component_scores,
        "limitations": [
            "This is a comparison heuristic, not a statistically rigorous fitness metric.",
            "Scores are sensitive to the chosen fill model and sample period.",
            "Low-trade backtests can still score deceptively well if one move dominates results.",
        ],
    }


def _format_trade(trade):
    return {
        "timestamp": trade.timestamp,
        "side": trade.side,
        "price": trade.price,
        "size": trade.size,
        "pnl_after_trade": trade.pnl_after_trade,
        "metadata": trade.metadata,
    }


if __name__ == "__main__":
    main()
