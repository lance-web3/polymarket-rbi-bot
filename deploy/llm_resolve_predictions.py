"""Stage B verdict — check which paper-traded predictions have resolved on Polymarket.

For each row in `data/llm_predictions.jsonl`, look up the current Gamma state of
that condition_id. If the market is closed and resolved (outcomePrices ∈ [0,1]),
compute:

  realized Brier(LLM)   = (p_llm - y)^2
  realized Brier(market) = (mid_at_prediction - y)^2
  paper PnL @ $5 size, treating each prediction's side (BUY/SELL) as a trade
    executed at the mid recorded at prediction time.

Outputs a console table + writes `data/llm_resolution_results.json`. Mechanical
B→C gate: GO if ≥3 resolved trades AND Brier(LLM) ≤ Brier(market) AND realized
paper PnL > 0.

Usage:
    python -m deploy.llm_resolve_predictions
    python -m deploy.llm_resolve_predictions --event-slug pga-championship-winner-2026
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.market_discovery import parse_jsonish_list
from polymarket_rbi_bot.calibration import brier_score, reference_brier_baselines
from polymarket_rbi_bot.config import BotConfig

logger = logging.getLogger(__name__)


def fetch_market_by_condition_id(config: BotConfig, condition_id: str, *, timeout: int = 30) -> dict[str, Any] | None:
    """Look up current Gamma state for a condition_id (open or closed) via the
    `condition_ids=<cid>` filter. Gamma's filter defaults to closed=false, so we
    explicitly try both flags."""
    for closed_flag in ("true", "false"):
        try:
            r = requests.get(
                f"{config.gamma_host}/markets",
                params={"condition_ids": condition_id, "closed": closed_flag, "limit": 5},
                timeout=timeout,
            )
        except requests.RequestException as e:
            logger.warning("gamma fetch failed for %s (closed=%s): %s", condition_id, closed_flag, e)
            continue
        if r.status_code != 200:
            continue
        data = r.json() if r.text else []
        for m in data:
            cid = str(m.get("conditionId") or "").strip()
            if cid == condition_id:
                return m
    return None


def extract_resolved_outcome(market: dict[str, Any]) -> int | None:
    outcomes = parse_jsonish_list(market.get("outcomes"))
    outcome_prices = parse_jsonish_list(market.get("outcomePrices"))
    if len(outcomes) < 2 or len(outcome_prices) < 2:
        return None
    yes_idx = next((i for i, o in enumerate(outcomes) if str(o).strip().lower() == "yes"), None)
    if yes_idx is None:
        return None
    try:
        yes_final = float(outcome_prices[yes_idx])
    except (TypeError, ValueError):
        return None
    if yes_final >= 0.99:
        return 1
    if yes_final <= 0.01:
        return 0
    return None


def load_predictions(path: Path) -> dict[str, dict[str, Any]]:
    """Most-recent prediction per condition_id."""
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = row.get("condition_id")
            if not cid:
                continue
            ts = row.get("ts") or ""
            cur = out.get(cid)
            if cur is None or ts > cur.get("ts", ""):
                out[cid] = row
    return out


def paper_pnl_per_share(side: str, p_llm: float, mid: float, outcome: int) -> float:
    """Naive paper PnL assuming we 'traded' at the mid, $1 per share.

    BUY YES at mid → payoff = 1 if outcome=1 else 0
    SELL YES at mid → mirror (equivalent to buying NO at 1-mid)
    """
    if side == "BUY":
        return float(outcome) - float(mid)
    if side == "SELL":
        return float(mid) - float(outcome)
    return 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=str, default=None)
    parser.add_argument("--event-slug", type=str, default=None,
                        help="Filter predictions by event_slug")
    parser.add_argument("--output", type=str, default="data/llm_resolution_results.json")
    parser.add_argument("--paper-trade-size", type=float, default=5.0)
    parser.add_argument("--min-edge-bps", type=float, default=300.0,
                        help="Only score predictions whose original edge_bps exceeded this magnitude.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config = BotConfig.from_env()
    preds_path = Path(args.predictions or config.llm_predictions_path)
    preds = load_predictions(preds_path)
    if args.event_slug:
        preds = {k: v for k, v in preds.items() if v.get("event_slug") == args.event_slug}
    logger.info("Loaded %d unique predictions", len(preds))

    results: list[dict[str, Any]] = []
    unresolved = 0
    not_found = 0
    for cid, pred in preds.items():
        market = fetch_market_by_condition_id(config, cid)
        if not market:
            not_found += 1
            continue
        outcome = extract_resolved_outcome(market)
        if outcome is None:
            unresolved += 1
            continue
        mid = pred.get("mid")
        p_llm = pred.get("p_llm")
        side = pred.get("side", "HOLD")
        edge_bps = pred.get("edge_bps") or 0.0
        if mid is None or p_llm is None:
            continue
        # Paper-trade at mid_at_prediction
        per_share = paper_pnl_per_share(side, p_llm, mid, outcome)
        pnl_usd = per_share * args.paper_trade_size if abs(edge_bps) >= args.min_edge_bps else 0.0
        results.append(
            {
                "condition_id": cid,
                "question": pred.get("question"),
                "event_slug": pred.get("event_slug"),
                "side": side,
                "edge_bps": edge_bps,
                "p_llm": p_llm,
                "mid_at_prediction": mid,
                "outcome": outcome,
                "brier_llm": (p_llm - outcome) ** 2,
                "brier_market": (mid - outcome) ** 2,
                "paper_pnl_per_share": per_share,
                "paper_pnl_usd_at_size": round(pnl_usd, 3),
                "would_trade": abs(edge_bps) >= args.min_edge_bps,
            }
        )

    logger.info("resolved=%d unresolved=%d not_found=%d", len(results), unresolved, not_found)

    # Aggregate
    if not results:
        print(f"\nNo resolved predictions yet. unresolved={unresolved} not_found={not_found}")
        return

    tradeables = [r for r in results if r["would_trade"]]
    overall_brier_llm = brier_score([r["p_llm"] for r in results], [r["outcome"] for r in results])
    overall_brier_market = brier_score([r["mid_at_prediction"] for r in results], [r["outcome"] for r in results])
    refs = reference_brier_baselines([r["outcome"] for r in results])

    total_pnl = sum(r["paper_pnl_usd_at_size"] for r in tradeables)
    win_count = sum(1 for r in tradeables if r["paper_pnl_usd_at_size"] > 0)

    # B->C gate
    gate_min_n = 3
    gate_pass = (
        len(tradeables) >= gate_min_n
        and overall_brier_llm <= overall_brier_market
        and total_pnl > 0
    )
    verdict = "GO" if gate_pass else "NO_GO"

    summary = {
        "n_resolved": len(results),
        "n_tradeable_above_edge": len(tradeables),
        "min_edge_bps": args.min_edge_bps,
        "paper_trade_size_usd": args.paper_trade_size,
        "overall_brier_llm": round(overall_brier_llm, 4),
        "overall_brier_market": round(overall_brier_market, 4),
        "overall_brier_base_rate": round(refs["always_base_rate"], 4),
        "base_rate_yes": round(refs["base_rate"], 4),
        "paper_pnl_total_usd": round(total_pnl, 3),
        "tradeable_winners": win_count,
        "tradeable_losers": len(tradeables) - win_count,
        "gate_min_n": gate_min_n,
        "verdict": verdict,
        "rows": results,
    }

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(summary, indent=2, default=str) + "\n")

    print(f"\n=== Stage B paper-trade results (size=${args.paper_trade_size:.2f}, min_edge={args.min_edge_bps}bps) ===")
    print(f"  resolved={len(results)}  tradeable={len(tradeables)}  not_found={not_found}  unresolved={unresolved}")
    print(f"  Brier(LLM)    = {overall_brier_llm:.4f}")
    print(f"  Brier(market) = {overall_brier_market:.4f}")
    print(f"  Brier(base)   = {refs['always_base_rate']:.4f}  (base rate YES = {refs['base_rate']:.3f})")
    print(f"  Paper PnL total = ${total_pnl:+.3f}  ({win_count}W / {len(tradeables) - win_count}L)")
    print(f"  Verdict: {verdict}")
    print(f"\nTop 10 tradeable bets by paper PnL:")
    for r in sorted(tradeables, key=lambda r: r["paper_pnl_usd_at_size"], reverse=True)[:10]:
        print(
            f"  {r['side']:<4} pnl=${r['paper_pnl_usd_at_size']:+.3f} y={r['outcome']} "
            f"p_llm={r['p_llm']:.3f} mid={r['mid_at_prediction']:.3f} edge={r['edge_bps']:+7.0f}bps "
            f"| {(r.get('question') or '')[:70]}"
        )
    print(f"\nBottom 10 (biggest losers):")
    for r in sorted(tradeables, key=lambda r: r["paper_pnl_usd_at_size"])[:10]:
        if r["paper_pnl_usd_at_size"] >= 0:
            break
        print(
            f"  {r['side']:<4} pnl=${r['paper_pnl_usd_at_size']:+.3f} y={r['outcome']} "
            f"p_llm={r['p_llm']:.3f} mid={r['mid_at_prediction']:.3f} edge={r['edge_bps']:+7.0f}bps "
            f"| {(r.get('question') or '')[:70]}"
        )


if __name__ == "__main__":
    main()
