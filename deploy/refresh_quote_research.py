from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _run(command: list[str]) -> None:
    completed = subprocess.run(command, check=True, text=True, capture_output=True)
    if completed.stdout.strip():
        print(completed.stdout.strip())


def _summarize_top_tokens(payload: dict) -> list[dict[str, object]]:
    runs = payload.get("runs") or []
    token_rows = []
    for row in runs:
        headline = row.get("headline") or {}
        if row.get("experiment") != "loose_baseline":
            continue
        token_rows.append(
            {
                "csv": row.get("csv"),
                "score": headline.get("score"),
                "trade_count": headline.get("trade_count"),
                "round_trip_count": headline.get("round_trip_count"),
                "net_return_pct": headline.get("net_return_pct"),
                "expectancy": headline.get("expectancy"),
                "max_drawdown_pct": headline.get("max_drawdown_pct"),
                "win_rate": headline.get("win_rate"),
                "time_in_market_ratio": headline.get("time_in_market_ratio"),
            }
        )
    token_rows.sort(
        key=lambda row: (
            float(row.get("trade_count") or 0),
            float(row.get("round_trip_count") or 0),
            float(row.get("score") or 0),
            float(row.get("expectancy") or 0),
        ),
        reverse=True,
    )
    return token_rows[:5]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebuild quote-backtest CSVs from collected quote snapshots, rerun the experiment matrix, and print a compact summary."
    )
    parser.add_argument("--input", default="data/quote_snapshots.jsonl", help="Collected quote JSONL input")
    parser.add_argument("--output-dir", default="data/quote_backtests", help="Per-token backtest CSV output directory")
    parser.add_argument("--matrix-out", default="data/quote_backtests_matrix.json", help="Experiment matrix JSON output path")
    parser.add_argument("--missing-quote-fill-ratio", type=float, default=0.0)
    parser.add_argument("--wide-spread-fill-ratio", type=float, default=0.5)
    args = parser.parse_args()

    python = sys.executable

    _run(
        [
            python,
            "-m",
            "deploy.build_quote_snapshot_csv",
            "--input",
            args.input,
            "--output-dir",
            args.output_dir,
        ]
    )

    _run(
        [
            python,
            "-m",
            "deploy.run_experiment_matrix",
            "--csv",
            args.output_dir,
            "--out",
            args.matrix_out,
            "--missing-quote-fill-ratio",
            str(args.missing_quote_fill_ratio),
            "--wide-spread-fill-ratio",
            str(args.wide_spread_fill_ratio),
        ]
    )

    payload = json.loads(Path(args.matrix_out).read_text())
    ranked = ((payload.get("ranked_summary") or {}).get("ranked_experiments") or [])
    top = ranked[:3]
    experiments = payload.get("experiments") or []

    compact = {
        "quote_csv_count": ((payload.get("summary") or {}).get("csv_count")),
        "experiment_count": ((payload.get("summary") or {}).get("experiment_count")),
        "top_experiments": top,
        "top_tokens_loose_baseline": _summarize_top_tokens(payload),
        "top_blockers": {
            exp.get("experiment"): exp.get("top_blocked_reasons")
            for exp in experiments
        },
        "matrix_out": args.matrix_out,
    }
    print(json.dumps(compact, indent=2))


if __name__ == "__main__":
    main()
