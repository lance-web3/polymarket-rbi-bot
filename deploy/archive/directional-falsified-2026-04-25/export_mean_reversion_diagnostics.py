from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.data import load_snapshots_from_csv
from strategies.long_entry_strategy import LongEntryStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export mean-reversion diagnostics against forward returns.")
    parser.add_argument("--csv", action="append", default=[], help="CSV path, directory, or glob. Repeatable.")
    parser.add_argument("--out", default="data/mean_reversion_diagnostics.csv", help="Flat CSV export path.")
    parser.add_argument("--summary-out", default="data/mean_reversion_diagnostics_summary.json", help="Summary JSON path.")
    return parser.parse_args()


def expand_csv_inputs(raw_inputs: list[str]) -> list[Path]:
    if not raw_inputs:
        raw_inputs = ["data/quote_backtests/*.csv"]
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


def safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def forward_return_bps(prices: list[float], index: int, horizon: int) -> float | None:
    target = index + horizon
    if target >= len(prices):
        return None
    start = prices[index]
    end = prices[target]
    if start <= 0:
        return None
    return ((end - start) / start) * 10_000


def bucket_name(value: float | None, thresholds: list[tuple[float, str]], default: str) -> str:
    if value is None:
        return "unknown"
    for threshold, name in thresholds:
        if value < threshold:
            return name
    return default


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def avg(items: list[float | None]) -> float | None:
        values = [float(v) for v in items if v is not None]
        return round(statistics.fmean(values), 2) if values else None

    summary: dict[str, Any] = {
        "row_count": len(rows),
        "candidate_count": sum(1 for row in rows if row["fade_candidate"]),
        "forward_returns": {
            "all": {
                "fwd_1_bar_bps": avg([row["fwd_1_bar_bps"] for row in rows]),
                "fwd_2_bar_bps": avg([row["fwd_2_bar_bps"] for row in rows]),
                "fwd_3_bar_bps": avg([row["fwd_3_bar_bps"] for row in rows]),
            },
            "fade_candidates": {
                "fwd_1_bar_bps": avg([row["fwd_1_bar_bps"] for row in rows if row["fade_candidate"]]),
                "fwd_2_bar_bps": avg([row["fwd_2_bar_bps"] for row in rows if row["fade_candidate"]]),
                "fwd_3_bar_bps": avg([row["fwd_3_bar_bps"] for row in rows if row["fade_candidate"]]),
            },
        },
    }

    bucket_specs = {
        "spread_bucket": [(80, "lt_80"), (150, "80_150"), (250, "150_250")],
        "slow_momentum_bucket": [(90, "lt_90"), (180, "90_180"), (300, "180_300")],
        "breakout_bucket": [(0.35, "lt_0.35"), (0.65, "0.35_0.65"), (0.85, "0.65_0.85")],
        "jump_share_bucket": [(0.35, "lt_0.35"), (0.60, "0.35_0.60"), (0.85, "0.60_0.85")],
    }
    buckets: dict[str, dict[str, Any]] = {}
    for field, thresholds in bucket_specs.items():
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            name = bucket_name(row.get(field), thresholds, f"ge_{thresholds[-1][0]}")
            grouped.setdefault(name, []).append(row)
        buckets[field] = {
            name: {
                "count": len(group_rows),
                "candidate_rate": round(sum(1 for item in group_rows if item["fade_candidate"]) / len(group_rows), 4) if group_rows else 0.0,
                "avg_fwd_1_bar_bps": avg([item["fwd_1_bar_bps"] for item in group_rows]),
                "avg_fwd_2_bar_bps": avg([item["fwd_2_bar_bps"] for item in group_rows]),
                "avg_fwd_3_bar_bps": avg([item["fwd_3_bar_bps"] for item in group_rows]),
            }
            for name, group_rows in grouped.items()
        }
    summary["buckets"] = buckets
    return summary


def main() -> None:
    args = parse_args()
    csv_paths = expand_csv_inputs(args.csv)
    if not csv_paths:
        raise SystemExit("No CSV files found.")

    feature_strategy = LongEntryStrategy(strict_mode=False, signal_version="v2")
    rows: list[dict[str, Any]] = []

    for csv_path in csv_paths:
        snapshots = load_snapshots_from_csv(csv_path)
        closes = [snapshot.candle.close for snapshot in snapshots]
        for index in range(len(snapshots)):
            history = snapshots[: index + 1]
            signal = feature_strategy.generate_signal(history)
            snapshot = snapshots[index]
            meta = signal.metadata or {}
            spread_bps = None
            if snapshot.best_bid is not None and snapshot.best_ask is not None and snapshot.mid_price > 0:
                spread_bps = ((snapshot.best_ask - snapshot.best_bid) / snapshot.mid_price) * 10_000
            slow_momentum_bps = safe_float(meta.get("slow_momentum_bps"))
            breakout_position = safe_float(meta.get("breakout_position"))
            jump_share = safe_float(meta.get("jump_share"))
            volatility_burst_ratio = safe_float(meta.get("volatility_burst_ratio"))
            fade_candidate = bool(
                slow_momentum_bps is not None
                and slow_momentum_bps >= 90.0
                and breakout_position is not None
                and breakout_position >= 0.65
                and jump_share is not None
                and jump_share >= 0.35
                and spread_bps is not None
                and spread_bps <= 150.0
                and volatility_burst_ratio is not None
                and volatility_burst_ratio >= 0.8
            )
            row = {
                "csv": csv_path.name,
                "timestamp": snapshot.candle.timestamp.isoformat(),
                "fade_candidate": fade_candidate,
                "price": snapshot.candle.close,
                "spread_bucket": spread_bps,
                "slow_momentum_bucket": slow_momentum_bps,
                "breakout_bucket": breakout_position,
                "jump_share_bucket": jump_share,
                "spread_bps": spread_bps,
                "return_bps": safe_float(meta.get("return_bps")),
                "pullback_bps": safe_float(meta.get("pullback_bps")),
                "above_mean_bps": safe_float(meta.get("above_mean_bps")),
                "realized_volatility_bps": safe_float(meta.get("realized_volatility_bps")),
                "positive_closes": safe_float(meta.get("positive_closes")),
                "breakout_position": breakout_position,
                "breakout_distance_bps": safe_float(meta.get("breakout_distance_bps")),
                "fast_momentum_bps": safe_float(meta.get("fast_momentum_bps")),
                "medium_momentum_bps": safe_float(meta.get("medium_momentum_bps")),
                "slow_momentum_bps": slow_momentum_bps,
                "momentum_alignment": safe_float(meta.get("momentum_alignment")),
                "baseline_persistence": safe_float(meta.get("baseline_persistence")),
                "trend_efficiency": safe_float(meta.get("trend_efficiency")),
                "largest_up_jump_bps": safe_float(meta.get("largest_up_jump_bps")),
                "jump_share": jump_share,
                "volatility_burst_ratio": volatility_burst_ratio,
                "fwd_1_bar_bps": forward_return_bps(closes, index, 1),
                "fwd_2_bar_bps": forward_return_bps(closes, index, 2),
                "fwd_3_bar_bps": forward_return_bps(closes, index, 3),
            }
            rows.append(row)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    summary = summarize(rows)
    summary_path = Path(args.summary_out)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps({"saved": str(out_path), "summary_saved": str(summary_path), "summary": summary}, indent=2))


if __name__ == "__main__":
    main()
