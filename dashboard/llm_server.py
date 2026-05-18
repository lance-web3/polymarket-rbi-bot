"""Lightweight web dashboard for the LLM-probability-engine bot.

Single HTML page, auto-refreshes every 30s. Stdlib only.
Run:    python -m dashboard.llm_server
Open:   http://localhost:8765

Shows on one screen:
  - Capital (USDC balance, self-cap, utilization bar)
  - Open positions + orders (from data/live_state.json + live CLOB)
  - Tradeable candidates (SELL signals passing refined filter)
  - LLM predictions (total, fresh-24h)
  - Stage A retro PnL + latest Stage B resolution snapshot
  - System health (collectors, last prediction, last resolution)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polymarket_rbi_bot.config import BotConfig  # noqa: E402

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams
except ImportError:
    ClobClient = None  # type: ignore
    ApiCreds = None  # type: ignore
    BalanceAllowanceParams = None  # type: ignore


DATA_DIR = ROOT / "data"


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        return rows
    return rows


def _parse_ts(s: Any) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


# Cache live CLOB state for 30s to avoid hammering the API on every refresh
_clob_cache: dict[str, Any] = {"ts": None, "data": None}
_clob_lock = threading.Lock()


def fetch_clob_state(config: BotConfig, cache_seconds: float = 30.0) -> dict[str, Any]:
    now = datetime.now(tz=timezone.utc)
    with _clob_lock:
        if _clob_cache["ts"] is not None and (now - _clob_cache["ts"]).total_seconds() < cache_seconds:
            return _clob_cache["data"]  # type: ignore[return-value]
    out: dict[str, Any] = {"connected": False}
    if not config.has_l2_auth or ClobClient is None:
        out["error"] = "no L2 auth or py-clob-client unavailable"
        with _clob_lock:
            _clob_cache["ts"] = now
            _clob_cache["data"] = out
        return out
    try:
        creds = ApiCreds(api_key=config.api_key, api_secret=config.api_secret, api_passphrase=config.api_passphrase)
        client = ClobClient(
            config.host, key=config.private_key, chain_id=config.chain_id,
            creds=creds, signature_type=config.signature_type, funder=config.funder_address,
        )
        out["connected"] = True
        try:
            bal = client.get_balance_allowance(params=BalanceAllowanceParams(asset_type="COLLATERAL", token_id=""))
            raw = bal.get("balance") if isinstance(bal, dict) else None
            if raw is not None:
                out["usdc_balance"] = round(float(raw) / 1_000_000, 4)
        except Exception as e:
            out["balance_error"] = str(e)
        try:
            orders = client.get_orders()
            out["open_orders"] = orders if isinstance(orders, list) else []
            out["open_orders_count"] = len(out["open_orders"])
        except Exception as e:
            out["orders_error"] = str(e)
    except Exception as e:
        out["error"] = str(e)
    with _clob_lock:
        _clob_cache["ts"] = now
        _clob_cache["data"] = out
    return out


def build_state(self_cap_usd: float = 100.0, filter_mid: float = 0.20, filter_delta: float = 0.05) -> dict[str, Any]:
    config = BotConfig.from_env()
    clob = fetch_clob_state(config)

    # Predictions
    preds = _read_jsonl(DATA_DIR / "llm_predictions.jsonl")
    fresh_cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    fresh = [p for p in preds if (_parse_ts(p.get("ts")) or datetime(1970, 1, 1, tzinfo=timezone.utc)) >= fresh_cutoff]

    # De-dup predictions by condition_id (most recent)
    latest_by_cid: dict[str, dict[str, Any]] = {}
    for p in preds:
        cid = p.get("condition_id")
        if not cid:
            continue
        ts = p.get("ts") or ""
        cur = latest_by_cid.get(cid)
        if cur is None or ts > (cur.get("ts") or ""):
            latest_by_cid[cid] = p

    # Tradeable candidates: SELL, mid<filter_mid, p_llm < mid - filter_delta,
    # AND market not yet resolved (end_date > now)
    now_utc = datetime.now(tz=timezone.utc)
    candidates = []
    for p in latest_by_cid.values():
        mid = p.get("mid")
        p_llm = p.get("p_llm")
        edge_bps = p.get("edge_bps")
        if mid is None or p_llm is None or edge_bps is None:
            continue
        try:
            mid_f = float(mid)
            p_llm_f = float(p_llm)
        except Exception:
            continue
        if p_llm_f >= mid_f - filter_delta:
            continue
        if mid_f >= filter_mid:
            continue
        # Drop expired markets (end_date in the past = already resolved or closing now)
        end_dt = _parse_ts(p.get("end_date"))
        if end_dt is not None and end_dt < now_utc:
            continue
        candidates.append(p)
    candidates.sort(key=lambda p: p.get("edge_bps") or 0)

    # Live state file
    state = _read_json(DATA_DIR / "live_state.json") or {}
    positions = state.get("positions") or {}
    fills = state.get("fills") or []
    realized_pnl = state.get("realized_pnl", 0.0)

    # Recent resolutions snapshot
    resolution = _read_json(DATA_DIR / "llm_resolution_results.json")
    retro = _read_json(DATA_DIR / "llm_retro_filter_pnl.json")
    paper = _read_jsonl(DATA_DIR / "paper_trades.jsonl")

    # Capital exposure = sum of (open position notional + open order notional)
    # NOT (self_cap - USDC balance) — that conflates "low balance" with "high exposure"
    exposure_positions = 0.0
    for token_id, pos in (state.get("positions") or {}).items():
        try:
            qty = float(pos.get("quantity") or 0)
            avg = float(pos.get("average_price") or 0)
            if qty > 0 and avg > 0:
                exposure_positions += qty * avg
        except Exception:
            continue

    exposure_orders = 0.0
    for o in (clob.get("open_orders") or []):
        try:
            price = float(o.get("price") or 0)
            size = float(o.get("size") or 0)
            side = (o.get("side") or "").upper()
            if side == "BUY" and price > 0 and size > 0:
                # BUY orders lock USDC at price*size
                exposure_orders += price * size
        except Exception:
            continue

    total_exposure = round(exposure_positions + exposure_orders, 4)
    usdc = clob.get("usdc_balance")
    # Cap utilization: of self_cap_usd, how much is currently at risk?
    cap_pct = round(min(total_exposure / self_cap_usd * 100, 100), 1) if self_cap_usd > 0 else 0
    # Available to deploy = min(USDC balance, remaining headroom under self-cap)
    headroom = max(0.0, self_cap_usd - total_exposure)
    available = None if usdc is None else round(min(usdc, headroom), 4)

    return {
        "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "config": {
            "host": config.host,
            "has_l2_auth": config.has_l2_auth,
            "self_cap_usd": self_cap_usd,
            "max_notional_per_order": config.max_notional_per_order,
            "daily_loss_limit": config.daily_loss_limit,
            "default_order_size": config.default_order_size,
            "filter_mid": filter_mid,
            "filter_delta": filter_delta,
        },
        "capital": {
            "usdc_balance": usdc,
            "self_cap_usd": self_cap_usd,
            "exposure_positions_usd": round(exposure_positions, 4),
            "exposure_orders_usd": round(exposure_orders, 4),
            "total_exposure_usd": total_exposure,
            "available_to_deploy_usd": available,
            "cap_utilization_pct": cap_pct,
            "clob_error": clob.get("error") or clob.get("balance_error"),
        },
        "open_orders": clob.get("open_orders") or [],
        "open_orders_count": clob.get("open_orders_count", 0),
        "positions": positions,
        "positions_count": len(positions),
        "fills_count": len(fills),
        "realized_pnl": realized_pnl,
        "predictions": {
            "total": len(preds),
            "fresh_24h": len(fresh),
            "unique_markets": len(latest_by_cid),
        },
        "candidates": [
            {
                "question": p.get("question"),
                "event_slug": p.get("event_slug"),
                "side": p.get("side"),
                "p_llm": p.get("p_llm"),
                "mid": p.get("mid"),
                "edge_bps": p.get("edge_bps"),
                "confidence": p.get("confidence_llm"),
                "liquidity": p.get("liquidity"),
                "end_date": p.get("end_date"),
                "token_id": p.get("token_id"),
                "no_token_id": p.get("no_token_id"),
                "condition_id": p.get("condition_id"),
                "ts": p.get("ts"),
                "reasoning": (p.get("reasoning") or "")[:300],
            }
            for p in candidates[:12]
        ],
        "stage_b_resolution": (
            {
                "n_resolved": resolution.get("n_resolved"),
                "n_tradeable": resolution.get("n_tradeable_above_edge"),
                "paper_pnl_total_usd": resolution.get("paper_pnl_total_usd"),
                "winners": resolution.get("tradeable_winners"),
                "losers": resolution.get("tradeable_losers"),
                "brier_llm": resolution.get("overall_brier_llm"),
                "brier_market": resolution.get("overall_brier_market"),
                "verdict": resolution.get("verdict"),
            }
            if resolution
            else None
        ),
        "stage_a_retro": (
            {
                "baseline_all_sell": retro.get("baseline_all_sell"),
                "baseline_all_buy_pnl_usd": retro.get("baseline_all_buy_pnl_usd"),
                "headline_filter": retro.get("headline_filter"),
                "grid_top": sorted(
                    [g for g in (retro.get("grid") or []) if (g.get("win_rate") or 0) > 0 and g.get("n", 0) >= 5],
                    key=lambda g: -(g.get("total_pnl_usd") or 0),
                )[:5],
            }
            if retro
            else None
        ),
        "paper_trades": {
            "total": len(paper),
            "recent": paper[-5:] if paper else [],
        },
    }


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Polymarket LLM Bot — Status</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #0f1419; color: #e6e8eb; margin: 0; padding: 16px; font-size: 14px; }
  h1 { font-size: 18px; margin: 0 0 4px 0; }
  h2 { font-size: 14px; margin: 0 0 8px 0; color: #6c7681; text-transform: uppercase;
       letter-spacing: 0.06em; font-weight: 600; }
  .grid { display: grid; gap: 16px; grid-template-columns: 1fr 1fr; }
  .full { grid-column: span 2; }
  .card { background: #1a1f25; border: 1px solid #262d35; border-radius: 8px; padding: 16px; }
  .stat-row { display: flex; gap: 24px; flex-wrap: wrap; }
  .stat { display: flex; flex-direction: column; gap: 2px; }
  .stat .label { color: #6c7681; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
  .stat .value { font-size: 20px; font-weight: 600; color: #e6e8eb; font-variant-numeric: tabular-nums; }
  .stat .value.pos { color: #4ade80; }
  .stat .value.neg { color: #f87171; }
  .stat .sub { font-size: 11px; color: #6c7681; }
  .bar { height: 6px; background: #262d35; border-radius: 3px; overflow: hidden; margin-top: 8px; }
  .bar-fill { height: 100%; background: linear-gradient(90deg, #3b82f6, #2563eb); transition: width 0.5s; }
  .bar-fill.warning { background: linear-gradient(90deg, #fbbf24, #d97706); }
  .bar-fill.danger { background: linear-gradient(90deg, #ef4444, #dc2626); }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; color: #6c7681; font-weight: 500; font-size: 11px;
       text-transform: uppercase; letter-spacing: 0.05em; padding: 6px 8px;
       border-bottom: 1px solid #262d35; }
  td { padding: 8px; border-bottom: 1px solid #1a1f25; font-variant-numeric: tabular-nums; }
  td.q { max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #b8bfc7; }
  .side-buy { color: #4ade80; font-weight: 600; }
  .side-sell { color: #f87171; font-weight: 600; }
  .edge-pos { color: #4ade80; }
  .edge-neg { color: #f87171; }
  .ts { color: #6c7681; font-size: 11px; }
  .muted { color: #6c7681; }
  .footer { color: #4a525c; font-size: 11px; margin-top: 24px; text-align: center; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
           font-size: 11px; font-weight: 600; }
  .badge.go { background: #052e16; color: #4ade80; }
  .badge.nogo { background: #2c0e0e; color: #f87171; }
  .badge.idle { background: #1e293b; color: #94a3b8; }
  .empty { color: #6c7681; font-style: italic; padding: 12px 0; }
  details summary { cursor: pointer; color: #6c7681; font-size: 12px; padding: 6px 0; }
  details[open] summary { color: #b8bfc7; }
  .reasoning { color: #6c7681; font-size: 11px; padding: 4px 8px; line-height: 1.5; }
</style>
</head>
<body>
<h1>Polymarket LLM Bot — Live Status</h1>
<div class="ts">Last refresh: <span id="ts">—</span> · Next refresh in <span id="countdown">30</span>s</div>

<div class="grid" style="margin-top: 16px;">

  <div class="card full">
    <h2>Capital</h2>
    <div id="capital"></div>
  </div>

  <div class="card">
    <h2>Open Positions <span class="muted" id="positions-count"></span></h2>
    <div id="positions"></div>
  </div>

  <div class="card">
    <h2>Open Orders <span class="muted" id="orders-count"></span></h2>
    <div id="orders"></div>
  </div>

  <div class="card full">
    <h2>Tradeable Candidates <span class="muted">(SELL · mid&lt;<span id="filter-mid">0.20</span> · |Δ|≥<span id="filter-delta">0.05</span>)</span></h2>
    <div id="candidates"></div>
  </div>

  <div class="card">
    <h2>Predictions</h2>
    <div id="predictions"></div>
  </div>

  <div class="card">
    <h2>Stage B (latest resolver verdict)</h2>
    <div id="stage_b"></div>
  </div>

  <div class="card full">
    <h2>Stage A Retro Evidence</h2>
    <div id="stage_a"></div>
  </div>

</div>

<div class="footer">
  Stdlib HTTP dashboard · Refreshes every 30s · Read-only · No auto-trading enabled
</div>

<script>
  function $(id) { return document.getElementById(id); }

  function pct(v) { return (v == null) ? "—" : v.toFixed(1) + "%"; }
  function $num(v, digits) {
    if (v == null) return "—";
    digits = digits || 2;
    return (typeof v === "number") ? v.toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits}) : v;
  }
  function $dollar(v, digits) {
    if (v == null) return "—";
    digits = digits || 2;
    const sign = v < 0 ? "−" : "";
    return sign + "$" + $num(Math.abs(v), digits);
  }
  function colorPnl(v) {
    if (v == null) return "muted";
    return v > 0 ? "pos" : v < 0 ? "neg" : "";
  }

  function render(data) {
    $("ts").textContent = data.ts;
    $("filter-mid").textContent = data.config.filter_mid;
    $("filter-delta").textContent = data.config.filter_delta;

    // Capital
    const c = data.capital;
    let capHTML = '<div class="stat-row">';
    capHTML += '<div class="stat"><span class="label">USDC Balance</span><span class="value">' + $dollar(c.usdc_balance, 4) + '</span></div>';
    capHTML += '<div class="stat"><span class="label">Self-Cap</span><span class="value">' + $dollar(c.self_cap_usd, 2) + '</span></div>';
    capHTML += '<div class="stat"><span class="label">In Positions</span><span class="value">' + $dollar(c.exposure_positions_usd, 4) + '</span></div>';
    capHTML += '<div class="stat"><span class="label">In Open Orders</span><span class="value">' + $dollar(c.exposure_orders_usd, 4) + '</span></div>';
    capHTML += '<div class="stat"><span class="label">Total Exposure</span><span class="value">' + $dollar(c.total_exposure_usd, 4) + '</span></div>';
    capHTML += '<div class="stat"><span class="label">Available</span><span class="value pos">' + $dollar(c.available_to_deploy_usd, 4) + '</span></div>';
    capHTML += '<div class="stat"><span class="label">Realized PnL</span><span class="value ' + colorPnl(data.realized_pnl) + '">' + $dollar(data.realized_pnl, 4) + '</span></div>';
    capHTML += '<div class="stat"><span class="label">Total Fills</span><span class="value">' + data.fills_count + '</span></div>';
    capHTML += '</div>';
    const utilClass = (c.cap_utilization_pct || 0) > 80 ? 'danger' : (c.cap_utilization_pct || 0) > 50 ? 'warning' : '';
    capHTML += '<div class="bar"><div class="bar-fill ' + utilClass + '" style="width: ' + (c.cap_utilization_pct || 0) + '%"></div></div>';
    capHTML += '<div class="sub" style="margin-top:4px; color:#6c7681">Cap utilization: ' + pct(c.cap_utilization_pct) + '</div>';
    if (c.clob_error) capHTML += '<div class="empty">CLOB error: ' + c.clob_error + '</div>';
    $("capital").innerHTML = capHTML;

    // Positions
    $("positions-count").textContent = "(" + data.positions_count + ")";
    if (data.positions_count === 0) {
      $("positions").innerHTML = '<div class="empty">No open positions</div>';
    } else {
      let html = '<table><thead><tr><th>Token</th><th>Qty</th><th>Avg Price</th><th>Opened</th></tr></thead><tbody>';
      for (const [tid, pos] of Object.entries(data.positions)) {
        html += '<tr><td>' + tid.substring(0, 12) + '…</td><td>' + pos.quantity + '</td><td>' + pos.average_price + '</td><td class="ts">' + (pos.opened_at || '—') + '</td></tr>';
      }
      html += '</tbody></table>';
      $("positions").innerHTML = html;
    }

    // Orders
    $("orders-count").textContent = "(" + data.open_orders_count + ")";
    if (data.open_orders_count === 0) {
      $("orders").innerHTML = '<div class="empty">No open orders</div>';
    } else {
      let html = '<table><thead><tr><th>Side</th><th>Price</th><th>Size</th><th>Status</th></tr></thead><tbody>';
      for (const o of data.open_orders) {
        html += '<tr><td class="side-' + (o.side || '').toLowerCase() + '">' + (o.side || '?') + '</td><td>' + (o.price || '?') + '</td><td>' + (o.size || '?') + '</td><td>' + (o.status || '?') + '</td></tr>';
      }
      html += '</tbody></table>';
      $("orders").innerHTML = html;
    }

    // Candidates
    if (data.candidates.length === 0) {
      $("candidates").innerHTML = '<div class="empty">No candidates passing the current filter. Run <code>python -m deploy.llm_predict_markets</code> on a new event to populate.</div>';
    } else {
      let html = '<table><thead><tr><th>Side</th><th>Edge</th><th>p_LLM</th><th>Mid</th><th>Conf</th><th>Liq</th><th>End</th><th>Question</th></tr></thead><tbody>';
      for (const c of data.candidates) {
        const sideClass = (c.side || '').toLowerCase() === 'sell' ? 'side-sell' : 'side-buy';
        const edgeClass = (c.edge_bps || 0) > 0 ? 'edge-pos' : 'edge-neg';
        const liqStr = c.liquidity ? '$' + Math.round(c.liquidity).toLocaleString() : '—';
        const endStr = (c.end_date || '').substring(0, 10);
        html += '<tr>';
        html += '<td class="' + sideClass + '">' + (c.side || '?') + '</td>';
        html += '<td class="' + edgeClass + '">' + (c.edge_bps != null ? c.edge_bps.toFixed(0) + ' bps' : '—') + '</td>';
        html += '<td>' + $num(c.p_llm, 3) + '</td>';
        html += '<td>' + $num(c.mid, 3) + '</td>';
        html += '<td>' + $num(c.confidence, 2) + '</td>';
        html += '<td>' + liqStr + '</td>';
        html += '<td class="ts">' + endStr + '</td>';
        html += '<td class="q" title="' + (c.question || '').replace(/"/g, '&quot;') + '">' + (c.question || '') + '</td>';
        html += '</tr>';
        if (c.reasoning) {
          html += '<tr><td colspan="8" class="reasoning">↳ ' + c.reasoning + '</td></tr>';
        }
      }
      html += '</tbody></table>';
      $("candidates").innerHTML = html;
    }

    // Predictions
    const p = data.predictions;
    let predHTML = '<div class="stat-row">';
    predHTML += '<div class="stat"><span class="label">Total Rows</span><span class="value">' + p.total + '</span></div>';
    predHTML += '<div class="stat"><span class="label">Unique Markets</span><span class="value">' + p.unique_markets + '</span></div>';
    predHTML += '<div class="stat"><span class="label">Fresh (24h)</span><span class="value">' + p.fresh_24h + '</span></div>';
    predHTML += '<div class="stat"><span class="label">Paper Decisions</span><span class="value">' + data.paper_trades.total + '</span></div>';
    predHTML += '</div>';
    $("predictions").innerHTML = predHTML;

    // Stage B
    if (data.stage_b_resolution) {
      const sb = data.stage_b_resolution;
      let html = '<div class="stat-row">';
      const v = (sb.verdict || 'IDLE').toLowerCase();
      const badge = v === 'go' ? '<span class="badge go">GO</span>' : v === 'no_go' ? '<span class="badge nogo">NO_GO</span>' : '<span class="badge idle">' + (sb.verdict || 'PENDING') + '</span>';
      html += '<div class="stat"><span class="label">Verdict</span>' + badge + '</div>';
      html += '<div class="stat"><span class="label">Paper PnL</span><span class="value ' + colorPnl(sb.paper_pnl_total_usd) + '">' + $dollar(sb.paper_pnl_total_usd, 3) + '</span></div>';
      html += '<div class="stat"><span class="label">Resolved</span><span class="value">' + sb.n_resolved + '</span></div>';
      html += '<div class="stat"><span class="label">Tradeable</span><span class="value">' + sb.n_tradeable + '</span></div>';
      html += '<div class="stat"><span class="label">Wins/Losses</span><span class="value">' + (sb.winners || 0) + 'W / ' + (sb.losers || 0) + 'L</span></div>';
      html += '</div>';
      html += '<div class="sub" style="margin-top:8px">Brier(LLM): ' + $num(sb.brier_llm, 4) + ' · Brier(market): ' + $num(sb.brier_market, 4) + '</div>';
      $("stage_b").innerHTML = html;
    } else {
      $("stage_b").innerHTML = '<div class="empty">No resolver output yet. Run <code>python -m deploy.llm_resolve_predictions</code></div>';
    }

    // Stage A retro
    if (data.stage_a_retro && data.stage_a_retro.baseline_all_sell) {
      const sa = data.stage_a_retro;
      const all_sell = sa.baseline_all_sell;
      let html = '<div class="stat-row">';
      html += '<div class="stat"><span class="label">All-SELL Retro</span><span class="value ' + colorPnl(all_sell.total_pnl_usd) + '">' + $dollar(all_sell.total_pnl_usd, 2) + '</span><span class="sub">' + all_sell.n + ' trades · ' + (all_sell.win_rate * 100).toFixed(1) + '% win</span></div>';
      html += '<div class="stat"><span class="label">All-BUY Retro</span><span class="value ' + colorPnl(sa.baseline_all_buy_pnl_usd) + '">' + $dollar(sa.baseline_all_buy_pnl_usd, 2) + '</span><span class="sub">avoid — anti-edge</span></div>';
      if (sa.headline_filter) {
        const hf = sa.headline_filter;
        html += '<div class="stat"><span class="label">Headline Filter</span><span class="value ' + colorPnl(hf.total_pnl_usd) + '">' + $dollar(hf.total_pnl_usd, 2) + '</span><span class="sub">' + hf.n + ' trades · ' + ((hf.win_rate || 0) * 100).toFixed(1) + '% win</span></div>';
      }
      html += '</div>';
      if (sa.grid_top && sa.grid_top.length) {
        html += '<details style="margin-top:12px"><summary>Top filter variants by PnL</summary><table style="margin-top:8px"><thead><tr><th>Max Mid</th><th>Min |Δ|</th><th>N</th><th>Win Rate</th><th>PnL</th><th>Capital at Risk</th></tr></thead><tbody>';
        for (const g of sa.grid_top) {
          html += '<tr><td>' + g.filter.max_mid + '</td><td>' + g.filter.min_edge_delta + '</td><td>' + g.n + '</td><td>' + ((g.win_rate || 0) * 100).toFixed(1) + '%</td><td class="' + colorPnl(g.total_pnl_usd) + '">' + $dollar(g.total_pnl_usd, 2) + '</td><td>' + $dollar(g.capital_at_risk_usd, 0) + '</td></tr>';
        }
        html += '</tbody></table></details>';
      }
      $("stage_a").innerHTML = html;
    } else {
      $("stage_a").innerHTML = '<div class="empty">No retro data. Run <code>python -m deploy.llm_retro_filter_pnl</code></div>';
    }
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/state");
      const data = await resp.json();
      render(data);
    } catch (e) {
      console.error("refresh failed", e);
    }
  }

  let countdown = 30;
  setInterval(() => {
    countdown -= 1;
    if (countdown <= 0) {
      refresh();
      countdown = 30;
    }
    $("countdown").textContent = countdown;
  }, 1000);
  refresh();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # silence default logging
        pass

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            body = HTML_TEMPLATE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            try:
                state = build_state(
                    self_cap_usd=self.server.self_cap_usd,  # type: ignore[attr-defined]
                    filter_mid=self.server.filter_mid,  # type: ignore[attr-defined]
                    filter_delta=self.server.filter_delta,  # type: ignore[attr-defined]
                )
                body = json.dumps(state, default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--self-cap-usd", type=float, default=100.0)
    parser.add_argument("--filter-mid", type=float, default=0.20)
    parser.add_argument("--filter-delta", type=float, default=0.05)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.self_cap_usd = args.self_cap_usd  # type: ignore[attr-defined]
    server.filter_mid = args.filter_mid  # type: ignore[attr-defined]
    server.filter_delta = args.filter_delta  # type: ignore[attr-defined]

    print(f"LLM dashboard running at http://{args.host}:{args.port}")
    print(f"Self-cap=${args.self_cap_usd}  filter=(mid<{args.filter_mid}, |Δ|≥{args.filter_delta})")
    print("Open the URL above in your browser. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        server.shutdown()


if __name__ == "__main__":
    main()
