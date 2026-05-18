from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.data import load_snapshots_from_csv
from strategies.long_entry_strategy import LongEntryStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze fade trades under conservative maker fill proxy assumptions.")
    parser.add_argument("--csv-dir", default="data/quote_backtests")
    parser.add_argument("--out", default="data/fade_maker_proxy.json")
    parser.add_argument("--holds", default="3,5,10")
    parser.add_argument("--size", type=float, default=10.0)
    parser.add_argument("--min-slow-momentum-bps", type=float, default=90.0)
    parser.add_argument("--min-breakout-position", type=float, default=0.65)
    parser.add_argument("--min-jump-share", type=float, default=0.35)
    parser.add_argument("--max-jump-share", type=float, default=0.85)
    parser.add_argument("--max-source-spread-bps", type=float, default=150.0)
    parser.add_argument("--max-opposite-entry-spread-bps", type=float, default=150.0)
    parser.add_argument("--min-bars-between-trades", type=int, default=3)
    return parser.parse_args()


def load_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def spread_bps(snapshot) -> float | None:
    if snapshot.best_bid is not None and snapshot.best_ask is not None and snapshot.mid_price > 0:
        return ((snapshot.best_ask - snapshot.best_bid) / snapshot.mid_price) * 10_000
    return None


def summarize(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"avg": None, "total": 0.0, "win_rate": None}
    return {
        "avg": round(statistics.fmean(values), 4),
        "total": round(sum(values), 4),
        "win_rate": round(sum(1 for v in values if v > 0) / len(values), 4),
    }


def scenario_pnl(entry_bid: float, entry_ask: float, exit_bid: float, exit_ask: float, size: float, entry_fill: float, exit_fill: float) -> float:
    entry_price = (entry_bid * entry_fill) + (entry_ask * (1 - entry_fill))
    exit_price = (exit_ask * exit_fill) + (exit_bid * (1 - exit_fill))
    return (exit_price - entry_price) * size


def main() -> None:
    args = parse_args()
    holds = [int(item) for item in args.holds.split(",") if item.strip()]
    csv_dir = Path(args.csv_dir)
    paths = sorted(csv_dir.glob("*.csv"))
    by_condition: dict[str, list[Path]] = defaultdict(list)
    for path in paths:
        rows = load_csv_rows(path)
        if not rows:
            continue
        by_condition[rows[0].get("condition_id") or "unknown"].append(path)

    feature_strategy = LongEntryStrategy(strict_mode=False, signal_version="v2")
    payload: dict[str, Any] = {"config": vars(args), "holds": []}
    proxy_scenarios = {
        "perfect_maker": (1.0, 1.0),
        "optimistic_proxy": (0.75, 0.75),
        "moderate_proxy": (0.50, 0.50),
        "tough_proxy": (0.25, 0.25),
        "maker_entry_only": (1.0, 0.0),
        "maker_exit_only": (0.0, 1.0),
        "taker_taker": (0.0, 0.0),
    }

    for hold_bars in holds:
        scenario_values: dict[str, list[float]] = defaultdict(list)
        trade_count = 0
        for condition_id, pair_paths in sorted(by_condition.items()):
            if len(pair_paths) != 2:
                continue
            for source_path, opposite_path in ((pair_paths[0], pair_paths[1]), (pair_paths[1], pair_paths[0])):
                source = load_snapshots_from_csv(source_path)
                opposite = load_snapshots_from_csv(opposite_path)
                next_allowed_index = 0
                max_index = min(len(source), len(opposite)) - hold_bars - 1
                for index in range(max_index):
                    if index < next_allowed_index:
                        continue
                    signal = feature_strategy.generate_signal(source[: index + 1])
                    meta = signal.metadata or {}
                    slow_momentum_bps = float(meta.get("slow_momentum_bps") or 0.0) if meta.get("slow_momentum_bps") not in {None, ""} else None
                    breakout_position = float(meta.get("breakout_position") or 0.0) if meta.get("breakout_position") not in {None, ""} else None
                    jump_share = float(meta.get("jump_share") or 0.0) if meta.get("jump_share") not in {None, ""} else None
                    source_spread = spread_bps(source[index])
                    opposite_entry = opposite[index + 1]
                    opposite_exit = opposite[index + 1 + hold_bars]
                    opposite_entry_spread = spread_bps(opposite_entry)
                    is_candidate = (
                        slow_momentum_bps is not None
                        and slow_momentum_bps >= args.min_slow_momentum_bps
                        and breakout_position is not None
                        and breakout_position >= args.min_breakout_position
                        and jump_share is not None
                        and jump_share >= args.min_jump_share
                        and jump_share <= args.max_jump_share
                        and source_spread is not None
                        and source_spread <= args.max_source_spread_bps
                        and opposite_entry.best_bid is not None
                        and opposite_entry.best_ask is not None
                        and opposite_exit.best_bid is not None
                        and opposite_exit.best_ask is not None
                        and opposite_entry_spread is not None
                        and opposite_entry_spread <= args.max_opposite_entry_spread_bps
                    )
                    if not is_candidate:
                        continue
                    trade_count += 1
                    entry_bid = float(opposite_entry.best_bid)
                    entry_ask = float(opposite_entry.best_ask)
                    exit_bid = float(opposite_exit.best_bid)
                    exit_ask = float(opposite_exit.best_ask)
                    for name, (entry_fill, exit_fill) in proxy_scenarios.items():
                        scenario_values[name].append(
                            scenario_pnl(entry_bid, entry_ask, exit_bid, exit_ask, args.size, entry_fill, exit_fill)
                        )
                    next_allowed_index = index + 1 + args.min_bars_between_trades
        payload["holds"].append(
            {
                "hold_bars": hold_bars,
                "trade_count": trade_count,
                "scenarios": {name: summarize(values) for name, values in scenario_values.items()},
            }
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
