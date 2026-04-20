from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path("data/paper_trades.jsonl")


def _market_name(row: dict) -> str:
    return str(row.get("question") or row.get("token_id") or "unknown")


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize paper-trading logs.")
    parser.add_argument("--tail", type=int, help="Only summarize the most recent N log entries.")
    parser.add_argument("--since", help="Only summarize entries on/after this ISO timestamp or date (e.g. 2026-03-28 or 2026-03-28T13:00:00+08:00).")
    args = parser.parse_args()

    if not LOG_PATH.exists():
        print(json.dumps({"status": "empty", "message": "No paper log found yet.", "path": str(LOG_PATH)}, indent=2))
        return

    rows = [json.loads(line) for line in LOG_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]

    since_dt = None
    if args.since:
        since_value = args.since if "T" in args.since else f"{args.since}T00:00:00+00:00"
        since_dt = _parse_iso_timestamp(since_value)
        if since_dt is None:
            raise SystemExit(f"Invalid --since value: {args.since}")
        rows = [row for row in rows if (_parse_iso_timestamp(row.get("timestamp")) or datetime.min.replace(tzinfo=timezone.utc)) >= since_dt]

    if args.tail is not None:
        rows = rows[-args.tail :]

    if not rows:
        print(
            json.dumps(
                {
                    "status": "empty",
                    "message": "No log entries matched the requested filters.",
                    "path": str(LOG_PATH),
                    "tail": args.tail,
                    "since": args.since,
                },
                indent=2,
            )
        )
        return

    status_counts = Counter(row.get("status", "unknown") for row in rows)
    reason_counts = Counter(row.get("reason", "") for row in rows)
    market_counts = Counter(_market_name(row) for row in rows)

    dry_runs = [row for row in rows if row.get("status") == "dry_run"]
    buy_dry_runs = [row for row in dry_runs if ((row.get("intent") or {}).get("side") == "BUY")]
    sell_dry_runs = [row for row in dry_runs if ((row.get("intent") or {}).get("side") == "SELL")]

    buy_markets = Counter(_market_name(row) for row in buy_dry_runs)
    buy_prices = [float((row.get("intent") or {}).get("price")) for row in buy_dry_runs if (row.get("intent") or {}).get("price") is not None]

    strategy_signal_counter = Counter()
    strategy_reason_counter = Counter()
    for row in rows:
        summary = row.get("signal_summary") or {}
        for signal in summary.get("signals", []):
            strategy = signal.get("strategy", "unknown")
            side = signal.get("side", "UNKNOWN")
            strategy_signal_counter[f"{strategy}:{side}"] += 1
            reason = signal.get("reason")
            if reason:
                strategy_reason_counter[f"{strategy}: {reason}"] += 1

    latest_buy = None
    if buy_dry_runs:
        row = buy_dry_runs[-1]
        latest_buy = {
            "timestamp": row.get("timestamp"),
            "market": _market_name(row),
            "token_id": row.get("token_id"),
            "price": (row.get("intent") or {}).get("price"),
            "mid_price": row.get("mid_price"),
        }

    payload = {
        "entries": len(rows),
        "filters": {"tail": args.tail, "since": args.since},
        "status_counts": dict(status_counts),
        "top_reasons": reason_counts.most_common(10),
        "top_markets": market_counts.most_common(10),
        "buy_opportunities": {
            "approved_count": len(buy_dry_runs),
            "sell_count": len(sell_dry_runs),
            "top_buy_markets": buy_markets.most_common(10),
            "latest_buy": latest_buy,
            "average_buy_price": (sum(buy_prices) / len(buy_prices)) if buy_prices else None,
        },
        "strategy_activity": {
            "signal_counts": strategy_signal_counter.most_common(20),
            "top_reasons": strategy_reason_counter.most_common(15),
        },
        "log_path": str(LOG_PATH),
    }

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
