from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


TICK = 0.001


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate passive fill likelihood proxies from quote snapshot CSVs.")
    parser.add_argument("--csv-dir", default="data/quote_backtests")
    parser.add_argument("--out", default="data/fill_likelihood_analysis.json")
    parser.add_argument("--windows", default="10,20", help="Window lengths in bars (about 30s each in current data).")
    return parser.parse_args()


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(records)
    if not count:
        return {"count": 0}
    def rate(key: str) -> float:
        return round(sum(1 for row in records if row.get(key)) / count, 4)
    def avg(key: str) -> float | None:
        vals = [float(row[key]) for row in records if row.get(key) is not None]
        return round(statistics.fmean(vals), 2) if vals else None
    return {
        "count": count,
        "avg_spread_bps": avg("spread_bps"),
        "avg_abs_next_move_bps": avg("abs_next_move_bps"),
        "crossed_mid_rate": rate("crossed_mid"),
        "crossed_price_rate": rate("crossed_price"),
        "moved_away_rate": rate("moved_away"),
        "never_touched_rate": rate("never_touched"),
    }


def evaluate_window(rows: list[dict[str, Any]], window: int, mode: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        bid = f(row.get("best_bid"))
        ask = f(row.get("best_ask"))
        mid = f(row.get("close")) or f(row.get("mid"))
        spread_bps = f(row.get("spread_bps"))
        if bid is None or ask is None or mid is None or ask <= bid:
            continue
        if mode == "at_touch":
            price = bid
        else:
            inside = bid + TICK
            if inside >= ask:
                continue
            price = inside
        future = rows[i + 1 : i + 1 + window]
        if not future:
            continue
        crossed_mid = False
        crossed_price = False
        moved_away = True
        next_moves: list[float] = []
        for fut in future:
            fut_bid = f(fut.get("best_bid"))
            fut_ask = f(fut.get("best_ask"))
            fut_mid = f(fut.get("close")) or f(fut.get("mid"))
            if fut_mid is not None and mid > 0:
                next_moves.append(abs((fut_mid - mid) / mid) * 10_000)
            if fut_mid is not None and fut_mid <= price:
                crossed_mid = True
            if fut_ask is not None and fut_ask <= price:
                crossed_price = True
            if fut_bid is not None and fut_bid >= price:
                moved_away = False
        out.append(
            {
                "spread_bps": spread_bps,
                "abs_next_move_bps": round(statistics.fmean(next_moves), 2) if next_moves else None,
                "crossed_mid": crossed_mid,
                "crossed_price": crossed_price,
                "moved_away": moved_away,
                "never_touched": not crossed_mid and not crossed_price,
            }
        )
    return out


def main() -> None:
    args = parse_args()
    windows = [int(item) for item in args.windows.split(",") if item.strip()]
    csv_dir = Path(args.csv_dir)
    payload: dict[str, Any] = {"windows": windows, "markets": [], "summary": {}}
    aggregate: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for path in sorted(csv_dir.glob("*.csv")):
        rows = load_rows(path)
        if not rows:
            continue
        meta = rows[0]
        market_entry = {
            "csv": path.name,
            "question": meta.get("question"),
            "outcome": meta.get("outcome"),
            "token_id": meta.get("token_id"),
            "windows": {},
        }
        for window in windows:
            at_touch = evaluate_window(rows, window, mode="at_touch")
            inside = evaluate_window(rows, window, mode="inside")
            market_entry["windows"][str(window)] = {
                "at_touch": summarize(at_touch),
                "one_tick_inside": summarize(inside),
            }
            aggregate[f"{window}:at_touch"].extend(at_touch)
            aggregate[f"{window}:one_tick_inside"].extend(inside)
        payload["markets"].append(market_entry)

    payload["summary"] = {
        key: summarize(records)
        for key, records in aggregate.items()
    }
    Path(args.out).write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["summary"], indent=2))


if __name__ == "__main__":
    main()
