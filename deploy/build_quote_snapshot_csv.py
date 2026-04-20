from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.storage import save_rows_to_csv


def _parse_ts(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if payload.get("source") == "gamma_markets_poll_missing_target":
            continue
        rows.append(payload)
    return rows


def _rows_to_snapshot_csv_rows(rows: list[dict[str, Any]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        token_id = str(row.get("token_id") or "")
        if not token_id:
            continue
        grouped[token_id].append(row)

    output_rows: list[dict[str, object]] = []
    for token_id, token_rows in grouped.items():
        ordered = sorted(token_rows, key=lambda row: _parse_ts(str(row["timestamp"])))
        previous_mid: float | None = None
        for row in ordered:
            best_bid = _safe_float(row.get("best_bid"))
            best_ask = _safe_float(row.get("best_ask"))
            mid = _safe_float(row.get("mid"))
            last_price = _safe_float(row.get("last_price"))

            close = mid if mid is not None else last_price
            if close is None:
                continue

            open_price = previous_mid if previous_mid is not None else close
            high_price = max(value for value in [open_price, close, best_bid, best_ask] if value is not None)
            low_price = min(value for value in [open_price, close, best_bid, best_ask] if value is not None)

            output_rows.append(
                {
                    "timestamp": row["timestamp"],
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "close": close,
                    "volume": _safe_float(row.get("volume")) or 0.0,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "token_id": token_id,
                    "condition_id": row.get("condition_id"),
                    "outcome": row.get("outcome"),
                    "question": row.get("question"),
                    "market_slug": row.get("market_slug"),
                    "market_family": row.get("market_family"),
                    "spread": _safe_float(row.get("spread")),
                    "spread_bps": _safe_float(row.get("spread_bps")),
                    "last_price": last_price,
                    "liquidity": _safe_float(row.get("liquidity")),
                    "closed": row.get("closed"),
                    "archived": row.get("archived"),
                    "endDate": row.get("end_date"),
                    "createdAt": row.get("created_at"),
                }
            )
            previous_mid = close

    output_rows.sort(key=lambda row: (str(row.get("token_id") or ""), str(row.get("timestamp") or "")))
    return output_rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert quote_snapshots.jsonl into backtest-ready CSV snapshots grouped by token."
    )
    parser.add_argument("--input", default="data/quote_snapshots.jsonl", help="Input JSONL from deploy.collect_quotes")
    parser.add_argument(
        "--output-dir",
        default="data/quote_backtests",
        help="Directory to write one CSV per token into",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"input file not found: {input_path}")

    raw_rows = _load_rows(input_path)
    csv_rows = _rows_to_snapshot_csv_rows(raw_rows)
    if not csv_rows:
        raise SystemExit("no usable snapshot rows found in input")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    per_token: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in csv_rows:
        per_token[str(row["token_id"])].append(row)

    manifest: list[dict[str, object]] = []
    for token_id, rows_for_token in per_token.items():
        destination = output_dir / f"quote_backtest_{token_id}.csv"
        save_rows_to_csv(destination, rows_for_token)
        manifest.append(
            {
                "token_id": token_id,
                "path": str(destination),
                "rows": len(rows_for_token),
                "question": rows_for_token[0].get("question"),
                "condition_id": rows_for_token[0].get("condition_id"),
                "outcome": rows_for_token[0].get("outcome"),
            }
        )

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "tokens": len(manifest), "manifest": str(manifest_path)}, indent=2))


if __name__ == "__main__":
    main()
