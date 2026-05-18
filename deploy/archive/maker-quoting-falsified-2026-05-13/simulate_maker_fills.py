"""Estimate realized net maker PnL per token on the collected CLOB JSONL corpus.

For each token (condition_id × outcome), this iterates through 30s polled
snapshots in chronological order and, at each snapshot, hypothesizes a resting
maker order:

  BUY  at price = best_bid + 1 tick   (one tick inside the spread)
  SELL at price = best_ask - 1 tick

It then walks forward `--fill-window-minutes` minutes looking for a fill:

  BUY filled  iff some future best_ask <= maker_buy_price   (a seller crossed our price)
  SELL filled iff some future best_bid >= maker_sell_price  (a buyer crossed our price)

When a fill occurs, the script looks `--adverse-window-minutes` further forward
and records the mid at that horizon. Maker PnL per share at that horizon, for a
BUY:

  pnl = (mid_at_adverse - maker_buy_price)
      = (mid_at_fill_minus_X) - (bid_at_entry + tick)

So the realized maker edge is (half-spread captured) minus (adverse drift).
Tokens where adverse drift > half-spread are net negative — the classic
maker-pickoff scenario.

Outputs `data/maker_fill_sim.json` with per-token aggregates and a global
ranking, plus a console-friendly summary of the top + bottom tokens.

This is a paper simulator: it does not model queue position, partial fills,
or cancel-replace cost. Treat results as an upper bound on achievable maker
edge, not a guarantee.
"""

from __future__ import annotations

import argparse
import json
import statistics
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TICK = 0.001  # Polymarket tick size: $0.001


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _load_records(path: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """Load JSONL into per-token sorted lists of {ts, bid, ask, mid, question, slug}."""
    by_token: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    rejected = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                rejected += 1
                continue
            cid = r.get("condition_id")
            out = r.get("outcome")
            bid_raw = r.get("best_bid")
            ask_raw = r.get("best_ask")
            ts_raw = r.get("timestamp")
            if not cid or not out or not ts_raw:
                rejected += 1
                continue
            if bid_raw is None or ask_raw is None:
                continue  # treat missing-quote as not-an-opportunity, not corruption
            try:
                bid, ask = float(bid_raw), float(ask_raw)
            except (TypeError, ValueError):
                rejected += 1
                continue
            if bid < 0 or ask > 1 or ask <= bid:
                continue
            ts_dt = _parse_ts(ts_raw)
            by_token[(cid, out)].append(
                {
                    "ts": ts_dt.timestamp(),
                    "bid": bid,
                    "ask": ask,
                    "mid": (bid + ask) / 2,
                    "question": r.get("question") or "",
                    "slug": r.get("market_slug") or "",
                }
            )
    print(f"loaded; rejected={rejected}; tokens={len(by_token)}; "
          f"records={sum(len(v) for v in by_token.values())}")
    for records in by_token.values():
        records.sort(key=lambda r: r["ts"])
    return by_token


def _next_index_at_or_after(ts_list: list[float], cutoff_ts: float, start_idx: int) -> int:
    """First index k >= start_idx with ts_list[k] >= cutoff_ts. Returns len if none."""
    return bisect_left(ts_list, cutoff_ts, lo=start_idx)


def _simulate_token(
    records: list[dict[str, Any]],
    fill_window_s: float,
    adverse_window_s: float,
) -> dict[str, Any]:
    n = len(records)
    if n < 5:
        return {
            "records": n,
            "skipped_reason": "fewer than 5 polls",
        }
    ts_list = [r["ts"] for r in records]

    buy_fills: list[dict[str, float]] = []
    sell_fills: list[dict[str, float]] = []
    spreads_bps: list[float] = []
    midpoints: list[float] = []

    for i in range(n):
        rec = records[i]
        bid, ask, mid = rec["bid"], rec["ask"], rec["mid"]
        if mid <= 0:
            continue
        spread = ask - bid
        if spread <= 0:
            continue
        spreads_bps.append(spread / mid * 10_000)
        midpoints.append(mid)
        maker_buy = bid + TICK
        maker_sell = ask - TICK
        if maker_buy >= ask:
            maker_buy = None
        if maker_sell <= bid:
            maker_sell = None
        if maker_buy is None and maker_sell is None:
            continue

        fill_cutoff = rec["ts"] + fill_window_s
        end_idx = _next_index_at_or_after(ts_list, fill_cutoff, i + 1)

        if maker_buy is not None:
            for j in range(i + 1, end_idx):
                if records[j]["ask"] <= maker_buy:
                    adverse_cutoff = records[j]["ts"] + adverse_window_s
                    k = _next_index_at_or_after(ts_list, adverse_cutoff, j)
                    if k >= n:
                        k = n - 1
                    if k <= j:
                        break
                    adverse_mid = records[k]["mid"]
                    pnl_dollars = adverse_mid - maker_buy
                    buy_fills.append(
                        {
                            "fill_idx": j,
                            "entry_mid": mid,
                            "maker_price": maker_buy,
                            "adverse_mid": adverse_mid,
                            "pnl_dollars": pnl_dollars,
                            "pnl_bps": pnl_dollars / maker_buy * 10_000,
                            "half_spread_captured_bps": (mid - maker_buy) / mid * 10_000,
                            "adverse_drift_bps": (adverse_mid - mid) / mid * 10_000,
                        }
                    )
                    break
        if maker_sell is not None:
            for j in range(i + 1, end_idx):
                if records[j]["bid"] >= maker_sell:
                    adverse_cutoff = records[j]["ts"] + adverse_window_s
                    k = _next_index_at_or_after(ts_list, adverse_cutoff, j)
                    if k >= n:
                        k = n - 1
                    if k <= j:
                        break
                    adverse_mid = records[k]["mid"]
                    pnl_dollars = maker_sell - adverse_mid
                    sell_fills.append(
                        {
                            "fill_idx": j,
                            "entry_mid": mid,
                            "maker_price": maker_sell,
                            "adverse_mid": adverse_mid,
                            "pnl_dollars": pnl_dollars,
                            "pnl_bps": pnl_dollars / maker_sell * 10_000,
                            "half_spread_captured_bps": (maker_sell - mid) / mid * 10_000,
                            "adverse_drift_bps": (mid - adverse_mid) / mid * 10_000,
                        }
                    )
                    break

    def _agg(fills: list[dict[str, float]]) -> dict[str, Any]:
        if not fills:
            return {
                "count": 0,
                "fill_rate": 0.0,
                "avg_pnl_bps": None,
                "median_pnl_bps": None,
                "avg_half_spread_captured_bps": None,
                "avg_adverse_drift_bps": None,
            }
        pnl_bps = [f["pnl_bps"] for f in fills]
        return {
            "count": len(fills),
            "fill_rate": round(len(fills) / max(1, n), 4),
            "avg_pnl_bps": round(statistics.fmean(pnl_bps), 2),
            "median_pnl_bps": round(statistics.median(pnl_bps), 2),
            "avg_pnl_dollars": round(statistics.fmean([f["pnl_dollars"] for f in fills]), 6),
            "avg_half_spread_captured_bps": round(
                statistics.fmean([f["half_spread_captured_bps"] for f in fills]), 2
            ),
            "avg_adverse_drift_bps": round(
                statistics.fmean([f["adverse_drift_bps"] for f in fills]), 2
            ),
        }

    return {
        "records": n,
        "avg_mid": round(statistics.fmean(midpoints), 5) if midpoints else None,
        "avg_spread_bps": round(statistics.fmean(spreads_bps), 2) if spreads_bps else None,
        "buy_maker": _agg(buy_fills),
        "sell_maker": _agg(sell_fills),
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, default=Path("data/quote_collection/run.jsonl"))
    p.add_argument(
        "--output", type=Path, default=Path("data/maker_fill_sim.json")
    )
    p.add_argument("--fill-window-minutes", type=float, default=30.0)
    p.add_argument("--adverse-window-minutes", type=float, default=15.0)
    p.add_argument(
        "--max-records-per-token",
        type=int,
        default=0,
        help="If >0, downsample each token's record list to this many samples (evenly spaced) before simulating.",
    )
    p.add_argument(
        "--top", type=int, default=15, help="How many tokens to show in console summary, each side."
    )
    args = p.parse_args()

    by_token = _load_records(args.input)
    fill_window_s = args.fill_window_minutes * 60
    adverse_window_s = args.adverse_window_minutes * 60

    per_token: dict[str, dict[str, Any]] = {}
    for (cid, outcome), recs in by_token.items():
        if args.max_records_per_token and len(recs) > args.max_records_per_token:
            step = len(recs) // args.max_records_per_token
            recs = recs[::step][: args.max_records_per_token]
        question = recs[0]["question"] if recs else ""
        slug = recs[0]["slug"] if recs else ""
        key = f"{cid}::{outcome}"
        per_token[key] = {
            "condition_id": cid,
            "outcome": outcome,
            "question": question,
            "slug": slug,
            **_simulate_token(recs, fill_window_s, adverse_window_s),
        }

    def _ranking_score(side: str, stats: dict[str, Any]) -> float:
        side_stats = stats.get(f"{side}_maker") or {}
        if not side_stats.get("count"):
            return float("-inf")
        return float(side_stats["fill_rate"]) * float(side_stats.get("avg_pnl_bps") or 0)

    buy_ranking = sorted(per_token.values(), key=lambda s: _ranking_score("buy", s), reverse=True)
    sell_ranking = sorted(per_token.values(), key=lambda s: _ranking_score("sell", s), reverse=True)

    payload = {
        "params": {
            "input": str(args.input),
            "fill_window_minutes": args.fill_window_minutes,
            "adverse_window_minutes": args.adverse_window_minutes,
            "max_records_per_token": args.max_records_per_token,
            "tick": TICK,
        },
        "n_tokens": len(per_token),
        "top_buy_maker": [s for s in buy_ranking[: args.top]],
        "top_sell_maker": [s for s in sell_ranking[: args.top]],
        "per_token": per_token,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    print("\n=== Top buy-maker tokens (fill_rate × avg_pnl_bps, desc) ===")
    for s in buy_ranking[: args.top]:
        bm = s.get("buy_maker") or {}
        if not bm.get("count"):
            continue
        score = _ranking_score("buy", s) * 100
        print(
            f"  {s['outcome']:>3} | mid≈{s.get('avg_mid'):.3f} | "
            f"fills={bm['count']:>4} rate={bm['fill_rate']:.2%} "
            f"pnl={bm['avg_pnl_bps']:>+7.1f}bps (hs={bm['avg_half_spread_captured_bps']:>+6.1f} adv={bm['avg_adverse_drift_bps']:>+7.1f}) "
            f"| score={score:>+7.1f} | {(s['question'] or s['slug'])[:60]}"
        )

    print("\n=== Top sell-maker tokens (fill_rate × avg_pnl_bps, desc) ===")
    for s in sell_ranking[: args.top]:
        sm = s.get("sell_maker") or {}
        if not sm.get("count"):
            continue
        score = _ranking_score("sell", s) * 100
        print(
            f"  {s['outcome']:>3} | mid≈{s.get('avg_mid'):.3f} | "
            f"fills={sm['count']:>4} rate={sm['fill_rate']:.2%} "
            f"pnl={sm['avg_pnl_bps']:>+7.1f}bps (hs={sm['avg_half_spread_captured_bps']:>+6.1f} adv={sm['avg_adverse_drift_bps']:>+7.1f}) "
            f"| score={score:>+7.1f} | {(s['question'] or s['slug'])[:60]}"
        )

    buy_positive = sum(
        1 for s in per_token.values() if (s.get("buy_maker") or {}).get("avg_pnl_bps") and s["buy_maker"]["avg_pnl_bps"] > 0
    )
    sell_positive = sum(
        1 for s in per_token.values() if (s.get("sell_maker") or {}).get("avg_pnl_bps") and s["sell_maker"]["avg_pnl_bps"] > 0
    )
    print(
        f"\nsummary: {buy_positive} of {len(per_token)} tokens have positive avg buy-maker PnL bps; "
        f"{sell_positive} have positive sell-maker PnL bps. Wrote {args.output}."
    )


if __name__ == "__main__":
    main()
