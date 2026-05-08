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

try:
    from deploy.collector_health import evaluate as _collector_evaluate
except Exception:  # pragma: no cover
    _collector_evaluate = None  # type: ignore


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

    def _collector_block(shortlist_path: Path | None = None, run_jsonl_path: Path | None = None) -> dict[str, Any]:
        if _collector_evaluate is None:
            return {"available": False}
        try:
            ns = argparse.Namespace(
                window_hours=24.0,
                dead_threshold_minutes=5.0,
                shortlist_path=shortlist_path,
                run_jsonl_path=run_jsonl_path,
            )
            code, h = _collector_evaluate(ns)
            return {
                "available": True,
                "status_code": code,
                "status_label": {0: "healthy", 1: "dead", 2: "silent_drops"}.get(code, "unknown"),
                "watchlist_size": h.get("watchlist_size"),
                "watchlist_seen_in_window": h.get("watchlist_seen_in_window"),
                "silent_drops_count": h.get("silent_drops_count"),
                "silent_drops": (h.get("silent_drops") or [])[:5],
                "rows_in_window": (h.get("collector") or {}).get("rows_in_window"),
                "rows_last_hour": (h.get("collector") or {}).get("rows_last_hour"),
                "last_row_age_seconds": (h.get("collector") or {}).get("last_row_age_seconds"),
                "last_row_ts": (h.get("collector") or {}).get("last_row_ts"),
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": str(exc)}

    collector_summary = _collector_block()
    nonsports_run = DATA_DIR / "quote_collection" / "nonsports_run.jsonl"
    nonsports_shortlist = DATA_DIR / "scan_shortlist_nonsports.json"
    if nonsports_run.exists() and nonsports_shortlist.exists():
        collector_summary["nonsports"] = _collector_block(nonsports_shortlist, nonsports_run)

    payload = {
        "generated_at": now.isoformat(),
        "sources": {
            "live_state": str(LIVE_STATE_PATH),
            "paper_log": str(PAPER_LOG_PATH),
            "classifier_output": str(CLASSIFIER_OUTPUT_PATH),
        },
        "config": config_summary,
        "collector": collector_summary,
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

INDEX_HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1"/>
    <title>Polymarket RBI Bot — Dashboard</title>
    <style>
      :root {
        --bg: #0b0d12;
        --bg-card: #14171f;
        --bg-card-hover: #1a1e28;
        --bg-elevated: #1d212c;
        --border: #232834;
        --border-strong: #2e3441;
        --text: #e6e9f0;
        --text-dim: #9aa3b3;
        --text-faint: #6b7384;
        --accent: #6ea8ff;
        --accent-dim: rgba(110, 168, 255, 0.15);
        --pos: #4ade80;
        --neg: #f87171;
        --warn: #fbbf24;
        --info: #60a5fa;
        --pill-bg: #232834;
        --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      }
      * { box-sizing: border-box; }
      html, body { height: 100%; }
      body {
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background: var(--bg);
        color: var(--text);
        font-size: 14px;
        line-height: 1.45;
      }
      .container { max-width: 1600px; margin: 0 auto; padding: 20px 24px 64px; }
      header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; gap: 16px; flex-wrap: wrap; }
      header .title { display: flex; align-items: center; gap: 10px; }
      header h1 { margin: 0; font-size: 16px; font-weight: 600; letter-spacing: 0.2px; }
      header .badge { color: var(--text-dim); font-size: 11px; font-weight: 500; padding: 3px 8px; border: 1px solid var(--border); border-radius: 999px; }
      header .meta { color: var(--text-faint); font-size: 11px; font-family: var(--mono); }

      .section-label { font-size: 10px; font-weight: 700; letter-spacing: 1.2px; color: var(--text-faint); text-transform: uppercase; margin: 18px 0 10px; display: flex; align-items: center; gap: 8px; }
      .section-label .count { background: var(--pill-bg); color: var(--text-dim); font-weight: 600; padding: 1px 7px; border-radius: 999px; font-size: 10px; letter-spacing: 0.4px; }

      .grid { display: grid; gap: 12px; }
      .grid-3 { grid-template-columns: repeat(3, 1fr); }
      .grid-4 { grid-template-columns: repeat(4, 1fr); }
      .grid-2 { grid-template-columns: repeat(2, 1fr); }
      @media (max-width: 1100px) { .grid-4, .grid-3 { grid-template-columns: repeat(2, 1fr); } }
      @media (max-width: 720px)  { .grid-4, .grid-3, .grid-2 { grid-template-columns: 1fr; } }

      .card {
        background: var(--bg-card);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 14px 16px;
        transition: background 0.15s ease, border-color 0.15s ease;
      }
      .card:hover { background: var(--bg-card-hover); border-color: var(--border-strong); }
      .card .card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
      .card .card-title { font-size: 11px; font-weight: 600; letter-spacing: 0.6px; text-transform: uppercase; color: var(--text-dim); }
      .card .card-sub { font-size: 11px; color: var(--text-faint); font-family: var(--mono); }

      .stat { display: flex; align-items: baseline; gap: 8px; }
      .stat .num { font-size: 26px; font-weight: 600; letter-spacing: -0.3px; }
      .stat .unit { color: var(--text-faint); font-size: 12px; }
      .row { display: flex; align-items: center; justify-content: space-between; padding: 4px 0; }
      .row .k { color: var(--text-dim); font-size: 12px; }
      .row .v { font-family: var(--mono); font-size: 12px; }

      .pos { color: var(--pos); }
      .neg { color: var(--neg); }
      .warn { color: var(--warn); }
      .info { color: var(--info); }
      .dim { color: var(--text-dim); }
      .faint { color: var(--text-faint); }
      .mono { font-family: var(--mono); }

      .pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 8px; border-radius: 999px; background: var(--pill-bg); color: var(--text-dim); font-size: 11px; font-weight: 500; letter-spacing: 0.2px; }
      .pill .dot { width: 6px; height: 6px; border-radius: 999px; background: var(--text-faint); }
      .pill.ok { color: var(--pos); background: rgba(74, 222, 128, 0.12); } .pill.ok .dot { background: var(--pos); }
      .pill.warn { color: var(--warn); background: rgba(251, 191, 36, 0.12); } .pill.warn .dot { background: var(--warn); }
      .pill.bad { color: var(--neg); background: rgba(248, 113, 113, 0.12); } .pill.bad .dot { background: var(--neg); }
      .pill.info { color: var(--info); background: rgba(96, 165, 250, 0.12); } .pill.info .dot { background: var(--info); }

      table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
      th, td { border-bottom: 1px solid var(--border); padding: 7px 8px; text-align: left; vertical-align: top; }
      th { background: transparent; color: var(--text-faint); font-weight: 600; font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px; position: sticky; top: 0; z-index: 1; }
      tbody tr:hover { background: var(--bg-elevated); }
      td.mono, th.mono { font-family: var(--mono); font-size: 11.5px; color: var(--text-dim); }
      td.right, th.right { text-align: right; }
      tbody tr td:first-child { color: var(--text); }

      .scroll-y { overflow-y: auto; }
      .empty { color: var(--text-faint); font-size: 12px; padding: 12px 4px; text-align: center; }

      .stripe { display: flex; align-items: center; gap: 12px; }
      .stripe > * { white-space: nowrap; }

      ::-webkit-scrollbar { width: 8px; height: 8px; }
      ::-webkit-scrollbar-track { background: transparent; }
      ::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 4px; }
      ::-webkit-scrollbar-thumb:hover { background: #3b4252; }
    </style>
  </head>
  <body>
    <div class="container">
      <header>
        <div class="title">
          <h1>Polymarket RBI Bot</h1>
          <span class="badge" id="modeBadge">—</span>
          <span class="badge" id="phaseBadge">—</span>
        </div>
        <div class="meta" id="meta">Loading…</div>
      </header>

      <div class="section-label">Overview</div>
      <div class="grid grid-4">
        <div class="card"><div class="card-head"><span class="card-title">PnL — Total</span><span id="pnlPill" class="pill"><span class="dot"></span>—</span></div><div class="stat"><span class="num" id="pnlTotal">—</span><span class="unit">USDC</span></div><div class="row"><span class="k">Realized</span><span class="v" id="pnlRealized">—</span></div><div class="row"><span class="k">Unrealized</span><span class="v" id="pnlUnrealized">—</span></div></div>
        <div class="card"><div class="card-head"><span class="card-title">Quote Collector</span><span id="collectorPill" class="pill"><span class="dot"></span>—</span></div><div class="stat"><span class="num" id="collectorRowsHr">—</span><span class="unit">rows / 1h</span></div><div class="row"><span class="k">Watchlist seen</span><span class="v" id="collectorWatchlist">—</span></div><div class="row"><span class="k">Last row age</span><span class="v" id="collectorAge">—</span></div></div>
        <div class="card"><div class="card-head"><span class="card-title">Live Health</span><span id="healthPill" class="pill"><span class="dot"></span>—</span></div><div class="row"><span class="k">Reconcile</span><span class="v" id="healthReconcile">—</span></div><div class="row"><span class="k">Last submit</span><span class="v" id="healthSubmit">—</span></div><div class="row"><span class="k">Last fill</span><span class="v" id="healthFill">—</span></div></div>
        <div class="card"><div class="card-head"><span class="card-title">Activity</span><span id="activityPill" class="pill info"><span class="dot"></span>paper</span></div><div class="stat"><span class="num" id="activityCount">—</span><span class="unit">paper trades</span></div><div class="row"><span class="k">Open positions</span><span class="v" id="activityOpen">—</span></div><div class="row"><span class="k">Open orders</span><span class="v" id="activityOrders">—</span></div></div>
      </div>

      <div class="section-label">Positions <span class="count" id="positionsCount">0</span></div>
      <div class="card">
        <div id="positions"></div>
      </div>

      <div class="grid grid-2" style="margin-top:14px;">
        <div>
          <div class="section-label">Orders <span class="count" id="openOrdersCount">0</span></div>
          <div class="card"><div id="orders"></div></div>
          <div class="section-label" style="margin-top:14px;">Recent Fills <span class="count" id="fillsCount">0</span></div>
          <div class="card"><div id="fills"></div></div>
        </div>
        <div>
          <div class="section-label">Paper Activity <span class="count" id="paperCount">0</span></div>
          <div class="card"><div id="paperStatus" class="stripe" style="margin-bottom:8px;"></div><div id="paper"></div></div>
        </div>
      </div>

      <div class="grid grid-2" style="margin-top:14px;">
        <div>
          <div class="section-label">Decision Context</div>
          <div class="card"><div id="decision"></div></div>
        </div>
        <div>
          <div class="section-label">Block Reasons</div>
          <div class="card"><div id="blocks"></div></div>
        </div>
      </div>

      <div class="grid grid-2" style="margin-top:14px;">
        <div>
          <div class="section-label">Collector Drops</div>
          <div class="card"><div id="drops"></div></div>
        </div>
        <div>
          <div class="section-label">Config</div>
          <div class="card"><div id="config"></div></div>
        </div>
      </div>
    </div>

    <script>
      const $ = (id) => document.getElementById(id);
      const fetchJson = async (url) => (await fetch(url)).json();

      function esc(x){ return (x==null?'' : String(x)).replace(/[&<>]/g, s=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[s])) }
      function fmtNum(v, dp=4){ if(v==null||isNaN(v)) return '—'; return Number(v).toFixed(dp); }
      function fmtSigned(v, dp=4){ if(v==null||isNaN(v)) return '<span class="dim">—</span>'; const n=Number(v); const cls=n>=0?'pos':'neg'; const sgn=n>=0?'+':''; return `<span class="${cls}">${sgn}${n.toFixed(dp)}</span>`; }
      function fmtBps(v){ if(v==null||isNaN(v)) return '<span class="dim">—</span>'; const n=Number(v); const cls=n>=0?'pos':'neg'; const sgn=n>=0?'+':''; return `<span class="${cls}">${sgn}${n.toFixed(1)} bps</span>`; }
      function fmtTs(ts){ if(!ts) return '—'; const d=new Date(ts); if (isNaN(d)) return '—'; const now=Date.now(); const sec=Math.round((now-d.getTime())/1000); if (sec<60) return sec+'s ago'; if (sec<3600) return Math.round(sec/60)+'m ago'; if (sec<86400) return Math.round(sec/3600)+'h ago'; return d.toLocaleString(); }
      function fmtAgeS(s){ if(s==null) return '—'; if(s<60) return Math.round(s)+'s'; if(s<3600) return Math.round(s/60)+'m'; return Math.round(s/3600)+'h'; }
      function pill(cls, text){ return `<span class="pill ${cls}"><span class="dot"></span>${esc(text)}</span>`; }

      function renderOverview(s){
        const cfg = s.config || {};
        $('modeBadge').textContent = (cfg.strict_strategy_mode? 'STRICT MODE':'NORMAL MODE') + (cfg.buy_only_mode? ' • BUY-ONLY':'');
        $('phaseBadge').textContent = 'PHASE 1 → 1.5';
        $('meta').innerHTML = `Updated ${fmtTs(s.generated_at)} • <span class="mono">${esc(s.sources?.live_state||'')}</span>`;

        const p = s.pnl || {};
        const total = Number(p.total||0);
        $('pnlTotal').innerHTML = fmtSigned(total, 4);
        $('pnlRealized').innerHTML = fmtSigned(p.realized, 4);
        $('pnlUnrealized').innerHTML = fmtSigned(p.unrealized, 4);
        $('pnlPill').outerHTML = `<span id="pnlPill" class="pill ${total>0?'ok':total<0?'bad':''}"><span class="dot"></span>${total>0?'positive':total<0?'negative':'flat'}</span>`;

        const c = s.collector || {};
        const cls = c.status_label==='healthy'?'ok' : c.status_label==='dead'?'bad' : c.status_label==='silent_drops'?'warn' : '';
        $('collectorPill').outerHTML = `<span id="collectorPill" class="pill ${cls}"><span class="dot"></span>${esc(c.status_label||'unknown')}</span>`;
        $('collectorRowsHr').textContent = c.rows_last_hour!=null? c.rows_last_hour.toLocaleString() : '—';
        $('collectorWatchlist').innerHTML = c.watchlist_size!=null? `${c.watchlist_seen_in_window||0}/${c.watchlist_size}` : '—';
        $('collectorAge').textContent = fmtAgeS(c.last_row_age_seconds);

        const h = s.health || {};
        const recStatus = (h.reconcile && h.reconcile.status) || 'unknown';
        const recCls = recStatus==='ok'?'ok':recStatus==='unknown'?'':'warn';
        $('healthPill').outerHTML = `<span id="healthPill" class="pill ${recCls}"><span class="dot"></span>${esc(recStatus)}</span>`;
        $('healthReconcile').innerHTML = esc((h.reconcile && h.reconcile.message) || '—');
        $('healthSubmit').textContent = fmtTs(h.last_submission_at);
        $('healthFill').textContent = fmtTs(h.last_fill_at);

        $('activityCount').textContent = (s.activity?.paper_entries||0).toLocaleString();
        $('activityOpen').textContent = (s.positions?.length||0);
        $('activityOrders').textContent = (s.orders?.open_count||0);
      }

      function renderPositions(s){
        const positions = s.positions || [];
        $('positionsCount').textContent = positions.length;
        if (!positions.length) { $('positions').innerHTML = '<div class="empty">No open positions</div>'; return; }
        const rows = positions.map(p=>`
          <tr>
            <td>${esc(p.market_label||p.question||'')}</td>
            <td class="mono">${esc((p.token_id||'').slice(0,10))}…</td>
            <td class="right">${esc(p.quantity)}</td>
            <td class="right">${fmtNum(p.average_price,4)}</td>
            <td class="right">${p.mid!=null?fmtNum(p.mid,4):'<span class="dim">—</span>'}</td>
            <td class="right">${fmtBps(p.unrealized_pnl_bps)}</td>
            <td class="right"><span class="dim">${p.minutes_since_entry!=null? p.minutes_since_entry+'m' : '—'}</span></td>
            <td class="right"><span class="dim">${p.maturity?.time_to_resolution_hours!=null? p.maturity.time_to_resolution_hours.toFixed(1)+'h' : '—'}</span></td>
          </tr>`).join('');
        $('positions').innerHTML = `<table><thead><tr><th>Market</th><th>Token</th><th class="right">Qty</th><th class="right">Avg</th><th class="right">Mid</th><th class="right">Unrlzd</th><th class="right">Held</th><th class="right">TTR</th></tr></thead><tbody>${rows}</tbody></table>`;
      }

      function renderOrders(s){
        const o = s.orders || {open:[], fills_recent:[]};
        $('openOrdersCount').textContent = o.open_count||0;
        $('fillsCount').textContent = o.fills_count||0;
        const open = (o.open||[]).map(x=>`<tr><td class="mono">${esc((x.order_id||x.id||'').slice(0,10))}…</td><td>${esc(x.market_label||x.question||'')}</td><td>${pill('info', x.side||'')}</td><td class="right">${esc(x.price||'')}</td><td class="right">${esc(x.size||'')}</td><td class="dim">${fmtTs(x.submitted_at||x.created_at)}</td></tr>`).join('');
        $('orders').innerHTML = open ? `<table><thead><tr><th>ID</th><th>Market</th><th>Side</th><th class="right">Px</th><th class="right">Size</th><th>Time</th></tr></thead><tbody>${open}</tbody></table>` : '<div class="empty">No open orders</div>';
        const fills = (o.fills_recent||[]).map(x=>`<tr><td class="mono">${esc((x.fill_id||'').slice(0,10))}…</td><td>${esc(x.market_label||x.question||'')}</td><td>${pill(x.side==='BUY'?'ok':'bad', x.side||'')}</td><td class="right">${esc(x.price||'')}</td><td class="right">${esc(x.size||'')}</td><td class="dim">${fmtTs(x.timestamp)}</td></tr>`).join('');
        $('fills').innerHTML = fills ? `<table><thead><tr><th>ID</th><th>Market</th><th>Side</th><th class="right">Px</th><th class="right">Size</th><th>Time</th></tr></thead><tbody>${fills}</tbody></table>` : '<div class="empty">No fills</div>';
      }

      function renderPaper(s){
        const a = s.activity || {};
        $('paperCount').textContent = a.paper_entries||0;
        const sc = a.paper_status_counts || {};
        const stripeHTML = Object.entries(sc).map(([k,v])=>{
          const cls = k==='executed'||k==='filled'?'ok':k==='blocked'||k==='error'?'bad':k==='approved'?'info':'';
          return `<span class="pill ${cls}"><span class="dot"></span>${esc(k)} ${v}</span>`;
        }).join('');
        $('paperStatus').innerHTML = stripeHTML || '<span class="faint">No status counts</span>';
        const rows = (a.recent_paper||[]).slice().reverse().slice(0,18).map(r=>{
          const stCls = r.status==='executed'||r.status==='filled'?'ok':r.status==='blocked'||r.status==='error'?'bad':'info';
          return `<tr><td class="dim">${fmtTs(r.timestamp)}</td><td>${pill(stCls, r.status||'')}</td><td>${esc((r.reason||'').slice(0,60))}</td><td>${esc(r.side||'')}</td><td class="right">${esc(r.price||'')}</td><td>${esc(r.market_label||r.question||'')}</td></tr>`;
        }).join('');
        $('paper').innerHTML = rows ? `<div class="scroll-y" style="max-height:380px;"><table><thead><tr><th>Time</th><th>Status</th><th>Reason</th><th>Side</th><th class="right">Px</th><th>Market</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="empty">No recent activity</div>';
      }

      function renderDecision(s){
        const dc = s.decision_context || {};
        if (!dc.token_id && !dc.signals) { $('decision').innerHTML = '<div class="empty">No recent decision context</div>'; return; }
        const sigs = (dc.signals||[]).map(x=>`<tr><td>${esc(x.strategy)}</td><td>${pill(x.side==='BUY'?'ok':x.side==='SELL'?'bad':'info', x.side)}</td><td class="mono">${esc(x.confidence)}</td><td class="dim">${esc((x.reason||'').slice(0,80))}</td></tr>`).join('');
        $('decision').innerHTML = `
          <div class="row"><span class="k">Market</span><span class="v">${esc(dc.market_label||dc.question||'(latest)')}</span></div>
          <div class="row"><span class="k">Buy / Sell score</span><span class="v"><span class="pos">${esc(dc.buy_score)}</span> / <span class="neg">${esc(dc.sell_score)}</span></span></div>
          <div class="row"><span class="k">Expected edge</span><span class="v">${fmtBps(dc.expected_edge_bps)}</span></div>
          <div class="row"><span class="k">Spread</span><span class="v">${fmtBps(dc.observed_spread_bps)}</span></div>
          <div class="row"><span class="k">TTR (h)</span><span class="v">${esc(dc.maturity?.time_to_resolution_hours ?? '—')}</span></div>
          <div style="margin-top:8px;"><table><thead><tr><th>Strategy</th><th>Side</th><th>Conf</th><th>Reason</th></tr></thead><tbody>${sigs || ''}</tbody></table></div>
        `;
      }

      function renderBlocks(s){
        const top = (s.activity?.top_block_reasons || []);
        if (!top.length) { $('blocks').innerHTML = '<div class="empty">No blocks recorded</div>'; return; }
        const max = Math.max(...top.map(([_,n])=>n));
        const rows = top.map(([reason,n])=>{
          const w = Math.round((n/max)*100);
          return `<div style="margin:6px 0;"><div class="row"><span class="k">${esc(reason)}</span><span class="v">${n}</span></div><div style="background:var(--bg-elevated); height:4px; border-radius:2px; margin-top:2px;"><div style="background:var(--accent); height:100%; width:${w}%; border-radius:2px;"></div></div></div>`;
        }).join('');
        $('blocks').innerHTML = rows;
      }

      function renderDrops(s){
        const c = s.collector || {};
        if (!c.available) { $('drops').innerHTML = '<div class="empty">Collector module unavailable</div>'; return; }
        if (!c.silent_drops_count) { $('drops').innerHTML = `<div class="row"><span class="k">Silent drops</span><span class="v"><span class="pos">0</span></span></div><div class="row"><span class="k">Watchlist size</span><span class="v">${c.watchlist_size||'—'}</span></div><div class="row"><span class="k">Rows in 24h</span><span class="v">${(c.rows_in_window||0).toLocaleString()}</span></div>`; return; }
        const rows = (c.silent_drops||[]).map(d=>`<tr><td>${esc(d.question||'(no question)')}</td><td class="mono">${esc((d.condition_id||'').slice(0,12))}…</td></tr>`).join('');
        $('drops').innerHTML = `<div class="row"><span class="k">Silent drops</span><span class="v"><span class="warn">${c.silent_drops_count}</span></span></div><table style="margin-top:8px;"><thead><tr><th>Question</th><th>Condition</th></tr></thead><tbody>${rows}</tbody></table>`;
      }

      function renderConfig(s){
        const cfg = s.config || {};
        const ksrc = s.sources || {};
        $('config').innerHTML = `
          <div class="row"><span class="k">Strict strategy mode</span><span class="v">${cfg.strict_strategy_mode? '<span class="pos">ON</span>' : '<span class="dim">OFF</span>'}</span></div>
          <div class="row"><span class="k">Buy-only mode</span><span class="v">${cfg.buy_only_mode? '<span class="pos">ON</span>' : '<span class="dim">OFF</span>'}</span></div>
          <div class="row"><span class="k">LLM classifier</span><span class="v"><span class="mono dim">${esc(cfg.llm_market_classifier_path||'(none)')}</span></span></div>
          <div class="row"><span class="k">Live state</span><span class="v"><span class="mono dim">${esc(ksrc.live_state||'')}</span></span></div>
          <div class="row"><span class="k">Paper log</span><span class="v"><span class="mono dim">${esc(ksrc.paper_log||'')}</span></span></div>
        `;
      }

      async function tick(){
        try {
          const s = await fetchJson('/api/summary');
          renderOverview(s);
          renderPositions(s);
          renderOrders(s);
          renderPaper(s);
          renderDecision(s);
          renderBlocks(s);
          renderDrops(s);
          renderConfig(s);
        } catch (e) {
          console.error(e);
          $('meta').textContent = 'Error fetching summary: ' + (e?.message || e);
        }
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

