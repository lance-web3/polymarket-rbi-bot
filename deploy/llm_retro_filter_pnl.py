"""Retro-apply the long-shot SELL filter to Stage A's 240 resolved markets and
compute realistic paper PnL.

The refined hypothesis from today's PGA + Billboard data: Claude's edge is
specifically selling overpriced LONG-SHOTS — markets where retail bids up
speculative tail buckets that have ≤15% probability and Claude correctly
prices them lower. The reverse cases (Claude SELL on a near-certainty, or
Claude BUY on a long-shot) fail because they require current information
Claude doesn't have.

This script applies several filter variants and reports paper PnL using
the correct buy-NO mechanics (no shorting on Polymarket):

  trade: BUY NO at price (1 - market_p_t7)
  capital deployed: $5 per trade
  shares purchased: $5 / (1 - market_p_t7)
  payoff if y=0 (YES did NOT happen): shares * $1
  payoff if y=1 (YES happened): $0

Outputs a grid of filter variants + headline numbers for the
"mid < 0.15 AND p_llm < mid - 0.02" filter.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.calibration import brier_score
from polymarket_rbi_bot.config import BotConfig

TRADE_CAPITAL_USD = 5.0


def buy_no_pnl(market_p_t7: float, outcome: int, capital: float) -> tuple[float, float]:
    """Return (profit_usd, payoff_ratio). Buy NO at (1 - market_p_t7) with $capital.
    If outcome=0 (YES NOT happen), NO wins $1/share. If outcome=1, NO pays 0."""
    no_price = 1.0 - market_p_t7
    if no_price <= 0:
        return 0.0, 0.0
    shares = capital / no_price
    payoff = shares * (1.0 if outcome == 0 else 0.0)
    profit = payoff - capital
    return round(profit, 4), round(profit / capital, 4)


def apply_filter(
    rows: list[dict[str, Any]],
    *,
    side: str = "SELL",
    max_mid: float | None = None,
    min_mid: float | None = None,
    min_edge_delta: float | None = None,  # p_llm < market_p - this AND model says lower (SELL)
    max_p_llm: float | None = None,
    min_confidence: float | None = None,
) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        if r.get("status") != "ok":
            continue
        p_llm = r.get("p_llm")
        mid = r.get("market_p_t7")
        conf = r.get("confidence_llm")
        if p_llm is None or mid is None or conf is None:
            continue
        # Side check
        if side == "SELL":
            if p_llm >= mid:
                continue
        elif side == "BUY":
            if p_llm <= mid:
                continue
        # Mid window
        if max_mid is not None and mid > max_mid:
            continue
        if min_mid is not None and mid < min_mid:
            continue
        # Edge magnitude
        if min_edge_delta is not None and abs(p_llm - mid) < min_edge_delta:
            continue
        if max_p_llm is not None and p_llm > max_p_llm:
            continue
        if min_confidence is not None and conf < min_confidence:
            continue
        out.append(r)
    return out


def score_filter(rows: list[dict[str, Any]], *, capital: float = TRADE_CAPITAL_USD) -> dict[str, Any]:
    if not rows:
        return {
            "n": 0, "wins": 0, "losses": 0,
            "win_rate": None, "total_pnl_usd": 0.0,
            "avg_pnl_per_trade_usd": None, "median_pnl_usd": None,
            "max_loss_usd": None, "max_win_usd": None,
            "brier_llm": None, "brier_market": None,
            "brier_delta_llm_minus_market": None,
            "capital_at_risk_usd": 0.0,
        }
    pnls = []
    wins = 0
    losses = 0
    p_llms = []
    p_mkts = []
    outcomes = []
    for r in rows:
        outcome = int(r["resolved_outcome"])
        mid = float(r["market_p_t7"])
        profit, _ = buy_no_pnl(mid, outcome, capital)
        pnls.append(profit)
        if profit > 0:
            wins += 1
        elif profit < 0:
            losses += 1
        p_llms.append(float(r["p_llm"]))
        p_mkts.append(mid)
        outcomes.append(outcome)
    total_pnl = sum(pnls)
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(rows), 3),
        "total_pnl_usd": round(total_pnl, 3),
        "avg_pnl_per_trade_usd": round(total_pnl / len(rows), 4),
        "median_pnl_usd": round(statistics.median(pnls), 4),
        "max_loss_usd": round(min(pnls), 3),
        "max_win_usd": round(max(pnls), 3),
        "brier_llm": round(brier_score(p_llms, outcomes), 4),
        "brier_market": round(brier_score(p_mkts, outcomes), 4),
        "brier_delta_llm_minus_market": round(brier_score(p_llms, outcomes) - brier_score(p_mkts, outcomes), 4),
        "capital_at_risk_usd": round(capital * len(rows), 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=str, default=None)
    parser.add_argument("--output", type=str, default="data/llm_retro_filter_pnl.json")
    parser.add_argument("--capital", type=float, default=TRADE_CAPITAL_USD)
    args = parser.parse_args()

    config = BotConfig.from_env()
    path = Path(args.input or config.llm_calibration_output_path)
    rows: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(row)
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    print(f"Loaded {len(rows)} rows, {len(ok_rows)} resolved with status=ok\n")

    # --- Baseline: no filter, score by Stage A's heuristic (any SELL signal)
    baseline_sell = apply_filter(ok_rows, side="SELL")
    baseline_buy = apply_filter(ok_rows, side="BUY")
    print(f"=== Baseline (no filter beyond side direction) ===")
    print(f"  All SELL (model < market): n={len(baseline_sell)}")
    print(f"    {score_filter(baseline_sell, capital=args.capital)}")
    print(f"  All BUY  (model > market): n={len(baseline_buy)}")
    # For BUY we use buy-YES mechanics, not buy-NO; compute separately
    buy_pnls = []
    for r in baseline_buy:
        outcome = int(r["resolved_outcome"])
        mid = float(r["market_p_t7"])
        yes_price = mid
        shares = args.capital / yes_price
        payoff = shares * (1.0 if outcome == 1 else 0.0)
        buy_pnls.append(payoff - args.capital)
    if buy_pnls:
        print(f"    n={len(buy_pnls)} total_pnl_usd={sum(buy_pnls):.3f} "
              f"wins={sum(1 for p in buy_pnls if p > 0)} losses={sum(1 for p in buy_pnls if p < 0)} "
              f"avg_pnl={sum(buy_pnls)/len(buy_pnls):.4f}")

    # --- Grid of long-shot SELL filters
    print(f"\n=== Long-shot SELL filter grid (capital ${args.capital}/trade, buy-NO mechanics) ===")
    grid_results = []
    for max_mid in (0.05, 0.10, 0.15, 0.20):
        for min_delta in (0.01, 0.02, 0.05, 0.10):
            filtered = apply_filter(
                ok_rows,
                side="SELL",
                max_mid=max_mid,
                min_edge_delta=min_delta,
            )
            score = score_filter(filtered, capital=args.capital)
            grid_results.append({
                "filter": {"max_mid": max_mid, "min_edge_delta": min_delta},
                **score,
            })
            wr = score.get("win_rate")
            ap = score.get("avg_pnl_per_trade_usd")
            wr_str = "  n/a  " if wr is None else f"{wr:6.2%}"
            ap_str = "  n/a  " if ap is None else f"{ap:+7.3f}"
            print(
                f"  mid<{max_mid:.2f} & |delta|>={min_delta:.2f}: "
                f"n={score['n']:>3d} pnl=${score['total_pnl_usd']:+8.2f} "
                f"win_rate={wr_str} avg=${ap_str} "
                f"capital_at_risk=${score['capital_at_risk_usd']:.0f}"
            )

    # --- Headline: user-specified filter
    print(f"\n=== HEADLINE: user-specified filter (mid<0.15 AND p_llm<mid-0.02) ===")
    headline = apply_filter(
        ok_rows, side="SELL", max_mid=0.15, min_edge_delta=0.02,
    )
    headline_score = score_filter(headline, capital=args.capital)
    print(json.dumps(headline_score, indent=2))
    if headline:
        print(f"\nSample 10 trades (by edge magnitude):")
        sorted_h = sorted(headline, key=lambda r: r["p_llm"] - r["market_p_t7"])
        for r in sorted_h[:10]:
            outcome = int(r["resolved_outcome"])
            mid = float(r["market_p_t7"])
            profit, _ = buy_no_pnl(mid, outcome, args.capital)
            print(
                f"  y={outcome} mid={mid:.3f} p_llm={r['p_llm']:.3f} delta={r['p_llm']-mid:+.3f} "
                f"pnl=${profit:+.3f}  | {(r.get('question') or '')[:70]}"
            )

    # --- Also: combine with niche filter — long-shot SELL only in awards_entertainment
    aw_only = [r for r in ok_rows if r.get("niche_llm") == "awards_entertainment"]
    aw_filtered = apply_filter(aw_only, side="SELL", max_mid=0.15, min_edge_delta=0.02)
    aw_score = score_filter(aw_filtered, capital=args.capital)
    print(f"\n=== Same filter restricted to niche=awards_entertainment only ===")
    print(f"  total n in niche: {len(aw_only)}")
    print(json.dumps(aw_score, indent=2))

    summary = {
        "baseline_all_sell": score_filter(baseline_sell, capital=args.capital),
        "baseline_all_buy_pnl_usd": round(sum(buy_pnls), 3) if buy_pnls else 0.0,
        "baseline_all_buy_n": len(baseline_buy),
        "headline_filter": {"max_mid": 0.15, "min_edge_delta": 0.02, **headline_score},
        "headline_within_awards": aw_score,
        "grid": grid_results,
        "capital_per_trade_usd": args.capital,
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(summary, indent=2) + "\n")
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
