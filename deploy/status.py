"""Single-command status dashboard for the LLM-probability bot.

Shows everything we care about on one screen:
  - USDC balance + L2 auth status
  - Max-cap setting + remaining capital
  - Open orders (count + total notional + per-token preview)
  - Positions + realized PnL (from live_state.json)
  - LLM predictions: total / recent (last 24h)
  - Tradeable candidates above the current filter (mid<0.20, |Δ|≥0.05, SELL only)
  - Paper-trade activity (last 7 days)
  - Stage A retro PnL headline (if file present)

Usage:
    python -m deploy.status
    python -m deploy.status --max-cap-usd 100   # override the soft cap
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from polymarket_rbi_bot.config import BotConfig

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
except ImportError:
    ClobClient = None  # type: ignore


ROOT = Path(__file__).resolve().parents[1]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _try_parse_ts(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def fetch_clob_state(config: BotConfig) -> dict[str, Any]:
    out: dict[str, Any] = {"connected": False}
    if not config.has_l2_auth or ClobClient is None:
        out["error"] = "no L2 auth or py-clob-client unavailable"
        return out
    try:
        creds = ApiCreds(
            api_key=config.api_key,
            api_secret=config.api_secret,
            api_passphrase=config.api_passphrase,
        )
        client = ClobClient(
            config.host,
            key=config.private_key,
            chain_id=config.chain_id,
            creds=creds,
            signature_type=config.signature_type,
            funder=config.funder_address,
        )
        out["connected"] = True
        bal = client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type="COLLATERAL", token_id="")
        )
        raw_bal = bal.get("balance") if isinstance(bal, dict) else None
        if raw_bal is not None:
            try:
                out["usdc_balance"] = round(float(raw_bal) / 1_000_000, 4)
            except (TypeError, ValueError):
                out["usdc_balance_raw"] = raw_bal
        orders = client.get_orders()
        if isinstance(orders, list):
            out["open_orders"] = orders
            out["open_orders_count"] = len(orders)
        else:
            out["open_orders"] = []
            out["open_orders_count"] = 0
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-cap-usd", type=float, default=100.0,
                        help="Your self-imposed max total capital cap.")
    parser.add_argument("--filter-mid", type=float, default=0.20)
    parser.add_argument("--filter-delta", type=float, default=0.05)
    parser.add_argument("--skip-clob", action="store_true",
                        help="Don't query Polymarket for balance/orders (faster, offline-safe).")
    args = parser.parse_args()

    config = BotConfig.from_env()

    print("=" * 72)
    print(f" Polymarket RBI Bot — Status @ {datetime.now(tz=timezone.utc).isoformat(timespec='seconds')}")
    print("=" * 72)

    # --- Account ---
    print("\n[Account]")
    print(f"  Host:           {config.host}")
    print(f"  Has L2 auth:    {config.has_l2_auth}")
    print(f"  Self-cap (USD): ${args.max_cap_usd:.2f}")
    print(f"  Max notional/order: ${config.max_notional_per_order:.2f}  Daily loss limit: ${config.daily_loss_limit:.2f}")
    print(f"  Default order size: {config.default_order_size} shares")

    if args.skip_clob:
        clob = {"connected": False, "skipped": True}
    else:
        clob = fetch_clob_state(config)
    if clob.get("connected"):
        usdc = clob.get("usdc_balance")
        print(f"  USDC balance:   ${usdc:.4f}" if usdc is not None else "  USDC balance:   (unavailable)")
        print(f"  Open orders:    {clob.get('open_orders_count', 0)}")
        if usdc is not None:
            remaining_cap = args.max_cap_usd - (args.max_cap_usd - usdc)
            print(f"  Estimated capital remaining for new trades: ${min(usdc, args.max_cap_usd):.4f}")
    else:
        print(f"  CLOB query:    skipped" if clob.get("skipped") else f"  CLOB query:    FAILED ({clob.get('error')})")

    # Open orders preview
    open_orders = clob.get("open_orders") or []
    if open_orders:
        print("\n[Open orders]")
        for o in open_orders[:5]:
            try:
                cid = (o.get("market") or o.get("conditionId") or "?")[:14]
                side = o.get("side", "?")
                price = o.get("price", "?")
                size = o.get("size", "?")
                status = o.get("status", "?")
                print(f"  {side:<4} {price} x {size}  cid={cid}...  status={status}")
            except Exception:  # noqa: BLE001
                print(f"  (unparseable order: {str(o)[:80]})")

    # --- Live state file ---
    state_path = ROOT / config.state_path
    state = _load_json(state_path)
    print("\n[Live state file]")
    if state is None:
        print(f"  {state_path}: not present (no live trades yet)")
    else:
        positions = state.get("positions") or {}
        realized = state.get("realized_pnl", 0.0)
        fills = state.get("fills") or []
        print(f"  Positions:   {len(positions)}  realized PnL: ${realized:+.4f}  fills recorded: {len(fills)}")
        for token_id, pos in list(positions.items())[:5]:
            qty = pos.get("quantity", 0)
            avg = pos.get("average_price", 0)
            print(f"    pos {token_id[:14]}...  qty={qty}  avg_price={avg}")

    # --- LLM predictions ---
    preds_path = ROOT / config.llm_predictions_path
    preds = _load_jsonl(preds_path)
    fresh_cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    fresh_preds = []
    for p in preds:
        ts = _try_parse_ts(p.get("ts"))
        if ts and ts >= fresh_cutoff:
            fresh_preds.append(p)
    print("\n[LLM predictions]")
    print(f"  Total rows:    {len(preds)}")
    print(f"  Fresh (<24h):  {len(fresh_preds)}")

    # Tradeable candidates per current filter
    candidates = []
    for p in preds:
        mid = p.get("mid")
        p_llm = p.get("p_llm")
        conf = p.get("confidence_llm")
        edge_bps = p.get("edge_bps")
        if mid is None or p_llm is None or edge_bps is None:
            continue
        # SELL on long-shot: p_llm < mid by at least filter_delta, mid below max
        if p_llm < float(mid) - args.filter_delta and float(mid) < args.filter_mid:
            candidates.append(p)
    candidates.sort(key=lambda p: (p["p_llm"] - p["mid"]))
    print(f"\n[Tradeable SELL candidates: mid<{args.filter_mid}, p_llm < mid-{args.filter_delta}]")
    print(f"  Count: {len(candidates)}")
    for p in candidates[:8]:
        end = (p.get("end_date") or "")[:10]
        liq = float(p.get("liquidity") or 0)
        print(
            f"  edge={p['edge_bps']:+7.0f}bps  p_llm={p['p_llm']:.3f}  mid={p['mid']:.3f}  "
            f"conf={p.get('confidence_llm', 0):.2f}  liq=${liq:>7,.0f}  end={end}  "
            f"| {(p.get('question') or '')[:60]}"
        )

    # --- Paper trades ---
    paper_path = ROOT / "data/paper_trades.jsonl"
    paper = _load_jsonl(paper_path)
    print(f"\n[Paper trades log]  total rows: {len(paper)}")

    # --- Latest resolution results ---
    resolve_path = ROOT / "data/llm_resolution_results.json"
    resolve = _load_json(resolve_path)
    if resolve:
        print("\n[Latest Stage B resolution snapshot]")
        print(f"  resolved={resolve.get('n_resolved')}  tradeable={resolve.get('n_tradeable_above_edge')}  "
              f"verdict={resolve.get('verdict')}")
        print(f"  paper PnL total: ${resolve.get('paper_pnl_total_usd', 0):+.3f} "
              f"({resolve.get('tradeable_winners', 0)}W/{resolve.get('tradeable_losers', 0)}L)")
        print(f"  Brier(LLM)={resolve.get('overall_brier_llm')}  Brier(market)={resolve.get('overall_brier_market')}")

    # --- Stage A retro ---
    retro_path = ROOT / "data/llm_retro_filter_pnl.json"
    retro = _load_json(retro_path)
    if retro:
        head = retro.get("headline_filter") or {}
        all_sell = retro.get("baseline_all_sell") or {}
        print("\n[Stage A retro PnL evidence]")
        print(f"  All-SELL baseline (150 trades from 240 markets):")
        print(f"    n={all_sell.get('n')}  win_rate={all_sell.get('win_rate')}  pnl=${all_sell.get('total_pnl_usd')}  "
              f"brier_delta={all_sell.get('brier_delta_llm_minus_market')}")
        print(f"  Filter (mid<{retro.get('headline_filter', {}).get('max_mid', '?')}, "
              f"|Δ|≥{retro.get('headline_filter', {}).get('min_edge_delta', '?')}):")
        print(f"    n={head.get('n')}  win_rate={head.get('win_rate')}  pnl=${head.get('total_pnl_usd')}")

    print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
