from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.data import load_snapshots_from_csv
from strategies.long_entry_strategy import LongEntryStrategy


@dataclass(slots=True)
class Trade:
    condition_id: str
    source_csv: str
    opposite_csv: str
    source_outcome: str
    opposite_outcome: str
    entry_index: int
    exit_index: int
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    return_bps: float
    source_slow_momentum_bps: float | None
    source_breakout_position: float | None
    source_jump_share: float | None
    source_spread_bps: float | None
    opposite_entry_spread_bps: float | None
    opposite_exit_spread_bps: float | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prototype fade backtest by buying the opposite token after stretched moves.")
    parser.add_argument("--csv-dir", default="data/quote_backtests", help="Directory containing quote backtest CSVs.")
    parser.add_argument("--out", default="data/fade_pair_backtest.json", help="Output JSON path.")
    parser.add_argument("--hold-bars", type=int, default=3)
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


def summarize_trades(trades: list[Trade]) -> dict[str, Any]:
    if not trades:
        return {
            "trade_count": 0,
            "avg_pnl": 0.0,
            "avg_return_bps": 0.0,
            "win_rate": None,
            "total_pnl": 0.0,
        }
    pnls = [trade.pnl for trade in trades]
    returns = [trade.return_bps for trade in trades]
    wins = [trade for trade in trades if trade.pnl > 0]
    return {
        "trade_count": len(trades),
        "avg_pnl": round(statistics.fmean(pnls), 4),
        "avg_return_bps": round(statistics.fmean(returns), 2),
        "win_rate": round(len(wins) / len(trades), 4),
        "total_pnl": round(sum(pnls), 4),
    }


def main() -> None:
    args = parse_args()
    csv_dir = Path(args.csv_dir)
    paths = sorted(csv_dir.glob("*.csv"))
    if not paths:
        raise SystemExit("No CSV files found.")

    by_condition: dict[str, list[Path]] = defaultdict(list)
    meta_by_path: dict[str, dict[str, Any]] = {}
    for path in paths:
        rows = load_csv_rows(path)
        if not rows:
            continue
        meta_by_path[str(path)] = rows[0]
        condition_id = rows[0].get("condition_id") or "unknown"
        by_condition[condition_id].append(path)

    feature_strategy = LongEntryStrategy(strict_mode=False, signal_version="v2")
    all_trades: list[Trade] = []
    per_pair: list[dict[str, Any]] = []

    for condition_id, pair_paths in sorted(by_condition.items()):
        if len(pair_paths) != 2:
            continue
        path_a, path_b = pair_paths
        rows_a = load_csv_rows(path_a)
        rows_b = load_csv_rows(path_b)
        snaps_a = load_snapshots_from_csv(path_a)
        snaps_b = load_snapshots_from_csv(path_b)
        meta_a = meta_by_path[str(path_a)]
        meta_b = meta_by_path[str(path_b)]

        pair_trades: list[Trade] = []
        next_allowed_index = 0
        max_index = min(len(snaps_a), len(snaps_b)) - args.hold_bars - 1
        for index in range(max_index):
            if index < next_allowed_index:
                continue
            history_a = snaps_a[: index + 1]
            signal_a = feature_strategy.generate_signal(history_a)
            feature_meta = signal_a.metadata or {}
            slow_momentum_bps = float(feature_meta.get("slow_momentum_bps") or 0.0) if feature_meta.get("slow_momentum_bps") not in {None, ""} else None
            breakout_position = float(feature_meta.get("breakout_position") or 0.0) if feature_meta.get("breakout_position") not in {None, ""} else None
            jump_share = float(feature_meta.get("jump_share") or 0.0) if feature_meta.get("jump_share") not in {None, ""} else None
            source_spread = spread_bps(snaps_a[index])
            opposite_entry = snaps_b[index + 1]
            opposite_exit = snaps_b[index + 1 + args.hold_bars]
            opposite_entry_spread = spread_bps(opposite_entry)
            opposite_exit_spread = spread_bps(opposite_exit)

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
                and opposite_entry.best_ask is not None
                and opposite_exit.best_bid is not None
                and opposite_entry_spread is not None
                and opposite_entry_spread <= args.max_opposite_entry_spread_bps
            )
            if not is_candidate:
                continue

            entry_price = float(opposite_entry.best_ask)
            exit_price = float(opposite_exit.best_bid)
            pnl = (exit_price - entry_price) * args.size
            return_bps = ((exit_price - entry_price) / entry_price) * 10_000 if entry_price > 0 else 0.0
            trade = Trade(
                condition_id=condition_id,
                source_csv=path_a.name,
                opposite_csv=path_b.name,
                source_outcome=str(meta_a.get("outcome") or ""),
                opposite_outcome=str(meta_b.get("outcome") or ""),
                entry_index=index + 1,
                exit_index=index + 1 + args.hold_bars,
                entry_ts=opposite_entry.candle.timestamp.isoformat(),
                exit_ts=opposite_exit.candle.timestamp.isoformat(),
                entry_price=entry_price,
                exit_price=exit_price,
                size=args.size,
                pnl=pnl,
                return_bps=return_bps,
                source_slow_momentum_bps=slow_momentum_bps,
                source_breakout_position=breakout_position,
                source_jump_share=jump_share,
                source_spread_bps=source_spread,
                opposite_entry_spread_bps=opposite_entry_spread,
                opposite_exit_spread_bps=opposite_exit_spread,
            )
            pair_trades.append(trade)
            all_trades.append(trade)
            next_allowed_index = index + 1 + args.min_bars_between_trades

        per_pair.append(
            {
                "condition_id": condition_id,
                "source_csv": path_a.name,
                "opposite_csv": path_b.name,
                "source_outcome": meta_a.get("outcome"),
                "opposite_outcome": meta_b.get("outcome"),
                "summary": summarize_trades(pair_trades),
                "sample_trades": [asdict(trade) for trade in pair_trades[:5]],
            }
        )

        # run reverse direction too
        pair_trades = []
        next_allowed_index = 0
        for index in range(max_index):
            if index < next_allowed_index:
                continue
            history_b = snaps_b[: index + 1]
            signal_b = feature_strategy.generate_signal(history_b)
            feature_meta = signal_b.metadata or {}
            slow_momentum_bps = float(feature_meta.get("slow_momentum_bps") or 0.0) if feature_meta.get("slow_momentum_bps") not in {None, ""} else None
            breakout_position = float(feature_meta.get("breakout_position") or 0.0) if feature_meta.get("breakout_position") not in {None, ""} else None
            jump_share = float(feature_meta.get("jump_share") or 0.0) if feature_meta.get("jump_share") not in {None, ""} else None
            source_spread = spread_bps(snaps_b[index])
            opposite_entry = snaps_a[index + 1]
            opposite_exit = snaps_a[index + 1 + args.hold_bars]
            opposite_entry_spread = spread_bps(opposite_entry)
            opposite_exit_spread = spread_bps(opposite_exit)
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
                and opposite_entry.best_ask is not None
                and opposite_exit.best_bid is not None
                and opposite_entry_spread is not None
                and opposite_entry_spread <= args.max_opposite_entry_spread_bps
            )
            if not is_candidate:
                continue
            entry_price = float(opposite_entry.best_ask)
            exit_price = float(opposite_exit.best_bid)
            pnl = (exit_price - entry_price) * args.size
            return_bps = ((exit_price - entry_price) / entry_price) * 10_000 if entry_price > 0 else 0.0
            trade = Trade(
                condition_id=condition_id,
                source_csv=path_b.name,
                opposite_csv=path_a.name,
                source_outcome=str(meta_b.get("outcome") or ""),
                opposite_outcome=str(meta_a.get("outcome") or ""),
                entry_index=index + 1,
                exit_index=index + 1 + args.hold_bars,
                entry_ts=opposite_entry.candle.timestamp.isoformat(),
                exit_ts=opposite_exit.candle.timestamp.isoformat(),
                entry_price=entry_price,
                exit_price=exit_price,
                size=args.size,
                pnl=pnl,
                return_bps=return_bps,
                source_slow_momentum_bps=slow_momentum_bps,
                source_breakout_position=breakout_position,
                source_jump_share=jump_share,
                source_spread_bps=source_spread,
                opposite_entry_spread_bps=opposite_entry_spread,
                opposite_exit_spread_bps=opposite_exit_spread,
            )
            pair_trades.append(trade)
            all_trades.append(trade)
            next_allowed_index = index + 1 + args.min_bars_between_trades

        per_pair.append(
            {
                "condition_id": condition_id,
                "source_csv": path_b.name,
                "opposite_csv": path_a.name,
                "source_outcome": meta_b.get("outcome"),
                "opposite_outcome": meta_a.get("outcome"),
                "summary": summarize_trades(pair_trades),
                "sample_trades": [asdict(trade) for trade in pair_trades[:5]],
            }
        )

    payload = {
        "config": vars(args),
        "summary": summarize_trades(all_trades),
        "pairs": per_pair,
        "sample_trades": [asdict(trade) for trade in all_trades[:20]],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
