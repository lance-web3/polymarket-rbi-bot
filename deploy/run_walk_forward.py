from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.market_filter import MarketFilter
from data.csv_metadata import extract_market_metadata
from deploy.run_experiment_matrix import (
    DEFAULT_EXPERIMENTS,
    build_market_filter_from_env,
    expand_csv_inputs,
    run_snapshots,
)
from polymarket_rbi_bot.data import load_snapshots_from_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a simple walk-forward experiment selection test across CSV files.")
    parser.add_argument("--csv", action="append", default=[], help="CSV path, directory, or glob. Repeatable.")
    parser.add_argument("--out", default="data/walk_forward.json", help="Where to write the JSON report.")
    parser.add_argument("--cash", type=float, default=1_000.0)
    parser.add_argument("--size", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=25.0)
    parser.add_argument("--fallback-half-spread-bps", type=float, default=50.0)
    parser.add_argument("--max-spread-bps", type=float, default=1500.0)
    parser.add_argument("--missing-quote-fill-ratio", type=float, default=1.0)
    parser.add_argument("--wide-spread-fill-ratio", type=float, default=0.5)
    parser.add_argument("--experiments", help="Optional JSON file with a list of experiment overrides.")
    parser.add_argument("--train-bars", type=int, default=200, help="Training window size in bars.")
    parser.add_argument("--test-bars", type=int, default=50, help="Out-of-sample test window size in bars.")
    parser.add_argument("--step-bars", type=int, default=50, help="How far to roll the window each step.")
    parser.add_argument("--min-train-round-trips", type=int, default=0, help="Skip train winners with fewer than this many round trips if alternatives exist.")
    parser.add_argument(
        "--family-filter",
        choices=["off", "on"],
        default="off",
        help="Apply MarketFilter family/keyword gates to each CSV.",
    )
    return parser.parse_args()


def _choose_train_winner(rows: list[dict[str, Any]], min_train_round_trips: int) -> dict[str, Any]:
    ranked = sorted(
        rows,
        key=lambda row: (
            int((row.get("headline") or {}).get("round_trip_count") or 0) >= min_train_round_trips,
            float((row.get("headline") or {}).get("score") or 0.0),
            float((row.get("headline") or {}).get("expectancy") or 0.0),
            float((row.get("headline") or {}).get("net_return_pct") or 0.0),
        ),
        reverse=True,
    )
    return ranked[0]


def _avg(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def run_walk_forward_for_csv(
    csv_path: Path,
    args: argparse.Namespace,
    experiments: list[dict[str, Any]],
    *,
    market_filter: MarketFilter | None = None,
    market_metadata: dict[str, Any] | None = None,
    family_filter_mode: str = "off",
) -> dict[str, Any]:
    snapshots = load_snapshots_from_csv(csv_path)
    total = len(snapshots)
    if total < args.train_bars + args.test_bars:
        return {
            "csv": str(csv_path),
            "snapshot_count": total,
            "market_family": (market_metadata or {}).get("market_family") if market_metadata else None,
            "windows": [],
            "summary": {
                "window_count": 0,
                "reason": f"not enough snapshots for train={args.train_bars} and test={args.test_bars}",
            },
        }

    run_kwargs = {
        "market_filter": market_filter,
        "market_metadata": market_metadata,
        "family_filter_mode": family_filter_mode,
    }

    windows: list[dict[str, Any]] = []
    step = max(args.step_bars, 1)
    start = 0
    while start + args.train_bars + args.test_bars <= total:
        train_start = start
        train_end = start + args.train_bars
        test_end = train_end + args.test_bars
        train = snapshots[train_start:train_end]
        test = snapshots[train_end:test_end]

        train_rows = [
            run_snapshots(train, f"{csv_path}#train[{train_start}:{train_end}]", args, experiment, **run_kwargs)
            for experiment in experiments
        ]
        train_winner = _choose_train_winner(train_rows, args.min_train_round_trips)
        chosen_experiment_name = train_winner["experiment"]
        chosen_experiment = next(exp for exp in experiments if exp["name"] == chosen_experiment_name)
        test_chosen = run_snapshots(test, f"{csv_path}#test[{train_end}:{test_end}]", args, chosen_experiment, **run_kwargs)
        test_all = [
            run_snapshots(test, f"{csv_path}#test[{train_end}:{test_end}]", args, experiment, **run_kwargs)
            for experiment in experiments
        ]
        best_test = max(test_all, key=lambda row: float((row.get("headline") or {}).get("score") or 0.0))

        windows.append(
            {
                "train_range": {"start": train_start, "end": train_end},
                "test_range": {"start": train_end, "end": test_end},
                "selected_experiment": chosen_experiment_name,
                "train_winner": train_winner,
                "test_selected": test_chosen,
                "test_best_experiment": best_test["experiment"],
                "test_best_headline": best_test["headline"],
                "selected_was_test_best": chosen_experiment_name == best_test["experiment"],
            }
        )
        start += step

    selected_test_scores = [float((row["test_selected"]["headline"] or {}).get("score") or 0.0) for row in windows]
    selected_test_returns = [float((row["test_selected"]["headline"] or {}).get("net_return_pct") or 0.0) for row in windows]
    selected_test_expectancies = [float((row["test_selected"]["headline"] or {}).get("expectancy") or 0.0) for row in windows]
    selected_test_drawdowns = [float((row["test_selected"]["headline"] or {}).get("max_drawdown_pct") or 0.0) for row in windows]
    selected_round_trips = [int((row["test_selected"]["headline"] or {}).get("round_trip_count") or 0) for row in windows]
    pick_counter = Counter(row["selected_experiment"] for row in windows)

    return {
        "csv": str(csv_path),
        "snapshot_count": total,
        "windows": windows,
        "summary": {
            "window_count": len(windows),
            "avg_selected_test_score": _avg(selected_test_scores),
            "avg_selected_test_return_pct": _avg(selected_test_returns),
            "avg_selected_test_expectancy": _avg(selected_test_expectancies),
            "avg_selected_test_drawdown_pct": _avg(selected_test_drawdowns),
            "avg_selected_test_round_trips": _avg([float(v) for v in selected_round_trips]),
            "selected_was_test_best_rate": round(sum(1 for row in windows if row["selected_was_test_best"]) / len(windows), 4) if windows else None,
            "selected_experiment_counts": dict(sorted(pick_counter.items())),
        },
    }


def main() -> None:
    args = parse_args()
    csv_paths = expand_csv_inputs(args.csv)
    if not csv_paths:
        raise SystemExit("No CSV files found. Pass --csv path/to/file.csv, a directory, or a glob.")

    experiments = deepcopy(DEFAULT_EXPERIMENTS)
    if args.experiments:
        experiments = json.loads(Path(args.experiments).read_text())

    market_filter = build_market_filter_from_env() if args.family_filter == "on" else None
    per_csv: list[dict[str, Any]] = []
    for csv_path in csv_paths:
        meta = extract_market_metadata(csv_path) if args.family_filter == "on" else None
        per_csv.append(
            run_walk_forward_for_csv(
                csv_path,
                args,
                experiments,
                market_filter=market_filter,
                market_metadata=meta,
                family_filter_mode=args.family_filter,
            )
        )
    usable = [row for row in per_csv if int((row.get("summary") or {}).get("window_count") or 0) > 0]

    aggregate_scores = [float(row["summary"]["avg_selected_test_score"]) for row in usable if row["summary"].get("avg_selected_test_score") is not None]
    aggregate_returns = [float(row["summary"]["avg_selected_test_return_pct"]) for row in usable if row["summary"].get("avg_selected_test_return_pct") is not None]
    aggregate_expectancies = [float(row["summary"]["avg_selected_test_expectancy"]) for row in usable if row["summary"].get("avg_selected_test_expectancy") is not None]
    aggregate_best_rate = [float(row["summary"]["selected_was_test_best_rate"]) for row in usable if row["summary"].get("selected_was_test_best_rate") is not None]
    aggregate_pick_counts = Counter()
    for row in usable:
        aggregate_pick_counts.update(row["summary"].get("selected_experiment_counts") or {})

    payload = {
        "summary": {
            "csv_count": len(csv_paths),
            "usable_csv_count": len(usable),
            "train_bars": args.train_bars,
            "test_bars": args.test_bars,
            "step_bars": args.step_bars,
            "avg_selected_test_score": _avg(aggregate_scores),
            "avg_selected_test_return_pct": _avg(aggregate_returns),
            "avg_selected_test_expectancy": _avg(aggregate_expectancies),
            "avg_selected_was_test_best_rate": _avg(aggregate_best_rate),
            "selected_experiment_counts": dict(sorted(aggregate_pick_counts.items())),
        },
        "inputs": {
            "csvs": [str(path) for path in csv_paths],
            "experiment_names": [experiment["name"] for experiment in experiments],
        },
        "per_csv": per_csv,
        "limitations": [
            "This is a first-pass walk-forward selection test, not full parameter optimization over a continuous search space.",
            "It chooses among the predefined experiment profiles on each training window, then evaluates the chosen profile on the next out-of-sample window.",
            "If windows are small or markets are sparse, results will still be noisy; treat this as an honesty upgrade, not proof of edge.",
        ],
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(json.dumps({"saved": str(out_path), "summary": payload["summary"]}, indent=2, default=str))


if __name__ == "__main__":
    main()
