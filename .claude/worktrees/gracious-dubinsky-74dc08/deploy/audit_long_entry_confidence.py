"""Diagnostic: histogram LongEntry signal.confidence across snapshots.

Phase 2 found 0 trades from every strict experiment on the 10 sports CSVs while
loose_baseline made 100+. The hypothesis is one of:

  (a) Signal is dead: confidence ≈ 0 on >99% of bars (universe is flat;
      LongEntry can't fire because momentum/return preconditions fail).
  (b) Threshold is wrong: confidence clusters between, say, 0.30 and 0.45,
      but MIN_ENTRY_CONFIDENCE = 0.50 chops it all off.

This walks each CSV through LongEntryStrategy(strict_mode=True, signal_version=v2)
and collects per-bar signal.confidence + signal.side. Distinguishing (a) vs (b)
is the difference between "find a different strategy" and "loosen one knob."

Usage:
    python -m deploy.audit_long_entry_confidence \
        --csv-dir data/quote_backtests \
        --out data/long_entry_confidence_audit.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.data import load_snapshots_from_csv
from polymarket_rbi_bot.models import SignalSide
from strategies.long_entry_strategy import LongEntryStrategy


BUCKETS = [0.0, 0.10, 0.20, 0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 0.90, 1.0001]


def histogram(values: list[float]) -> list[dict[str, Any]]:
    out = []
    for lo, hi in zip(BUCKETS[:-1], BUCKETS[1:]):
        count = sum(1 for v in values if lo <= v < hi)
        out.append({"lo": lo, "hi": hi, "count": count})
    return out


def audit_csv(csv_path: Path, min_entry_confidence: float) -> dict[str, Any]:
    snapshots = list(load_snapshots_from_csv(str(csv_path)))
    strategy = LongEntryStrategy(strict_mode=True, signal_version="v2")

    confidences: list[float] = []
    buy_confidences: list[float] = []
    sell_confidences: list[float] = []
    side_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    bars_above_threshold = 0
    bars_above_threshold_buy = 0

    for end_idx in range(1, len(snapshots) + 1):
        history = snapshots[:end_idx]
        signal = strategy.generate_signal(history)
        c = float(signal.confidence)
        confidences.append(c)
        side_counts[signal.side.value] = side_counts.get(signal.side.value, 0) + 1
        if signal.side == SignalSide.BUY:
            buy_confidences.append(c)
            if c >= min_entry_confidence:
                bars_above_threshold_buy += 1
        elif signal.side == SignalSide.SELL:
            sell_confidences.append(c)
        if c >= min_entry_confidence:
            bars_above_threshold += 1

    nonzero = [c for c in confidences if c > 0.0]
    return {
        "csv": csv_path.name,
        "snapshot_count": len(snapshots),
        "side_counts": side_counts,
        "min_entry_confidence_threshold": min_entry_confidence,
        "bars_above_threshold_any_side": bars_above_threshold,
        "bars_above_threshold_buy_only": bars_above_threshold_buy,
        "fraction_zero": round(1 - len(nonzero) / max(len(confidences), 1), 4),
        "all_confidence": {
            "n": len(confidences),
            "max": max(confidences) if confidences else 0.0,
            "mean": round(statistics.fmean(confidences), 4) if confidences else 0.0,
            "p50": round(statistics.median(confidences), 4) if confidences else 0.0,
            "p90": round(statistics.quantiles(confidences, n=10)[-1], 4) if len(confidences) >= 10 else None,
            "p95": round(statistics.quantiles(confidences, n=20)[-1], 4) if len(confidences) >= 20 else None,
            "p99": round(statistics.quantiles(confidences, n=100)[-1], 4) if len(confidences) >= 100 else None,
        },
        "buy_confidence": {
            "n": len(buy_confidences),
            "max": max(buy_confidences) if buy_confidences else 0.0,
            "mean": round(statistics.fmean(buy_confidences), 4) if buy_confidences else 0.0,
        },
        "histogram": histogram(confidences),
        "buy_histogram": histogram(buy_confidences),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--csv-dir", type=Path, default=Path("data/quote_backtests"))
    parser.add_argument("--csv", action="append", default=[], help="Specific CSV(s); overrides --csv-dir.")
    parser.add_argument("--out", type=Path, default=Path("data/long_entry_confidence_audit.json"))
    args = parser.parse_args()

    cfg = BotConfig.from_env()
    threshold = cfg.min_entry_confidence

    if args.csv:
        csvs = [Path(c) for c in args.csv]
    else:
        csvs = sorted(args.csv_dir.glob("quote_backtest_*.csv"))

    print(f"MIN_ENTRY_CONFIDENCE = {threshold}")
    print(f"CSVs: {len(csvs)}")
    per_csv = []
    aggregate_conf: list[float] = []
    aggregate_side_counts = {"BUY": 0, "SELL": 0, "HOLD": 0}
    aggregate_above_thresh = 0
    aggregate_above_thresh_buy = 0
    aggregate_buy_conf: list[float] = []
    for c in csvs:
        try:
            row = audit_csv(c, threshold)
        except Exception as exc:  # noqa: BLE001
            print(f"  [skip] {c.name}: {exc}")
            continue
        per_csv.append(row)
        aggregate_side_counts["BUY"] += row["side_counts"].get("BUY", 0)
        aggregate_side_counts["SELL"] += row["side_counts"].get("SELL", 0)
        aggregate_side_counts["HOLD"] += row["side_counts"].get("HOLD", 0)
        aggregate_above_thresh += row["bars_above_threshold_any_side"]
        aggregate_above_thresh_buy += row["bars_above_threshold_buy_only"]
        print(
            f"  {c.name[:40]}... bars={row['snapshot_count']} "
            f"BUY={row['side_counts']['BUY']} "
            f"buy_p_max={row['buy_confidence']['max']} "
            f"≥{threshold:.2f}={row['bars_above_threshold_buy_only']}"
        )

    print()
    print("=== AGGREGATE ===")
    print(f"  Total bars             : {sum(r['snapshot_count'] for r in per_csv)}")
    print(f"  Side counts            : {aggregate_side_counts}")
    print(f"  Bars above threshold   : {aggregate_above_thresh}")
    print(f"  BUY bars above thresh  : {aggregate_above_thresh_buy}")

    payload = {
        "min_entry_confidence_threshold": threshold,
        "csv_count": len(per_csv),
        "aggregate_side_counts": aggregate_side_counts,
        "aggregate_bars_above_threshold_any_side": aggregate_above_thresh,
        "aggregate_bars_above_threshold_buy_only": aggregate_above_thresh_buy,
        "per_csv": per_csv,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
