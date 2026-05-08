"""Health check for the quote collector LaunchAgent.

Track A's verdict on 2026-05-02 depends on having continuous coverage of all
72 watchlist conditions. If the collector silently dropped any of them
mid-window, we'd discover the gap on decision day — by which point the data
can't be backfilled.

This script reads `data/scan_shortlist.json` (the watchlist) and
`data/quote_collection/run.jsonl` (the collector output), then reports:

  - rows in the last hour (collector liveness)
  - last-write age (does the file look frozen?)
  - per-condition row count over a window (default last 24h)
  - any watchlist condition with **0 rows** in the window (silent drops)

Exit code:
  0 = healthy
  1 = collector looks dead (no rows in last hour, or file age > 5 min)
  2 = at least one watchlist condition has 0 rows in window

Run interactively:
    python -m deploy.collector_health
    python -m deploy.collector_health --window-hours 6
    python -m deploy.collector_health --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHORTLIST_PATH = ROOT / "data" / "scan_shortlist.json"
RUN_JSONL = ROOT / "data" / "quote_collection" / "run.jsonl"
COLLECTOR_OUT = ROOT / "data" / "quote_collection" / "collector.out.log"

# Optional second stream (non-sports corpus collected for Phase 2 honest-test
# re-run). Falls through silently if disabled / file missing.
NONSPORTS_SHORTLIST_PATH = ROOT / "data" / "scan_shortlist_nonsports.json"
NONSPORTS_RUN_JSONL = ROOT / "data" / "quote_collection" / "nonsports_run.jsonl"


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def load_watchlist(path: Path | None = None) -> list[dict[str, Any]]:
    target = path or SHORTLIST_PATH
    if not target.exists():
        return []
    data = json.loads(target.read_text())
    return data.get("shortlist", [])


def scan_run_jsonl(window_hours: float, dead_threshold_minutes: float, run_jsonl: Path | None = None) -> dict[str, Any]:
    target = run_jsonl or RUN_JSONL
    if not target.exists():
        return {"error": f"missing {target}", "rows_in_window": 0}

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    last_hour_cutoff = now - timedelta(hours=1)
    five_min_cutoff = now - timedelta(minutes=dead_threshold_minutes)

    by_condition: Counter[str] = Counter()
    by_token: Counter[str] = Counter()
    rows_in_window = 0
    rows_last_hour = 0
    last_ts: datetime | None = None
    total_rows = 0

    with target.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            total_rows += 1
            ts = _parse_iso(row.get("timestamp", ""))
            if ts is None:
                continue
            if last_ts is None or ts > last_ts:
                last_ts = ts
            if ts >= cutoff:
                rows_in_window += 1
                cid = row.get("condition_id")
                tid = row.get("token_id")
                if cid:
                    by_condition[cid] += 1
                if tid:
                    by_token[tid] += 1
            if ts >= last_hour_cutoff:
                rows_last_hour += 1

    file_age_seconds: float | None = None
    if last_ts is not None:
        file_age_seconds = (now - last_ts).total_seconds()

    is_dead = (
        rows_last_hour == 0
        or last_ts is None
        or last_ts < five_min_cutoff
    )

    return {
        "total_rows": total_rows,
        "rows_in_window": rows_in_window,
        "rows_last_hour": rows_last_hour,
        "last_row_ts": last_ts.isoformat() if last_ts else None,
        "last_row_age_seconds": file_age_seconds,
        "is_dead": is_dead,
        "by_condition_in_window": dict(by_condition),
        "by_token_in_window": dict(by_token),
    }


def evaluate(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    shortlist_path = getattr(args, "shortlist_path", None) or SHORTLIST_PATH
    run_jsonl_path = getattr(args, "run_jsonl_path", None) or RUN_JSONL
    watchlist = load_watchlist(shortlist_path)
    expected_conditions = {e["condition_id"] for e in watchlist if e.get("condition_id")}
    expected_questions = {e["condition_id"]: e.get("question") for e in watchlist}

    scan = scan_run_jsonl(args.window_hours, args.dead_threshold_minutes, run_jsonl_path)
    by_cond = scan.get("by_condition_in_window", {})
    seen = set(by_cond.keys())
    silent_drops = sorted(expected_conditions - seen)

    # Conditions present but with very low row counts (potential degraded source)
    median_rows = sorted(by_cond.values())[len(by_cond) // 2] if by_cond else 0
    low_threshold = max(1, int(median_rows * 0.25)) if median_rows else 0
    degraded = sorted(
        [(cid, n) for cid, n in by_cond.items() if 0 < n < low_threshold and cid in expected_conditions],
        key=lambda x: x[1],
    )

    health: dict[str, Any] = {
        "window_hours": args.window_hours,
        "watchlist_size": len(expected_conditions),
        "watchlist_seen_in_window": len(seen & expected_conditions),
        "silent_drops_count": len(silent_drops),
        "silent_drops": [{"condition_id": cid, "question": expected_questions.get(cid)} for cid in silent_drops],
        "median_rows_per_condition": median_rows,
        "degraded_conditions": [
            {"condition_id": cid, "rows": n, "question": expected_questions.get(cid)}
            for cid, n in degraded
        ],
        "collector": {
            "total_rows": scan.get("total_rows"),
            "rows_in_window": scan.get("rows_in_window"),
            "rows_last_hour": scan.get("rows_last_hour"),
            "last_row_ts": scan.get("last_row_ts"),
            "last_row_age_seconds": scan.get("last_row_age_seconds"),
            "is_dead": scan.get("is_dead"),
        },
    }

    if scan.get("is_dead"):
        return 1, health
    if silent_drops:
        return 2, health
    return 0, health


def main() -> None:
    parser = argparse.ArgumentParser(description="Quote collector health check")
    parser.add_argument("--window-hours", type=float, default=24.0)
    parser.add_argument("--dead-threshold-minutes", type=float, default=5.0,
                        help="If last row older than this, collector is considered dead.")
    parser.add_argument("--shortlist-path", type=Path, default=None, help="Override watchlist file path.")
    parser.add_argument("--run-jsonl-path", type=Path, default=None, help="Override run.jsonl path.")
    parser.add_argument("--stream", choices=["main", "nonsports"], default=None,
                        help="Convenience: target one of the known streams instead of providing paths manually.")
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    parser.add_argument("--out", type=Path, default=None, help="Optional file to write JSON snapshot.")
    args = parser.parse_args()

    if args.stream == "nonsports":
        args.shortlist_path = args.shortlist_path or NONSPORTS_SHORTLIST_PATH
        args.run_jsonl_path = args.run_jsonl_path or NONSPORTS_RUN_JSONL

    code, health = evaluate(args)
    payload = {"status_code": code, "checked_at": datetime.now(timezone.utc).isoformat(), **health}

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, default=str))

    if args.json:
        print(json.dumps(payload, indent=2, default=str))
        sys.exit(code)

    c = health["collector"]
    print(f"=== Quote collector health (window {args.window_hours}h) ===")
    print(f"  Total rows in run.jsonl   : {c['total_rows']:,}")
    print(f"  Rows in window            : {c['rows_in_window']:,}")
    print(f"  Rows in last hour         : {c['rows_last_hour']:,}")
    print(f"  Last row at               : {c['last_row_ts']}")
    print(f"  Last row age (s)          : {c['last_row_age_seconds']:.0f}" if c.get("last_row_age_seconds") is not None else "  Last row age (s)          : —")
    print(f"  Watchlist size            : {health['watchlist_size']}")
    print(f"  Watchlist seen in window  : {health['watchlist_seen_in_window']}")
    print(f"  Median rows/condition     : {health['median_rows_per_condition']:,}")
    print()

    if health["silent_drops_count"]:
        print(f"⚠ SILENT DROPS ({health['silent_drops_count']}): conditions in watchlist but 0 rows in window")
        for d in health["silent_drops"][:10]:
            print(f"    - {d['condition_id']}  {d['question']}")
        if health["silent_drops_count"] > 10:
            print(f"    ... and {health['silent_drops_count'] - 10} more")
        print()

    if health["degraded_conditions"]:
        print(f"⚠ DEGRADED ({len(health['degraded_conditions'])}): row count < 25% of median")
        for d in health["degraded_conditions"][:10]:
            print(f"    - {d['condition_id']}  rows={d['rows']:,}  {d['question']}")
        print()

    if code == 0:
        print("✓ Healthy.")
    elif code == 1:
        print("✗ COLLECTOR APPEARS DEAD. Check launchd:")
        print("  launchctl list | grep quote-collector")
        print(f"  tail {COLLECTOR_OUT}")
    elif code == 2:
        print(f"✗ {health['silent_drops_count']} watchlist conditions silently dropped. Investigate before 2026-05-02 decision.")

    sys.exit(code)


if __name__ == "__main__":
    main()
