from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtesting.engine import BacktestEngine
from data.market_discovery import GammaMarketDiscoveryClient, extract_yes_token
from data.polymarket_client import PolymarketHistoryClient
from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.data import rows_to_snapshots
from strategies.cvd_strategy import CVDStrategy
from strategies.long_entry_strategy import LongEntryStrategy
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy
from bot.market_filter import MarketFilter


def _experiment_score(*, result, metrics: dict) -> dict[str, Any]:
    # Mirrors deploy/run_backtest._build_experiment_score but local to avoid circular imports
    round_trips = int(metrics.get("round_trip_count") or 0)
    expectancy = float(metrics.get("expectancy") or 0.0)
    trade_count = int(metrics.get("trade_count") or 0)
    net_return_pct = (
        ((result.mark_to_market_equity / result.starting_cash) - 1) * 100 if result.starting_cash else 0.0
    )
    drawdown_pct = result.max_drawdown * 100

    component_scores = {
        "net_return": {
            "score": round(max(0.0, min(35.0, net_return_pct * 3.5)), 1),
            "max_score": 35.0,
            "value": round(net_return_pct, 2),
            "explanation": "Positive return matters most, capped to avoid fake precision.",
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
            "explanation": "More completed trades increases confidence.",
        },
        "drawdown_control": {
            "score": round(max(0.0, min(20.0, 20.0 - drawdown_pct * 2.0)), 1),
            "max_score": 20.0,
            "value": round(drawdown_pct, 2),
            "explanation": "Lower drawdown earns more points.",
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
            "Heuristic for comparison; sensitive to fill model and period.",
            "Low-trade samples may overstate results.",
        ],
    }


def run() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scan, rank, backtest, and compare Polymarket markets in a single research loop."
        )
    )
    # Scan/selection
    parser.add_argument("--scan-limit", type=int, default=50, help="Markets to fetch from discovery API.")
    parser.add_argument("--top", type=int, default=10, help="Top ranked markets to backtest.")
    # Backtest exec assumptions (mirrors deploy/run_backtest.py)
    parser.add_argument("--cash", type=float, default=1_000.0, help="Starting cash for the backtest.")
    parser.add_argument("--size", type=float, default=10.0, help="Target fixed per-trade size.")
    parser.add_argument("--slippage-bps", type=float, default=25.0, help="Adverse slippage (bps).")
    parser.add_argument(
        "--fallback-half-spread-bps",
        type=float,
        default=50.0,
        help="Half-spread used on mid fallback (bps).",
    )
    parser.add_argument(
        "--max-spread-bps",
        type=float,
        default=1500.0,
        help="When observed spread exceeds this, use wide-spread fill behavior.",
    )
    parser.add_argument(
        "--missing-quote-fill-ratio",
        type=float,
        default=1.0,
        help="Fraction of size to fill when same-side quote is missing (0..1).",
    )
    parser.add_argument(
        "--wide-spread-fill-ratio",
        type=float,
        default=0.5,
        help="Fraction of size to fill when spread wider than --max-spread-bps (0..1).",
    )
    parser.add_argument(
        "--with-cvd",
        action="store_true",
        help="Include CVD strategy if snapshots contain trade-flow data.",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=str(Path("data") / "research_results.json"),
        help="Where to write the aggregated JSON report.",
    )

    args = parser.parse_args()

    config = BotConfig.from_env()
    discovery = GammaMarketDiscoveryClient(host=config.gamma_host)
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
    )

    # 1) Scan and rank
    scan_rows: list[dict[str, Any]] = []
    for market in discovery.list_markets(limit=args.scan_limit, closed=False, archived=False):
        yes_token = extract_yes_token(market)
        if not yes_token:
            continue
        try:
            result = market_filter.evaluate(market, yes_token["token_id"])
        except Exception as exc:  # pragma: no cover - network/path dependent
            # If evaluation errors, record minimal info and continue
            scan_rows.append(
                {
                    "question": market.get("question"),
                    "condition_id": market.get("conditionId") or market.get("condition_id"),
                    "token_id": yes_token["token_id"],
                    "outcome": yes_token["outcome"],
                    "eligible": False,
                    "reason": f"evaluation_error: {exc}",
                }
            )
            continue

        row = {
            "question": market.get("question"),
            "condition_id": market.get("conditionId") or market.get("condition_id"),
            "token_id": yes_token["token_id"],
            "outcome": yes_token["outcome"],
            "eligible": result.eligible,
            "reason": result.reason,
            **result.metrics,
        }
        scan_rows.append(row)

    ranked = sorted(
        scan_rows,
        key=lambda row: (
            1 if row.get("eligible") else 0,
            float(row.get("quality_score") or 0.0),
            float(row.get("liquidity") or 0.0),
            float(row.get("abs_return_bps_24h") or 0.0),
        ),
        reverse=True,
    )

    # 2) Backtest top-N
    client = PolymarketHistoryClient(host=config.host)
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for index, candidate in enumerate(ranked[: max(args.top, 0)], start=1):
        token_id = str(candidate.get("token_id"))
        condition_id = candidate.get("condition_id")
        question = candidate.get("question")
        try:
            rows = client.fetch_price_history(
                token_id=token_id,
                interval="max",
                fidelity=60,
            )
            snapshots = rows_to_snapshots(rows)

            strategies = [LongEntryStrategy(strict_mode=config.strict_strategy_mode), MACDStrategy(), RSIStrategy()]
            cvd_enabled = args.with_cvd and any(snapshot.trades for snapshot in snapshots)
            if cvd_enabled:
                strategies.append(CVDStrategy())

            engine = BacktestEngine(
                strategies=strategies,
                starting_cash=args.cash,
                per_trade_size=args.size,
                slippage_bps=args.slippage_bps,
                fallback_half_spread_bps=args.fallback_half_spread_bps,
                max_spread_bps=args.max_spread_bps,
                missing_quote_fill_ratio=args.missing_quote_fill_ratio,
                wide_spread_fill_ratio=args.wide_spread_fill_ratio,
                strict_mode=config.strict_strategy_mode,
                min_entry_confidence=config.min_entry_confidence,
                min_buy_score=config.min_buy_score,
                min_buy_sell_score_gap=config.min_buy_sell_score_gap,
                min_buy_signal_count=config.min_buy_signal_count,
                strict_require_confirmers=config.strict_require_confirmers,
                strict_min_confirmers=config.strict_min_confirmers,
                strict_confirmer_buy_bonus=config.strict_confirmer_buy_bonus,
                strict_confirmer_sell_penalty=config.strict_confirmer_sell_penalty,
                strict_min_entry_score=config.strict_min_entry_score,
                min_hold_bars=config.min_hold_bars,
                cooldown_bars_after_exit=config.cooldown_bars_after_exit,
                strict_max_hold_bars=config.strict_max_hold_bars,
                strict_fail_exit_bars=config.strict_fail_exit_bars,
                strict_fail_exit_pnl_bps=config.strict_fail_exit_pnl_bps,
                strict_take_profit_bars=config.strict_take_profit_bars,
                strict_take_profit_pnl_bps=config.strict_take_profit_pnl_bps,
                strict_profit_giveback_bps=config.strict_profit_giveback_bps,
                strict_extended_hold_bars=config.strict_extended_hold_bars,
                strict_extended_hold_exit_gap=config.strict_extended_hold_exit_gap,
                estimated_round_trip_cost_bps=config.estimated_round_trip_cost_bps,
                min_expected_edge_bps=config.min_expected_edge_bps,
                edge_cost_buffer_bps=config.edge_cost_buffer_bps,
            )
            result = engine.run(snapshots)
            metrics = result.metadata.get("metrics", {})
            exec_model = result.metadata.get("execution_model", {})
            exp_score = _experiment_score(result=result, metrics=metrics)

            results.append(
                {
                    "rank": index,
                    "question": question,
                    "token_id": token_id,
                    "condition_id": condition_id,
                    "outcome": candidate.get("outcome"),
                    "tradability": {
                        "eligible": bool(candidate.get("eligible")),
                        "quality_score": candidate.get("quality_score"),
                        "quality_tier": candidate.get("quality_tier"),
                        "reason": candidate.get("reason"),
                        "ranking_summary": candidate.get("ranking_summary"),
                    },
                    "backtest": {
                        "snapshot_count": len(snapshots),
                        "strategies": [s.name for s in strategies],
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
                        "headline": {
                            "net_return_pct": round(
                                ((result.mark_to_market_equity / result.starting_cash) - 1) * 100, 2
                            )
                            if result.starting_cash
                            else None,
                            "expectancy_per_round_trip": metrics.get("expectancy"),
                            "round_trip_count": metrics.get("round_trip_count"),
                            "max_drawdown_pct": round(result.max_drawdown * 100, 2),
                        },
                        "experiment_score": exp_score,
                    },
                    "execution_model": exec_model,
                }
            )
        except Exception as exc:  # pragma: no cover - network/path dependent
            errors.append(
                {
                    "rank": index,
                    "question": question,
                    "token_id": token_id,
                    "condition_id": condition_id,
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=1),
                }
            )
            continue

    # 3) Rank shortlist by experiment score primarily, then quality score
    ranked_candidates = sorted(
        results,
        key=lambda r: (
            float(((r.get("backtest") or {}).get("experiment_score") or {}).get("score") or 0.0),
            float(((r.get("tradability") or {}).get("quality_score") or 0.0)),
        ),
        reverse=True,
    )

    shortlist = [
        {
            "rank": i + 1,
            "question": r.get("question"),
            "token_id": r.get("token_id"),
            "condition_id": r.get("condition_id"),
            "net_return_pct": ((r["backtest"]["headline"]["net_return_pct"])) if r.get("backtest") else None,
            "experiment_score": ((r["backtest"]["experiment_score"]["score"])) if r.get("backtest") else None,
            "tradability_score": ((r["tradability"]["quality_score"])) if r.get("tradability") else None,
            "tier": ((r["backtest"]["experiment_score"]["tier"])) if r.get("backtest") else None,
        }
        for i, r in enumerate(ranked_candidates[: max(args.top, 0)])
    ]

    payload = {
        "summary": {
            "markets_scanned": len(scan_rows),
            "eligible_count": sum(1 for row in scan_rows if row.get("eligible")),
            "ineligible_count": sum(1 for row in scan_rows if not row.get("eligible")),
            "top_requested": args.top,
            "backtested": len(results),
            "errors": len(errors),
        },
        "shortlist": shortlist,
        "candidates": ranked_candidates,
        "errors": errors,
        "assumptions": {
            "backtest": {
                "starting_cash": args.cash,
                "per_trade_size": args.size,
                "slippage_bps": args.slippage_bps,
                "fallback_half_spread_bps": args.fallback_half_spread_bps,
                "max_spread_bps": args.max_spread_bps,
                "missing_quote_fill_ratio": args.missing_quote_fill_ratio,
                "wide_spread_fill_ratio": args.wide_spread_fill_ratio,
                "with_cvd": bool(args.with_cvd),
            },
            "selection": {
                "scan_limit": args.scan_limit,
                "rank_by": [
                    "eligible -> quality_score -> liquidity -> abs_return_bps_24h (descending)",
                ],
                "shortlist_rank_by": [
                    "experiment_score.score -> tradability.quality_score (descending)",
                ],
            },
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps({"saved": str(out_path), "shortlist": shortlist}, indent=2, default=str))


if __name__ == "__main__":
    run()
