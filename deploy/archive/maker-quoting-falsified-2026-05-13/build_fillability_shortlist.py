from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a ranked fillability shortlist from fill-likelihood analysis.")
    parser.add_argument("--analysis", default="data/fill_likelihood_analysis.json")
    parser.add_argument("--out", default="data/fillability_shortlist.json")
    parser.add_argument("--window", default="10", help="Window key to rank on, e.g. 10 or 20")
    return parser.parse_args()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def main() -> None:
    args = parse_args()
    analysis = json.loads(Path(args.analysis).read_text())
    window = str(args.window)
    ranked: list[dict[str, Any]] = []

    for market in analysis.get("markets", []):
        at_touch = ((market.get("windows") or {}).get(window) or {}).get("at_touch") or {}
        one_tick = ((market.get("windows") or {}).get(window) or {}).get("one_tick_inside") or {}

        at_cross = float(at_touch.get("crossed_price_rate") or 0.0)
        inside_cross = float(one_tick.get("crossed_price_rate") or 0.0)
        never_touch = float(at_touch.get("never_touched_rate") or 1.0)
        spread = float(at_touch.get("avg_spread_bps") or 9999.0)
        move = float(at_touch.get("avg_abs_next_move_bps") or 0.0)

        cross_score = 55.0 * inside_cross + 35.0 * at_cross
        spread_score = 20.0 * clamp(1.0 - (spread / 400.0), 0.0, 1.0)
        move_score = 10.0 * clamp(move / 25.0, 0.0, 1.0)
        touch_penalty = 20.0 * never_touch
        fillability_score = round(cross_score + spread_score + move_score - touch_penalty, 2)

        ranked.append(
            {
                "question": market.get("question"),
                "outcome": market.get("outcome"),
                "token_id": market.get("token_id"),
                "csv": market.get("csv"),
                "window": window,
                "metrics": {
                    "at_touch_crossed_price_rate": round(at_cross, 4),
                    "one_tick_inside_crossed_price_rate": round(inside_cross, 4),
                    "never_touched_rate": round(never_touch, 4),
                    "avg_spread_bps": round(spread, 2),
                    "avg_abs_next_move_bps": round(move, 2),
                },
                "fillability_score": fillability_score,
            }
        )

    ranked.sort(key=lambda row: row["fillability_score"], reverse=True)
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx

    payload = {
        "window": window,
        "summary": {
            "market_count": len(ranked),
            "top_markets": ranked[:5],
        },
        "ranked": ranked,
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
