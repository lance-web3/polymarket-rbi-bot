from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.storage import save_rows_to_csv
from polymarket_rbi_bot.data import load_snapshots_from_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export per-bar activity/state features from quote-backtest CSVs.")
    parser.add_argument("--csv", action="append", default=[], help="CSV path, directory, or glob. Repeatable.")
    parser.add_argument("--out", default="data/state_features.csv", help="Output CSV path.")
    parser.add_argument("--lookback-bars", type=int, default=5, help="Lookback window for quote-change and realized-move features.")
    parser.add_argument("--flat-move-bps", type=float, default=5.0, help="Absolute move threshold for flat/moving/jumping bucketing.")
    parser.add_argument("--jump-move-bps", type=float, default=25.0, help="Absolute move threshold above which a bar is considered jumping.")
    parser.add_argument("--tight-spread-bps", type=float, default=100.0, help="Spread threshold for tight/medium/wide bucketing.")
    parser.add_argument("--wide-spread-bps", type=float, default=300.0, help="Spread threshold above which a bar is considered wide.")
    parser.add_argument("--low-quote-change-ratio", type=float, default=0.2, help="Quote-change ratio threshold for low activity.")
    parser.add_argument("--high-quote-change-ratio", type=float, default=0.6, help="Quote-change ratio threshold for high activity.")
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


def _f(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _spread_bps(best_bid: float | None, best_ask: float | None, close: float | None) -> float | None:
    if best_bid is None or best_ask is None:
        return None
    mid = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else close
    if mid is None or mid <= 0 or best_ask < best_bid:
        return None
    return ((best_ask - best_bid) / mid) * 10_000


def _safe_ratio(num: float, den: float) -> float | None:
    if den == 0:
        return None
    return num / den


def _movement_bucket(abs_move_bps: float | None, flat_move_bps: float, jump_move_bps: float) -> str:
    if abs_move_bps is None:
        return "unknown"
    if abs_move_bps < flat_move_bps:
        return "flat"
    if abs_move_bps < jump_move_bps:
        return "moving"
    return "jumping"


def _spread_bucket(spread_bps: float | None, tight_spread_bps: float, wide_spread_bps: float) -> str:
    if spread_bps is None:
        return "unknown"
    if spread_bps < tight_spread_bps:
        return "tight"
    if spread_bps < wide_spread_bps:
        return "medium"
    return "wide"


def _activity_bucket(change_ratio: float | None, low_threshold: float, high_threshold: float) -> str:
    if change_ratio is None:
        return "unknown"
    if change_ratio < low_threshold:
        return "low"
    if change_ratio < high_threshold:
        return "medium"
    return "high"


def _price_bucket(price: float | None) -> str:
    if price is None:
        return "unknown"
    if price < 0.1:
        return "0-10c"
    if price < 0.3:
        return "10-30c"
    if price < 0.7:
        return "30-70c"
    if price < 0.9:
        return "70-90c"
    return "90-100c"


def _hours_to_resolution(snapshot) -> float | None:
    for key in ("endDate", "end_date", "resolution_ts", "end_ts", "end_time"):
        raw = snapshot.metadata.get(key)
        if not raw:
            continue
        try:
            from datetime import datetime, timezone
            text = str(raw)
            if text.endswith("Z"):
                text = text.replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(text)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                from datetime import datetime, timezone
                if str(raw).isdigit():
                    end_dt = datetime.fromtimestamp(int(str(raw)), tz=timezone.utc)
                else:
                    continue
            except Exception:
                continue
        delta = end_dt - snapshot.candle.timestamp
        return delta.total_seconds() / 3600
    return None


def _resolution_bucket(hours: float | None) -> str:
    if hours is None:
        return "unknown"
    if hours < 2:
        return "lt_2h"
    if hours < 12:
        return "2-12h"
    if hours < 48:
        return "12-48h"
    if hours < 168:
        return "2-7d"
    return "gt_7d"


def build_rows_for_csv(path: Path, args: argparse.Namespace) -> list[dict[str, object]]:
    snapshots = load_snapshots_from_csv(path)
    rows: list[dict[str, object]] = []
    lookback = max(args.lookback_bars, 1)

    prev_bid = None
    prev_ask = None
    prev_close = None
    recent_change_flags: list[int] = []

    for index, snapshot in enumerate(snapshots):
        best_bid = snapshot.best_bid
        best_ask = snapshot.best_ask
        close = snapshot.candle.close
        spread_bps = _spread_bps(best_bid, best_ask, close)

        abs_move_bps = None
        if prev_close is not None and prev_close > 0:
            abs_move_bps = abs((close - prev_close) / prev_close) * 10_000

        quote_changed = int(
            (prev_bid is not None and best_bid is not None and not math.isclose(prev_bid, best_bid))
            or (prev_ask is not None and best_ask is not None and not math.isclose(prev_ask, best_ask))
        ) if index > 0 else 0
        recent_change_flags.append(quote_changed)
        if len(recent_change_flags) > lookback:
            recent_change_flags = recent_change_flags[-lookback:]

        quote_change_count = sum(recent_change_flags)
        quote_change_ratio = _safe_ratio(quote_change_count, len(recent_change_flags))

        start = max(0, index - lookback + 1)
        realized_moves = []
        for earlier in snapshots[start:index + 1]:
            if earlier.candle.close > 0:
                realized_moves.append(earlier.candle.close)
        lookback_move_bps = None
        if len(realized_moves) >= 2 and realized_moves[0] > 0:
            lookback_move_bps = abs((realized_moves[-1] - realized_moves[0]) / realized_moves[0]) * 10_000

        hours_to_resolution = _hours_to_resolution(snapshot)
        state_label = "/".join(
            [
                _spread_bucket(spread_bps, args.tight_spread_bps, args.wide_spread_bps),
                _movement_bucket(abs_move_bps, args.flat_move_bps, args.jump_move_bps),
                _activity_bucket(quote_change_ratio, args.low_quote_change_ratio, args.high_quote_change_ratio),
            ]
        )

        rows.append(
            {
                "source_csv": str(path),
                "timestamp": snapshot.candle.timestamp.isoformat(),
                "token_id": snapshot.metadata.get("token_id"),
                "condition_id": snapshot.metadata.get("condition_id"),
                "question": snapshot.metadata.get("question"),
                "outcome": snapshot.metadata.get("outcome"),
                "close": close,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
                "abs_move_bps": round(abs_move_bps, 2) if abs_move_bps is not None else None,
                "lookback_move_bps": round(lookback_move_bps, 2) if lookback_move_bps is not None else None,
                "quote_changed": quote_changed,
                "quote_change_count": quote_change_count,
                "quote_change_ratio": round(quote_change_ratio, 4) if quote_change_ratio is not None else None,
                "quote_source": snapshot.metadata.get("quote_source"),
                "price_bucket": _price_bucket(close),
                "spread_bucket": _spread_bucket(spread_bps, args.tight_spread_bps, args.wide_spread_bps),
                "movement_bucket": _movement_bucket(abs_move_bps, args.flat_move_bps, args.jump_move_bps),
                "activity_bucket": _activity_bucket(quote_change_ratio, args.low_quote_change_ratio, args.high_quote_change_ratio),
                "hours_to_resolution": round(hours_to_resolution, 2) if hours_to_resolution is not None else None,
                "resolution_bucket": _resolution_bucket(hours_to_resolution),
                "state_label": state_label,
            }
        )

        prev_bid = best_bid
        prev_ask = best_ask
        prev_close = close

    return rows


def main() -> None:
    args = parse_args()
    csv_paths = expand_csv_inputs(args.csv)
    if not csv_paths:
        raise SystemExit("No CSV files found. Pass --csv path/to/file.csv, a directory, or a glob.")

    rows: list[dict[str, object]] = []
    for path in csv_paths:
        rows.extend(build_rows_for_csv(path, args))

    if not rows:
        raise SystemExit("No feature rows generated.")

    out_path = Path(args.out)
    save_rows_to_csv(out_path, rows)
    summary = {
        "saved": str(out_path),
        "csv_count": len(csv_paths),
        "row_count": len(rows),
        "state_labels": len({str(row.get('state_label')) for row in rows}),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
