from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtesting.engine import BacktestEngine
from bot.market_filter import MarketFilter
from data.csv_metadata import extract_market_metadata
from polymarket_rbi_bot.calibration import brier_score, reference_brier_baselines
from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.data import load_snapshots_from_csv
from polymarket_rbi_bot.models import SignalSide
from strategies.long_entry_strategy import LongEntryStrategy
from strategies.macd_strategy import MACDStrategy
from strategies.rsi_strategy import RSIStrategy


def build_market_filter_from_env() -> MarketFilter:
    config = BotConfig.from_env()
    return MarketFilter(
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


def compute_trade_brier(trades: list[Any]) -> dict[str, Any]:
    """Pair each BUY's long_entry_confidence with the next SELL's realized_pnl sign.

    Returns {count, brier_score, baselines, predictions, outcomes} or a
    null-filled dict when fewer than 1 round trip exists.
    """
    predictions: list[float] = []
    outcomes: list[int] = []
    pending_confidence: float | None = None
    for trade in trades:
        side = trade.side
        meta = trade.metadata or {}
        if side == SignalSide.BUY:
            summary = meta.get("decision_summary") or {}
            raw_conf = summary.get("long_entry_confidence")
            try:
                conf = float(raw_conf) if raw_conf is not None else 0.0
            except (TypeError, ValueError):
                conf = 0.0
            pending_confidence = max(0.0, min(1.0, conf))
        elif side == SignalSide.SELL and pending_confidence is not None:
            realized = meta.get("realized_pnl_delta")
            try:
                outcome = 1 if realized is not None and float(realized) > 0.0 else 0
            except (TypeError, ValueError):
                outcome = 0
            predictions.append(pending_confidence)
            outcomes.append(outcome)
            pending_confidence = None
    if not predictions:
        return {"count": 0, "brier_score": None, "baselines": None}
    return {
        "count": len(predictions),
        "brier_score": round(brier_score(predictions, outcomes), 6),
        "baselines": {k: round(v, 6) for k, v in reference_brier_baselines(outcomes).items()},
    }


DEFAULT_EXPERIMENTS = [
    {
        "name": "loose_baseline",
        "description": "Loose ensemble baseline.",
        "strict_mode": False,
        "long_entry_version": "v2",
        "strict_long_entry_led": False,
        "strict_exit_style": "legacy",
        "enable_maturity_gating": False,
        "enable_microstructure_gating": False,
    },
    {
        "name": "strict_full",
        "description": "Current strict profile with long-entry-led entries, upgraded exits, and gating on.",
        "strict_mode": True,
        "long_entry_version": "v2",
        "strict_long_entry_led": True,
        "strict_exit_style": "upgraded",
        "enable_maturity_gating": True,
        "enable_microstructure_gating": True,
    },
    {
        "name": "strict_middle_ground",
        "description": "Moderately relaxed strict profile: wider price zone, lower momentum floor, and quote gating kept on.",
        "strict_mode": True,
        "long_entry_version": "v2",
        "strict_long_entry_led": True,
        "strict_exit_style": "upgraded",
        "enable_maturity_gating": True,
        "enable_microstructure_gating": True,
        "long_entry_overrides": {
            "strict_min_price": 0.10,
            "strict_max_price": 0.72,
            "strict_min_return_bps": 90.0,
            "strict_max_pullback_bps": 320.0,
            "min_fast_momentum_bps": 15.0,
            "min_medium_momentum_bps": 45.0,
            "min_slow_momentum_bps": 90.0,
            "min_positive_closes": 2,
            "min_above_mean_bps": 10.0,
            "min_baseline_persistence": 0.48,
            "min_trend_efficiency": 0.18,
            "max_one_bar_jump_bps": 260.0,
            "max_jump_share": 0.9,
            "max_volatility_burst_ratio": 3.2,
            "min_breakout_distance_bps": 0.0
        },
        "engine_overrides": {
            "min_entry_confidence": 0.42,
            "strict_min_entry_score": 0.46,
            "strict_max_current_spread_bps": 420.0,
            "strict_max_avg_spread_bps": 380.0,
            "strict_max_wide_spread_rate": 0.55
        }
    },
    {
        "name": "strict_middle_anti_churn",
        "description": "Middle strict profile with tighter spread tolerance, more edge buffer, and less eager exits.",
        "strict_mode": True,
        "long_entry_version": "v2",
        "strict_long_entry_led": True,
        "strict_exit_style": "upgraded",
        "enable_maturity_gating": True,
        "enable_microstructure_gating": True,
        "long_entry_overrides": {
            "strict_min_price": 0.10,
            "strict_max_price": 0.72,
            "strict_min_return_bps": 90.0,
            "strict_max_pullback_bps": 320.0,
            "min_fast_momentum_bps": 15.0,
            "min_medium_momentum_bps": 45.0,
            "min_slow_momentum_bps": 90.0,
            "min_positive_closes": 2,
            "min_above_mean_bps": 10.0,
            "min_baseline_persistence": 0.48,
            "min_trend_efficiency": 0.18,
            "max_one_bar_jump_bps": 260.0,
            "max_jump_share": 0.9,
            "max_volatility_burst_ratio": 3.2,
            "min_breakout_distance_bps": 0.0
        },
        "engine_overrides": {
            "min_entry_confidence": 0.48,
            "strict_min_entry_score": 0.52,
            "strict_max_current_spread_bps": 220.0,
            "strict_max_avg_spread_bps": 220.0,
            "strict_max_wide_spread_rate": 0.35,
            "min_hold_bars": 5,
            "min_buy_sell_score_gap": 0.45,
            "estimated_round_trip_cost_bps": 95.0,
            "edge_cost_buffer_bps": 60.0,
            "strict_extended_hold_exit_gap": 0.22
        }
    },
    {
        "name": "strict_no_long_entry_led",
        "description": "Strict, but use old aggregate entry gating instead of long-entry-led entries.",
        "strict_mode": True,
        "long_entry_version": "v2",
        "strict_long_entry_led": False,
        "strict_exit_style": "upgraded",
        "enable_maturity_gating": True,
        "enable_microstructure_gating": True,
    },
    {
        "name": "strict_long_entry_legacy",
        "description": "Strict with the simpler legacy long-entry recipe.",
        "strict_mode": True,
        "long_entry_version": "legacy",
        "strict_long_entry_led": True,
        "strict_exit_style": "upgraded",
        "enable_maturity_gating": True,
        "enable_microstructure_gating": True,
    },
    {
        "name": "strict_exit_legacy",
        "description": "Strict with old-like simpler exits.",
        "strict_mode": True,
        "long_entry_version": "v2",
        "strict_long_entry_led": True,
        "strict_exit_style": "legacy",
        "enable_maturity_gating": True,
        "enable_microstructure_gating": True,
    },
    {
        "name": "strict_no_maturity_gate",
        "description": "Strict with maturity gating disabled.",
        "strict_mode": True,
        "long_entry_version": "v2",
        "strict_long_entry_led": True,
        "strict_exit_style": "upgraded",
        "enable_maturity_gating": False,
        "enable_microstructure_gating": True,
    },
    {
        "name": "strict_no_micro_gate",
        "description": "Strict with microstructure gating disabled.",
        "strict_mode": True,
        "long_entry_version": "v2",
        "strict_long_entry_led": True,
        "strict_exit_style": "upgraded",
        "enable_maturity_gating": True,
        "enable_microstructure_gating": False,
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a practical backtest ablation matrix across CSV files.")
    parser.add_argument("--csv", action="append", default=[], help="CSV path, directory, or glob. Repeatable.")
    parser.add_argument("--out", default="data/experiment_matrix.json", help="Where to write the JSON report.")
    parser.add_argument("--cash", type=float, default=1_000.0)
    parser.add_argument("--size", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=25.0)
    parser.add_argument("--fallback-half-spread-bps", type=float, default=50.0)
    parser.add_argument("--max-spread-bps", type=float, default=1500.0)
    parser.add_argument("--missing-quote-fill-ratio", type=float, default=1.0)
    parser.add_argument("--wide-spread-fill-ratio", type=float, default=0.5)
    parser.add_argument("--experiments", help="Optional JSON file with a list of experiment overrides.")
    parser.add_argument(
        "--family-filter",
        choices=["off", "on", "both"],
        default="off",
        help="Apply MarketFilter family/keyword gates to each CSV. 'both' runs on and off per CSV.",
    )
    return parser.parse_args()


def expand_csv_inputs(raw_inputs: list[str]) -> list[Path]:
    if not raw_inputs:
        raw_inputs = ["data/*.csv"]
    paths: list[Path] = []
    for item in raw_inputs:
        candidate = Path(item)
        if any(char in item for char in "*?[]"):
            paths.extend(sorted(Path().glob(item)))
        elif candidate.is_dir():
            paths.extend(sorted(candidate.glob("*.csv")))
        elif candidate.exists():
            paths.append(candidate)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def experiment_score(*, net_return_pct: float, expectancy: float | None, round_trip_count: int, max_drawdown_pct: float) -> float:
    expectancy_value = float(expectancy or 0.0)
    return round(
        max(0.0, min(35.0, net_return_pct * 3.5))
        + max(0.0, min(25.0, expectancy_value * 12.5))
        + max(0.0, min(20.0, round_trip_count * 2.0))
        + max(0.0, min(20.0, 20.0 - max_drawdown_pct * 2.0)),
        1,
    )


def build_engine_kwargs(args: argparse.Namespace, experiment: dict[str, Any]) -> dict[str, Any]:
    kwargs = {
        "starting_cash": args.cash,
        "per_trade_size": args.size,
        "slippage_bps": args.slippage_bps,
        "fallback_half_spread_bps": args.fallback_half_spread_bps,
        "max_spread_bps": args.max_spread_bps,
        "missing_quote_fill_ratio": args.missing_quote_fill_ratio,
        "wide_spread_fill_ratio": args.wide_spread_fill_ratio,
        "strict_mode": bool(experiment.get("strict_mode", False)),
        "strict_long_entry_led": bool(experiment.get("strict_long_entry_led", True)),
        "strict_exit_style": str(experiment.get("strict_exit_style", "upgraded")),
        "enable_maturity_gating": bool(experiment.get("enable_maturity_gating", True)),
        "enable_microstructure_gating": bool(experiment.get("enable_microstructure_gating", True)),
    }
    kwargs.update(experiment.get("engine_overrides", {}))
    return kwargs


def run_snapshots(
    snapshots: list[Any],
    snapshot_label: str,
    args: argparse.Namespace,
    experiment: dict[str, Any],
    *,
    market_filter: MarketFilter | None = None,
    market_metadata: dict[str, Any] | None = None,
    family_filter_mode: str = "off",
) -> dict[str, Any]:
    long_entry_kwargs = {
        "strict_mode": bool(experiment.get("strict_mode", False)),
        "signal_version": str(experiment.get("long_entry_version", "v2")),
    }
    long_entry_kwargs.update(experiment.get("long_entry_overrides", {}))
    strategies = [
        LongEntryStrategy(**long_entry_kwargs),
        MACDStrategy(),
        RSIStrategy(),
    ]
    engine_kwargs = build_engine_kwargs(args, experiment)
    if market_filter is not None and market_metadata is not None:
        engine_kwargs["market_filter"] = market_filter
        engine_kwargs["market_metadata"] = market_metadata
    engine = BacktestEngine(strategies=strategies, **engine_kwargs)
    result = engine.run(snapshots)
    metrics = deepcopy(result.metadata.get("metrics", {}))
    cost_attribution = deepcopy(result.metadata.get("cost_attribution", {}))
    strict_meta = deepcopy(result.metadata.get("strict_mode", {}))
    blocked_entries = strict_meta.get("blocked_entries", {}) if isinstance(strict_meta, dict) else {}
    top_blocks = sorted(blocked_entries.items(), key=lambda item: item[1], reverse=True)[:3]
    net_return_pct = (((result.mark_to_market_equity / result.starting_cash) - 1) * 100) if result.starting_cash else 0.0
    max_drawdown_pct = result.max_drawdown * 100
    score = experiment_score(
        net_return_pct=net_return_pct,
        expectancy=metrics.get("expectancy"),
        round_trip_count=int(metrics.get("round_trip_count") or 0),
        max_drawdown_pct=max_drawdown_pct,
    )
    brier = compute_trade_brier(result.trades)
    family_name = None
    if market_metadata:
        family_name = (market_metadata.get("market_family") or "").strip() or None
    if family_name is None and market_filter is not None and market_metadata:
        try:
            family_info = market_filter._classify_family(market_metadata)
            family_name = (family_info or {}).get("family") or None
        except Exception:
            family_name = None
    skipped_by_family = bool(result.metadata.get("skipped_by_family_filter"))
    family_filter_reason = result.metadata.get("family_filter_reason")
    return {
        "csv": snapshot_label,
        "snapshot_count": len(snapshots),
        "experiment": experiment["name"],
        "description": experiment.get("description"),
        "config": experiment,
        "family_filter_mode": family_filter_mode,
        "market_family": family_name,
        "skipped_by_family_filter": skipped_by_family,
        "family_filter_reason": family_filter_reason,
        "headline": {
            "net_return_pct": round(net_return_pct, 2),
            "expectancy": metrics.get("expectancy"),
            "max_drawdown_pct": round(max_drawdown_pct, 2),
            "trade_count": metrics.get("trade_count"),
            "round_trip_count": metrics.get("round_trip_count"),
            "win_rate": metrics.get("win_rate"),
            "time_in_market_ratio": metrics.get("time_in_market_ratio"),
            "score": score,
            "brier_score": brier.get("brier_score"),
            "brier_count": brier.get("count"),
        },
        "blocked_entries": blocked_entries,
        "top_blocked_entries": [{"reason": reason, "count": count} for reason, count in top_blocks if count],
        "strict_metadata": strict_meta,
        "metrics": metrics,
        "cost_attribution": cost_attribution,
        "brier": brier,
    }


def run_one(
    csv_path: Path,
    args: argparse.Namespace,
    experiment: dict[str, Any],
    *,
    market_filter: MarketFilter | None = None,
    market_metadata: dict[str, Any] | None = None,
    family_filter_mode: str = "off",
) -> dict[str, Any]:
    snapshots = load_snapshots_from_csv(csv_path)
    return run_snapshots(
        snapshots,
        str(csv_path),
        args,
        experiment,
        market_filter=market_filter,
        market_metadata=market_metadata,
        family_filter_mode=family_filter_mode,
    )


def summarize_experiment(rows: list[dict[str, Any]]) -> dict[str, Any]:
    net_returns = [float(row["headline"]["net_return_pct"]) for row in rows]
    expectancies = [float(row["headline"]["expectancy"] or 0.0) for row in rows]
    drawdowns = [float(row["headline"]["max_drawdown_pct"]) for row in rows]
    trades = [int(row["headline"]["trade_count"] or 0) for row in rows]
    wins = [row["headline"]["win_rate"] for row in rows if row["headline"]["win_rate"] is not None]
    time_in_market = [float(row["headline"]["time_in_market_ratio"] or 0.0) for row in rows]
    scores = [float(row["headline"]["score"]) for row in rows]
    blocked = defaultdict(int)
    for row in rows:
        for reason, count in (row.get("blocked_entries") or {}).items():
            blocked[reason] += int(count or 0)
    mode = rows[0].get("family_filter_mode", "off")
    base_name = rows[0]["experiment"]
    display_name = base_name if mode == "off" else f"{base_name}[filter_{mode}]"
    return {
        "experiment": display_name,
        "base_experiment": base_name,
        "family_filter_mode": mode,
        "description": rows[0].get("description"),
        "config": rows[0].get("config"),
        "market_count": len(rows),
        "avg_net_return_pct": round(statistics.fmean(net_returns), 3),
        "avg_expectancy": round(statistics.fmean(expectancies), 4),
        "avg_max_drawdown_pct": round(statistics.fmean(drawdowns), 3),
        "avg_trade_count": round(statistics.fmean(trades), 2),
        "avg_win_rate": round(statistics.fmean(wins), 4) if wins else None,
        "avg_time_in_market_ratio": round(statistics.fmean(time_in_market), 4),
        "avg_score": round(statistics.fmean(scores), 2),
        "top_blocked_reasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(blocked.items(), key=lambda item: item[1], reverse=True)[:5]
            if count
        ],
        "markets": [
            {
                "csv": row["csv"],
                **row["headline"],
            }
            for row in sorted(rows, key=lambda item: item["headline"]["score"], reverse=True)
        ],
    }


def build_toggle_summary(experiment_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    baseline = next((row for row in experiment_summaries if row["experiment"] == "strict_full"), None)
    if baseline is None:
        return []
    toggle_rows = []
    for row in experiment_summaries:
        if row["experiment"] == baseline["experiment"]:
            continue
        toggle_rows.append(
            {
                "experiment": row["experiment"],
                "vs": baseline["experiment"],
                "delta_avg_net_return_pct": round(row["avg_net_return_pct"] - baseline["avg_net_return_pct"], 3),
                "delta_avg_expectancy": round(row["avg_expectancy"] - baseline["avg_expectancy"], 4),
                "delta_avg_drawdown_pct": round(row["avg_max_drawdown_pct"] - baseline["avg_max_drawdown_pct"], 3),
                "delta_avg_trade_count": round(row["avg_trade_count"] - baseline["avg_trade_count"], 2),
                "delta_avg_win_rate": round((row["avg_win_rate"] or 0.0) - (baseline["avg_win_rate"] or 0.0), 4),
                "delta_avg_time_in_market_ratio": round(row["avg_time_in_market_ratio"] - baseline["avg_time_in_market_ratio"], 4),
                "delta_avg_score": round(row["avg_score"] - baseline["avg_score"], 2),
            }
        )
    return sorted(toggle_rows, key=lambda item: item["delta_avg_score"], reverse=True)


def build_ranked_summary(experiment_summaries: list[dict[str, Any]], toggle_summary: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(experiment_summaries, key=lambda item: item["avg_score"], reverse=True)
    best = ranked[0] if ranked else None
    notes: list[str] = []
    if best:
        notes.append(
            f"Best by average score: {best['experiment']} (score {best['avg_score']}, avg return {best['avg_net_return_pct']}%, avg expectancy {best['avg_expectancy']})."
        )
    for row in toggle_summary[:3]:
        direction = "helped" if row["delta_avg_score"] > 0 else "hurt" if row["delta_avg_score"] < 0 else "was neutral"
        turnover_note = "via lower turnover" if row["delta_avg_trade_count"] < 0 and row["delta_avg_expectancy"] >= 0 else "via better expectancy" if row["delta_avg_expectancy"] > 0 else "mostly by changing turnover" if row["delta_avg_trade_count"] != 0 else "without much turnover change"
        notes.append(
            f"{row['experiment']} {direction} vs strict_full (Δscore {row['delta_avg_score']}, Δexpectancy {row['delta_avg_expectancy']}, Δtrades {row['delta_avg_trade_count']}) {turnover_note}."
        )
    return {
        "ranked_experiments": [
            {
                "rank": index + 1,
                "experiment": row["experiment"],
                "avg_score": row["avg_score"],
                "avg_net_return_pct": row["avg_net_return_pct"],
                "avg_expectancy": row["avg_expectancy"],
                "avg_trade_count": row["avg_trade_count"],
                "avg_win_rate": row["avg_win_rate"],
                "avg_max_drawdown_pct": row["avg_max_drawdown_pct"],
            }
            for index, row in enumerate(ranked)
        ],
        "notes": notes,
    }


def summarize_by_family(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        family = row.get("market_family") or "unknown"
        mode = row.get("family_filter_mode", "off")
        key = f"{family}::filter_{mode}"
        groups[key].append(row)
    out: dict[str, Any] = {}
    for key, bucket in groups.items():
        briers = [float(r["headline"]["brier_score"]) for r in bucket if r["headline"].get("brier_score") is not None]
        trades = [int(r["headline"]["trade_count"] or 0) for r in bucket]
        returns = [float(r["headline"]["net_return_pct"] or 0.0) for r in bucket]
        wins = [r["headline"]["win_rate"] for r in bucket if r["headline"]["win_rate"] is not None]
        skipped = sum(1 for r in bucket if r.get("skipped_by_family_filter"))
        out[key] = {
            "run_count": len(bucket),
            "skipped_by_family_filter": skipped,
            "avg_trade_count": round(statistics.fmean(trades), 2) if trades else None,
            "avg_net_return_pct": round(statistics.fmean(returns), 3) if returns else None,
            "avg_win_rate": round(statistics.fmean(wins), 4) if wins else None,
            "avg_brier_score": round(statistics.fmean(briers), 6) if briers else None,
            "brier_sample_count": len(briers),
        }
    return out


def main() -> None:
    args = parse_args()
    csv_paths = expand_csv_inputs(args.csv)
    if not csv_paths:
        raise SystemExit("No CSV files found. Pass --csv path/to/file.csv, a directory, or a glob.")

    experiments = deepcopy(DEFAULT_EXPERIMENTS)
    if args.experiments:
        experiments = json.loads(Path(args.experiments).read_text())

    modes: list[str] = ["off"] if args.family_filter == "off" else ["on"] if args.family_filter == "on" else ["off", "on"]
    market_filter = build_market_filter_from_env() if "on" in modes else None
    metadata_cache: dict[str, dict[str, Any] | None] = {}

    detailed_rows: list[dict[str, Any]] = []
    for csv_path in csv_paths:
        key = str(csv_path.resolve())
        if key not in metadata_cache:
            metadata_cache[key] = extract_market_metadata(csv_path)
        meta = metadata_cache[key]
        for experiment in experiments:
            for mode in modes:
                active_filter = market_filter if mode == "on" else None
                active_meta = meta if mode == "on" else None
                detailed_rows.append(
                    run_one(
                        csv_path,
                        args,
                        experiment,
                        market_filter=active_filter,
                        market_metadata=active_meta,
                        family_filter_mode=mode,
                    )
                )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in detailed_rows:
        grouped_key = f"{row['experiment']}::filter_{row['family_filter_mode']}"
        grouped[grouped_key].append(row)

    experiment_summaries = [summarize_experiment(rows) for _, rows in grouped.items()]
    experiment_summaries.sort(key=lambda item: item["avg_score"], reverse=True)
    toggle_summary = build_toggle_summary(experiment_summaries)
    ranked_summary = build_ranked_summary(experiment_summaries, toggle_summary)
    by_family = summarize_by_family(detailed_rows)

    payload = {
        "summary": {
            "csv_count": len(csv_paths),
            "experiment_count": len(experiments),
            "total_runs": len(detailed_rows),
            "family_filter_modes": modes,
        },
        "inputs": {
            "csvs": [str(path) for path in csv_paths],
        },
        "ranked_summary": ranked_summary,
        "toggle_summary": toggle_summary,
        "by_family": by_family,
        "experiments": experiment_summaries,
        "runs": detailed_rows,
        "limitations": [
            "This runner compares backtest behavior on the CSV set you feed it; it does not by itself discover markets.",
            "LLM classifier and market-family filtering are selection-layer tools, so they are not cleanly isolatable from raw CSV-only backtests in the same way as entry/exit/gating toggles.",
            "Legacy long_entry and legacy strict exits are intentionally old-like approximations for comparison, not exact historical reproductions.",
        ],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps({"saved": str(out_path), "ranked_summary": ranked_summary}, indent=2, default=str))


if __name__ == "__main__":
    main()
