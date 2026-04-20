from __future__ import annotations

import argparse
import json
import os
import socket
import threading
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# Local modules (keep imports light and optional)
try:
    from polymarket_rbi_bot.config import BotConfig
except Exception:  # pragma: no cover - dashboard should still run without optional deps
    BotConfig = None  # type: ignore

try:
    from data.market_discovery import GammaMarketDiscoveryClient
except Exception:  # pragma: no cover - dashboard should still run without optional deps
    GammaMarketDiscoveryClient = None  # type: ignore


# --------- Data loading helpers ---------

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LIVE_STATE_PATH = DATA_DIR / "live_state.json"
PAPER_LOG_PATH = DATA_DIR / "paper_trades.jsonl"
CLASSIFIER_INPUT_PATH = DATA_DIR / "market_classifier_input.json"
CLASSIFIER_OUTPUT_PATH = DATA_DIR / "market_classifier_output.json"
QUOTE_MANIFEST_PATH = DATA_DIR / "quote_backtests" / "manifest.json"


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_jsonl_tail(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = [json.loads(line) for line in lines[-limit:]]
        return rows
    except Exception:
        return []


def _read_probe_results(limit: int = 10) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for path in sorted(DATA_DIR.glob("passive_fill_probe_*_result.json"))[-limit:]:
            row = _read_json(path, {})
            if isinstance(row, dict) and row:
                rows.append(row)
    except Exception:
        return []
    return rows


def _env_config_summary() -> dict[str, Any]:
    try:
        cfg = BotConfig.from_env() if BotConfig is not None else None
    except Exception:
        cfg = None
    return {
        "strict_strategy_mode": bool(getattr(cfg, "strict_strategy_mode", False)),
        "buy_only_mode": bool(getattr(cfg, "buy_only_mode", True)),
        "llm_market_classifier_path": getattr(cfg, "llm_market_classifier_path", None),
        "state_path": getattr(cfg, "state_path", str(LIVE_STATE_PATH)),
        "openai_classifier_output_path": getattr(cfg, "openai_classifier_output_path", str(CLASSIFIER_OUTPUT_PATH)),
    }


def _load_token_metadata() -> dict[str, dict[str, str]]:
    manifest = _read_json(QUOTE_MANIFEST_PATH, [])
    token_meta: dict[str, dict[str, str]] = {}
    if isinstance(manifest, list):
        for row in manifest:
            token_id = str(row.get("token_id") or "").strip()
            if not token_id:
                continue
            token_meta[token_id] = {
                "question": str(row.get("question") or ""),
                "outcome": str(row.get("outcome") or ""),
                "condition_id": str(row.get("condition_id") or ""),
            }
    return token_meta


def _short_token(token: str | None) -> str:
    text = str(token or "").strip()
    if len(text) <= 16:
        return text
    return f"{text[:8]}…{text[-6:]}"


def _maybe_backfill_token_meta(token_meta: dict[str, dict[str, str]], token_id: str | None) -> dict[str, str]:
    token = str(token_id or "").strip()
    if not token:
        return {}
    if token in token_meta and token_meta[token].get("question"):
        return token_meta[token]
    if GammaMarketDiscoveryClient is None:
        return token_meta.get(token, {})
    try:
        client = GammaMarketDiscoveryClient(timeout=10)
        market = client.find_market_by_token_id(token, limit=1000)
        token_meta[token] = {
            "question": str(market.get("question") or token_meta.get(token, {}).get("question") or ""),
            "outcome": str(token_meta.get(token, {}).get("outcome") or market.get("outcome") or ""),
            "condition_id": str(market.get("conditionId") or token_meta.get(token, {}).get("condition_id") or ""),
        }
    except Exception:
        pass
    return token_meta.get(token, {})


def _market_label(token_meta: dict[str, dict[str, str]], *, token_id: str | None = None, question: str | None = None, outcome: str | None = None) -> str:
    token = str(token_id or "").strip()
    meta = _maybe_backfill_token_meta(token_meta, token) if token else {}
    q = str(question or meta.get("question") or "").strip()
    o = str(outcome or meta.get("outcome") or "").strip()
    if q and o:
        return f"{q} ({o})"
    if q:
        return q
    if o and token:
        return f"{o} ({_short_token(token)})"
    return _short_token(token) or ""


# --------- Aggregation logic ---------

def build_summary(tail: int = 200) -> dict[str, Any]:
    now = _utcnow()
    state = _read_json(LIVE_STATE_PATH, {})
    paper_rows = _read_jsonl_tail(PAPER_LOG_PATH, limit=max(50, tail))
    config_summary = _env_config_summary()
    token_meta = _load_token_metadata()

    # Latest market quotes per token from paper logs
    latest_quotes: dict[str, dict[str, Any]] = {}
    latest_signal_ctx: dict[str, dict[str, Any]] = {}
    last_paper_ts: datetime | None = None
    for row in paper_rows:
        ts = _parse_iso(str(row.get("timestamp"))) or now
        if last_paper_ts is None or ts > last_paper_ts:
            last_paper_ts = ts
        token = str(row.get("token_id") or "")
        if token:
            best_bid = row.get("best_bid") or (row.get("snapshot") or {}).get("best_bid")
            best_ask = row.get("best_ask") or (row.get("snapshot") or {}).get("best_ask")
            latest_quotes[token] = {
                "mid": row.get("mid_price"),
                "bid": best_bid,
                "ask": best_ask,
                "question": row.get("question"),
                "status": row.get("status"),
                "ts": ts.isoformat(),
            }
            if isinstance(row.get("signal_summary"), dict):
                latest_signal_ctx[token] = row["signal_summary"]

    # Positions and unrealized PnL
    positions_payload = []
    unrealized_total = 0.0
    for token_id, payload in (state.get("positions") or {}).items():
        qty = float(payload.get("quantity") or 0.0)
        avg = float(payload.get("average_price") or 0.0)
        opened_at = _parse_iso(payload.get("opened_at"))
        quotes = latest_quotes.get(token_id, {})
        mid = quotes.get("mid")
        bid = quotes.get("bid")
        ask = quotes.get("ask")
        mid_price = float(mid) if mid is not None else None
        unrealized = None
        unrealized_bps = None
        if mid_price is not None and qty > 0 and avg > 0:
            unrealized = (mid_price - avg) * qty
            unrealized_bps = ((mid_price - avg) / avg) * 10_000
            unrealized_total += unrealized
        maturity = None
        signal_ctx = latest_signal_ctx.get(token_id) or {}
        if signal_ctx:
            maturity = signal_ctx.get("maturity")
        question = quotes.get("question") or token_meta.get(token_id, {}).get("question")
        outcome = token_meta.get(token_id, {}).get("outcome")
        positions_payload.append(
            {
                "token_id": token_id,
                "market_label": _market_label(token_meta, token_id=token_id, question=question, outcome=outcome),
                "quantity": qty,
                "average_price": avg,
                "opened_at": opened_at.isoformat() if opened_at else None,
                "minutes_since_entry": round((now - opened_at).total_seconds() / 60.0, 1) if opened_at else None,
                "mid": mid_price,
                "bid": float(bid) if bid is not None else None,
                "ask": float(ask) if ask is not None else None,
                "unrealized_pnl": round(unrealized, 4) if unrealized is not None else None,
                "unrealized_pnl_bps": round(unrealized_bps, 1) if unrealized_bps is not None else None,
                "question": question,
                "outcome": outcome,
                "maturity": maturity,
            }
        )

    # Orders / fills (recent window only)
    open_orders = []
    for item in list((state.get("open_orders") or {}).values()):
        token_id = str(item.get("asset_id") or item.get("token_id") or "")
        outcome = item.get("outcome") or token_meta.get(token_id, {}).get("outcome")
        question = item.get("question") or token_meta.get(token_id, {}).get("question")
        enriched = dict(item)
        enriched["token_id"] = token_id
        enriched["question"] = question
        enriched["outcome"] = outcome
        enriched["market_label"] = _market_label(token_meta, token_id=token_id, question=question, outcome=outcome)
        open_orders.append(enriched)
    fills = list((state.get("fills") or []))
    fills_recent = []
    for item in fills[-50:]:
        token_id = str(item.get("token_id") or item.get("asset_id") or "")
        raw = item.get("raw") or {}
        outcome = item.get("outcome") or raw.get("outcome") or token_meta.get(token_id, {}).get("outcome")
        question = item.get("question") or raw.get("question") or token_meta.get(token_id, {}).get("question")
        enriched = dict(item)
        enriched["token_id"] = token_id
        enriched["question"] = question
        enriched["outcome"] = outcome
        enriched["market_label"] = _market_label(token_meta, token_id=token_id, question=question, outcome=outcome)
        fills_recent.append(enriched)

    # Paper activity counters
    status_counts = Counter([str(row.get("status")) for row in paper_rows])
    block_reasons = Counter([str(row.get("reason")) for row in paper_rows if row.get("status") in {"no_trade", "blocked"}])
    recent_paper = [
        {
            "timestamp": row.get("timestamp"),
            "status": row.get("status"),
            "reason": row.get("reason"),
            "token_id": row.get("token_id"),
            "price": (row.get("intent") or {}).get("price"),
            "side": (row.get("intent") or {}).get("side"),
            "mid_price": row.get("mid_price"),
            "question": row.get("question") or token_meta.get(str(row.get("token_id") or ""), {}).get("question"),
            "outcome": row.get("outcome") or token_meta.get(str(row.get("token_id") or ""), {}).get("outcome"),
            "market_label": _market_label(
                token_meta,
                token_id=str(row.get("token_id") or ""),
                question=row.get("question"),
                outcome=row.get("outcome"),
            ),
        }
        for row in paper_rows[-50:]
    ]
    recent_probes = [
        {
            "probe_id": row.get("probe_id"),
            "question": row.get("question"),
            "outcome": row.get("outcome"),
            "market_label": _market_label(
                token_meta,
                token_id=str(row.get("token_id") or ""),
                question=row.get("question"),
                outcome=row.get("outcome"),
            ),
            "price": row.get("price"),
            "size": row.get("size"),
            "posted_notional": row.get("posted_notional"),
            "matched_size": row.get("matched_size"),
            "final_status": row.get("final_status"),
            "filled": row.get("filled"),
            "order_id": row.get("order_id"),
        }
        for row in _read_probe_results(limit=10)
    ]

    # Decision context: pick the most recent with signal_summary
    latest_signal_row = None
    for row in reversed(paper_rows):
        if not isinstance(row.get("signal_summary"), dict):
            continue
        token_id = str(row.get("token_id") or "")
        question = str(row.get("question") or "")
        if token_id == "test-token" or question.lower().startswith("test"):
            continue
        latest_signal_row = row
        break
    if latest_signal_row is None:
        for row in reversed(paper_rows):
            if isinstance(row.get("signal_summary"), dict):
                latest_signal_row = row
                break
    decision_context = None
    if latest_signal_row:
        ss = latest_signal_row.get("signal_summary") or {}
        decision_context = {
            "timestamp": latest_signal_row.get("timestamp"),
            "token_id": latest_signal_row.get("token_id"),
            "question": latest_signal_row.get("question") or token_meta.get(str(latest_signal_row.get("token_id") or ""), {}).get("question"),
            "outcome": latest_signal_row.get("outcome") or token_meta.get(str(latest_signal_row.get("token_id") or ""), {}).get("outcome"),
            "market_label": _market_label(
                token_meta,
                token_id=str(latest_signal_row.get("token_id") or ""),
                question=latest_signal_row.get("question"),
                outcome=latest_signal_row.get("outcome"),
            ),
            "buy_score": ss.get("buy_score"),
            "sell_score": ss.get("sell_score"),
            "expected_edge_bps": ss.get("expected_edge_bps"),
            "observed_spread_bps": ss.get("observed_spread_bps"),
            "maturity": ss.get("maturity"),
            "microstructure": ss.get("microstructure"),
            "signals": ss.get("signals"),
        }

    # Health
    cooldowns = state.get("cooldowns") or {}
    reconcile = state.get("reconcile") or {}
    last_submission_at = _parse_iso(cooldowns.get("last_submission_at"))
    last_fill_at = _parse_iso(cooldowns.get("last_fill_at"))
    reconcile_attempt = _parse_iso(reconcile.get("last_attempt_at"))
    last_log_age_sec = (now - last_paper_ts).total_seconds() if last_paper_ts else None

    # Classifier snapshot
    classifier_output = _read_json(CLASSIFIER_OUTPUT_PATH, [])
    classifier_count = 0
    try:
        if isinstance(classifier_output, list):
            classifier_count = len(classifier_output)
        elif isinstance(classifier_output, dict):
            if isinstance(classifier_output.get("records"), list):
                classifier_count = len(classifier_output["records"])
            elif isinstance(classifier_output.get("markets"), list):
                classifier_count = len(classifier_output["markets"])
            else:
                classifier_count = len(classifier_output)
    except Exception:
        classifier_count = 0

    payload = {
        "generated_at": now.isoformat(),
        "sources": {
            "live_state": str(LIVE_STATE_PATH),
            "paper_log": str(PAPER_LOG_PATH),
            "classifier_output": str(CLASSIFIER_OUTPUT_PATH),
        },
        "config": config_summary,
        "pnl": {
            "realized": float(state.get("realized_pnl") or 0.0),
            "unrealized": round(float(unrealized_total), 4),
            "total": round(float(state.get("realized_pnl") or 0.0) + float(unrealized_total), 4),
        },
        "positions": positions_payload,
        "orders": {
            "open": open_orders,
            "open_count": len(open_orders),
            "fills_recent": fills_recent,
            "fills_count": len(fills),
        },
        "activity": {
            "paper_entries": len(paper_rows),
            "paper_status_counts": dict(status_counts),
            "top_block_reasons": block_reasons.most_common(8),
            "recent_paper": recent_paper,
            "recent_probes": recent_probes,
        },
        "health": {
            "cooldowns": cooldowns,
            "reconcile": reconcile,
            "last_submission_at": last_submission_at.isoformat() if last_submission_at else None,
            "last_fill_at": last_fill_at.isoformat() if last_fill_at else None,
            "reconcile_last_attempt_at": reconcile_attempt.isoformat() if reconcile_attempt else None,
            "last_log_age_seconds": round(last_log_age_sec, 1) if last_log_age_sec is not None else None,
        },
        "decision_context": decision_context,
        "classifier": {
            "output_count": classifier_count,
            "path": str(CLASSIFIER_OUTPUT_PATH),
        },
    }
    return payload


# --------- HTTP server ---------

INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Polymarket RBI Bot - Local Dashboard</title>
    <style>
      body { font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 16px; color: #222; }
      h1 { margin: 0 0 8px 0; font-size: 20px; }
      .meta { color: #666; font-size: 12px; margin-bottom: 12px; }
      .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }
      .card { border: 1px solid #ddd; border-radius: 8px; padding: 12px; background: #fff; }
      table { width: 100%; border-collapse: collapse; font-size: 13px; }
      th, td { border-bottom: 1px solid #eee; padding: 6px 4px; text-align: left; }
      th { background: #fafafa; position: sticky; top: 0; }
      .pos { color: #0a7; }
      .neg { color: #c33; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; }
      .pill { display: inline-block; padding: 2px 6px; border-radius: 999px; background: #eee; font-size: 11px; }
      .small { font-size: 12px; color: #555; }
    </style>
  </head>
  <body>
    <h1>Polymarket RBI Bot — Local Dashboard</h1>
    <div class="meta" id="meta"></div>

    <div class="grid">
      <div class="card">
        <h3>PnL</h3>
        <div id="pnl"></div>
      </div>
      <div class="card">
        <h3>Health</h3>
        <div id="health"></div>
      </div>
      <div class="card">
        <h3>Config</h3>
        <div id="config"></div>
      </div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h3>Positions</h3>
      <div id="positions"></div>
    </div>

    <div class="grid" style="margin-top:12px;">
      <div class="card">
        <h3>Orders / Fills</h3>
        <div id="orders"></div>
      </div>
      <div class="card">
        <h3>Recent Paper Activity</h3>
        <div id="paper"></div>
      </div>
    </div>

    <div class="card" style="margin-top:12px;">
      <h3>Decision Context</h3>
      <div id="decision"></div>
    </div>

    <script>
      async function fetchSummary() {
        const res = await fetch('/api/summary');
        return await res.json();
      }

      function esc(x){ return (x==null?'' : String(x)).replace(/[&<>]/g, s=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[s])) }
      function fmtPnL(v){ if(v==null) return '—'; const cls=v>=0?'pos':'neg'; return `<span class="${cls}">${v.toFixed(4)}</span>`; }
      function fmtBps(v){ if(v==null) return '—'; const cls=v>=0?'pos':'neg'; return `<span class="${cls}">${v.toFixed(1)} bps</span>`; }
      function fmtTs(ts){ if(!ts) return '—'; const d = new Date(ts); return d.toLocaleString(); }

      function render(summary){
        document.getElementById('meta').innerHTML = `Generated at ${fmtTs(summary.generated_at)} • Sources: <span class=mono>${esc(summary.sources.live_state)}</span>, <span class=mono>${esc(summary.sources.paper_log)}</span>`;
        document.getElementById('pnl').innerHTML = `
          <div>Total: ${fmtPnL(summary.pnl.total)} | Realized: ${fmtPnL(summary.pnl.realized)} | Unrealized: ${fmtPnL(summary.pnl.unrealized)}</div>
        `;
        const h = summary.health;
        document.getElementById('health').innerHTML = `
          <div>Reconcile: <span class=pill>${esc(h.reconcile?.status||'unknown')}</span></div>
          <div class=small>Message: ${esc(h.reconcile?.message||'')}</div>
          <div class=small>Last submit: ${fmtTs(h.last_submission_at)} | Last fill: ${fmtTs(h.last_fill_at)}</div>
          <div class=small>Last log age: ${h.last_log_age_seconds!=null? esc(h.last_log_age_seconds+'s') : '—'}</div>
        `;
        const cfg = summary.config;
        document.getElementById('config').innerHTML = `
          <div>Strict mode: <b>${cfg.strict_strategy_mode? 'ON':'OFF'}</b>; Buy-only: <b>${cfg.buy_only_mode? 'ON':'OFF'}</b></div>
          <div class=small>Classifier path: <span class=mono>${esc(cfg.llm_market_classifier_path||'(none)')}</span></div>
        `;

        const posRows = summary.positions.map(p=>`
          <tr>
            <td>${esc(p.market_label||p.question||p.token_id||'')}</td>
            <td class=mono>${esc(p.token_id)}</td>
            <td>${esc(p.quantity)}</td>
            <td>${esc(p.average_price)}</td>
            <td>${p.mid!=null? esc(p.mid): '—'}</td>
            <td>${p.bid!=null? esc(p.bid): '—'}</td>
            <td>${p.ask!=null? esc(p.ask): '—'}</td>
            <td>${fmtBps(p.unrealized_pnl_bps)}</td>
            <td>${p.minutes_since_entry!=null? esc(p.minutes_since_entry+'m'): '—'}</td>
            <td>${esc(p.maturity?.time_to_resolution_hours!=null? p.maturity.time_to_resolution_hours.toFixed(1)+'h' : '—')}</td>
          </tr>`).join('');
        document.getElementById('positions').innerHTML = `
          <table>
            <thead><tr><th>Market</th><th>Token</th><th>Qty</th><th>Avg</th><th>Mid</th><th>Bid</th><th>Ask</th><th>Unrlzd</th><th>Held</th><th>TTR</th></tr></thead>
            <tbody>${posRows || '<tr><td colspan=10>No open positions</td></tr>'}</tbody>
          </table>`;

        const o = summary.orders;
        const openRows = (o.open||[]).map(x=>`<tr><td class=mono>${esc(x.order_id||x.id||'(id)')}</td><td>${esc(x.market_label||x.question||x.token_id||x.asset_id||x.market||'')}</td><td>${esc(x.side||'')}</td><td>${esc(x.price||'')}</td><td>${esc(x.size||'')}</td><td>${fmtTs(x.submitted_at||x.created_at)}</td></tr>`).join('');
        const fillRows = (o.fills_recent||[]).map(x=>`<tr><td class=mono>${esc(x.fill_id||'')}</td><td>${esc(x.market_label||x.question||x.token_id||'')}</td><td>${esc(x.side||'')}</td><td>${esc(x.price||'')}</td><td>${esc(x.size||'')}</td><td>${fmtTs(x.timestamp)}</td></tr>`).join('');
        document.getElementById('orders').innerHTML = `
          <div class=small>Open orders: ${esc(o.open_count)} | Fills: ${esc(o.fills_count)}</div>
          <div style="max-height:200px; overflow:auto; margin-top:6px;">
            <table><thead><tr><th>Order</th><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>Time</th></tr></thead><tbody>${openRows || '<tr><td colspan=6>No open orders</td></tr>'}</tbody></table>
            <div style="height:8px;"></div>
            <table><thead><tr><th>Fill</th><th>Market</th><th>Side</th><th>Price</th><th>Size</th><th>Time</th></tr></thead><tbody>${fillRows || '<tr><td colspan=6>No fills</td></tr>'}</tbody></table>
          </div>`;

        const paperRows = (summary.activity.recent_paper||[]).slice().reverse().map(r=>`<tr><td>${fmtTs(r.timestamp)}</td><td><span class=pill>${esc(r.status)}</span></td><td>${esc(r.reason||'')}</td><td>${esc(r.side||'')}</td><td>${esc(r.price||'')}</td><td>${esc(r.market_label||r.question||r.token_id||'')}</td></tr>`).join('');
        const probeRows = (summary.activity.recent_probes||[]).slice().reverse().map(r=>`<tr><td>${esc(r.probe_id ?? '')}</td><td>${esc(r.market_label||r.question||'')}</td><td>${esc(r.price ?? '')}</td><td>${esc(r.size ?? '')}</td><td>${esc(r.posted_notional ?? '')}</td><td>${esc(r.matched_size ?? '')}</td><td><span class=pill>${esc(r.final_status||'')}</span></td></tr>`).join('');
        const sc = summary.activity.paper_status_counts || {};
        const scText = Object.entries(sc).map(([k,v])=>`${k}:${v}`).join(' • ');
        document.getElementById('paper').innerHTML = `
          <div class=small>${esc(scText)}</div>
          <div style="max-height:240px; overflow:auto; margin-top:6px;">
          <table><thead><tr><th>Time</th><th>Status</th><th>Reason</th><th>Side</th><th>Price</th><th>Market</th></tr></thead><tbody>${paperRows || '<tr><td colspan=6>No recent entries</td></tr>'}</tbody></table>
          <div style="height:8px;"></div>
          <table><thead><tr><th>Probe</th><th>Market</th><th>Price</th><th>Size</th><th>Notional</th><th>Matched</th><th>Final</th></tr></thead><tbody>${probeRows || '<tr><td colspan=7>No recent probes</td></tr>'}</tbody></table>
          </div>`;

        const dc = summary.decision_context || {};
        const sigRows = (dc.signals||[]).map(s=>`<tr><td>${esc(s.strategy)}</td><td>${esc(s.side)}</td><td>${esc(s.confidence)}</td><td>${esc(s.reason||'')}</td></tr>`).join('');
        document.getElementById('decision').innerHTML = `
          <div class=small>Market: ${esc(dc.market_label||dc.question||'(latest)')} • <span class=mono>${esc(dc.token_id||'')}</span></div>
          <div class=small>Buy: ${esc(dc.buy_score)} | Sell: ${esc(dc.sell_score)} | Edge: ${esc(dc.expected_edge_bps)}</div>
          <div class=small>Spread: ${esc(dc.observed_spread_bps)} | Maturity TTR (h): ${esc(dc.maturity?.time_to_resolution_hours ?? '—')}</div>
          <div style="max-height:200px; overflow:auto; margin-top:6px;">
            <table><thead><tr><th>Strategy</th><th>Side</th><th>Conf</th><th>Reason</th></tr></thead><tbody>${sigRows || '<tr><td colspan=4>No signals in recent context</td></tr>'}</tbody></table>
          </div>`;
      }

      async function tick(){
        try { const s = await fetchSummary(); render(s); } catch (e) { console.error(e); }
      }
      tick();
      setInterval(tick, 5000);
    </script>
  </body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib signature)
        if self.path.startswith("/api/summary"):
            self._json(build_summary())
            return
        if self.path.startswith("/api/raw/state"):
            self._json(_read_json(LIVE_STATE_PATH, {}))
            return
        if self.path.startswith("/api/raw/paper"):
            self._json(_read_jsonl_tail(PAPER_LOG_PATH, 200))
            return
        # Default: serve the inline HTML index
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(INDEX_HTML.encode("utf-8"))

    def log_message(self, fmt: str, *args: Any) -> None:  # silence stdlib noisy logs
        return

    def _json(self, payload: Any) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(host: str, port: int) -> None:
    server = HTTPServer((host, port), DashboardHandler)
    print(f"Dashboard running on http://{host}:{port} (Ctrl+C to stop)")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local dashboard for Polymarket RBI bot (lightweight HTTP server)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8008, help="Bind port (default 8008)")
    parser.add_argument("--dump", action="store_true", help="Print summary JSON once and exit (no server)")
    args = parser.parse_args()

    if args.dump:
        print(json.dumps(build_summary(), indent=2, default=str))
        return

    run_server(args.host, args.port)


if __name__ == "__main__":
    main()

