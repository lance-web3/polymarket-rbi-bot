"""Stage B — forward-looking LLM predictions on currently-open Polymarket markets.

Targets the edge pattern Stage A found: multi-bucket markets tied to one
underlying bracketed event (album releases, song contests, sports brackets,
awards prediction fields). Pulls currently-open markets matching `--event-slug`
(or grouped by shared event slug if `--auto-group`), runs Claude on each bucket,
computes edge vs current market mid, and writes ranked trade candidates.

Idempotent: skips markets with a prediction newer than `--cache-ttl-hours` in
the predictions JSONL. Safe to re-run.

Usage:
    # Predict on a specific event slug (recommended for testing)
    python -m deploy.llm_predict_markets --event-slug pga-championship-winner-2026

    # Predict on multiple PGA brackets in one command
    python -m deploy.llm_predict_markets \\
        --event-slug pga-championship-winner-2026 \\
        --event-slug 2026-pga-championship-top10 \\
        --event-slug 2026-pga-championship-top5 \\
        --event-slug 2026-pga-championship-top20

    # Auto-discover bracketed events with N+ markets
    python -m deploy.llm_predict_markets --auto-group --min-event-markets 5 --top 50

    # Show ranked candidates without running new LLM calls
    python -m deploy.llm_predict_markets --rank-only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.market_discovery import extract_yes_token, parse_jsonish_list


def _extract_outcome_tokens(market: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return (yes_token_id, no_token_id) strings."""
    token_ids = parse_jsonish_list(market.get("clobTokenIds"))
    outcomes = parse_jsonish_list(market.get("outcomes"))
    yes_id: str | None = None
    no_id: str | None = None
    for idx, tid in enumerate(token_ids):
        outcome = str(outcomes[idx]).strip().lower() if idx < len(outcomes) else ""
        if outcome == "yes":
            yes_id = str(tid)
        elif outcome == "no":
            no_id = str(tid)
    # Fallback: if outcomes aren't labeled, assume index 0 is YES, 1 is NO
    if yes_id is None and len(token_ids) >= 1:
        yes_id = str(token_ids[0])
    if no_id is None and len(token_ids) >= 2:
        no_id = str(token_ids[1])
    return yes_id, no_id
from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.llm_client import LLMError, LLMProbabilityClient

logger = logging.getLogger(__name__)


# --- Gamma helpers --------------------------------------------------------------

def fetch_open_markets_by_event_slug(
    config: BotConfig, event_slug: str, *, timeout: int = 30
) -> list[dict[str, Any]]:
    """Find currently-open markets whose first event has the given slug."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Pull pages, filter in Python (Gamma doesn't expose event-slug filter directly)
    for page in range(30):
        r = requests.get(
            f"{config.gamma_host}/markets",
            params={
                "limit": 100, "closed": "false", "archived": "false",
                "order": "liquidity", "ascending": "false", "offset": page * 100,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for m in data:
            cid = str(m.get("conditionId") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            events = m.get("events") or []
            ev_slug = events[0].get("slug") if events else None
            if ev_slug != event_slug:
                continue
            out.append(m)
        if len(data) < 100:
            break
    return out


def fetch_bracketed_events(
    config: BotConfig,
    *,
    min_event_markets: int = 5,
    max_pages: int = 30,
    timeout: int = 30,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Auto-discover currently-open events with >= min_event_markets bucket markets,
    ranked by total liquidity descending."""
    by_slug: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen: set[str] = set()
    for page in range(max_pages):
        r = requests.get(
            f"{config.gamma_host}/markets",
            params={
                "limit": 100, "closed": "false", "archived": "false",
                "order": "liquidity", "ascending": "false", "offset": page * 100,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        for m in data:
            cid = str(m.get("conditionId") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            events = m.get("events") or []
            slug = events[0].get("slug") if events else m.get("slug")
            if not slug:
                continue
            by_slug[slug].append(m)
        if len(data) < 100:
            break
    bracketed = [(slug, ms) for slug, ms in by_slug.items() if len(ms) >= min_event_markets]

    def score(item):
        return sum(
            float(m.get("liquidityNum") or m.get("liquidity") or 0) for m in item[1]
        )

    return sorted(bracketed, key=score, reverse=True)


# --- Predictions JSONL ----------------------------------------------------------

def load_predictions_index(path: Path, prompt_version: str, max_age: timedelta) -> set[str]:
    """Return set of condition_ids with a fresh prediction already on disk."""
    fresh: set[str] = set()
    if not path.exists():
        return fresh
    now = datetime.now(tz=timezone.utc)
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("prompt_version") != prompt_version:
                continue
            ts_raw = row.get("ts") or row.get("_meta", {}).get("ts")
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            if (now - ts) > max_age:
                continue
            cid = row.get("condition_id")
            if cid:
                fresh.add(str(cid))
    return fresh


def append_prediction(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


# --- Per-market prediction ------------------------------------------------------

def _safe_float(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def predict_market(
    client: LLMProbabilityClient,
    market: dict[str, Any],
    *,
    extra_context: str | None = None,
) -> dict[str, Any]:
    """Run LLM on one market and return a prediction row ready to append.

    The row records BOTH the YES token_id (as `token_id`) and the NO token_id
    (as `no_token_id`). LLMProbabilityStrategy can be queried with either
    token_id; if matched on NO, it flips p_llm → 1−p_llm and the side direction.
    This unlocks SELL-YES trades by buying NO (Polymarket has no shorting).
    """
    yes_token_id, no_token_id = _extract_outcome_tokens(market)
    bid = _safe_float(market.get("bestBid"))
    ask = _safe_float(market.get("bestAsk"))
    mid = None
    if bid is not None and ask is not None and ask > bid:
        mid = (bid + ask) / 2

    events = market.get("events") or []
    event_slug = events[0].get("slug") if events else None
    event_title = events[0].get("title") if events else None

    pred = client.predict(
        question=market.get("question") or "",
        description=market.get("description") or "",
        niche_hint=None,
        condition_id=str(market.get("conditionId") or ""),
        extra_context=extra_context,
    )

    p_llm = float(pred["p_yes"])
    edge_bps = None
    if mid is not None:
        edge_bps = round((p_llm - mid) * 10_000, 1)

    return {
        "ts": datetime.now(tz=timezone.utc).isoformat(),
        "prompt_version": pred["_meta"]["prompt_version"],
        "condition_id": str(market.get("conditionId") or ""),
        "token_id": yes_token_id or "",
        "no_token_id": no_token_id or "",
        "question": market.get("question"),
        "event_slug": event_slug,
        "event_title": event_title,
        "end_date": market.get("endDate"),
        "liquidity": market.get("liquidityNum") or market.get("liquidity"),
        "best_bid": bid,
        "best_ask": ask,
        "mid": mid,
        "p_llm": p_llm,
        "confidence_llm": float(pred["confidence"]),
        "niche_llm": pred["niche_classification"],
        "edge_bps": edge_bps,
        "side": "BUY" if (edge_bps is not None and edge_bps > 0) else ("SELL" if edge_bps is not None and edge_bps < 0 else "HOLD"),
        "evidence": pred.get("top_evidence"),
        "what_would_flip": pred.get("what_would_flip"),
        "reasoning": (pred.get("reasoning") or "")[:600],
        "_meta": pred.get("_meta"),
    }


# --- Ranking + reporting --------------------------------------------------------

def rank_candidates(
    path: Path,
    *,
    min_edge_bps: float = 300.0,
    min_confidence: float = 0.3,
    max_age_hours: float = 48.0,
) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    now = datetime.now(tz=timezone.utc)
    rows: dict[str, dict[str, Any]] = {}  # most-recent per condition_id
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = row.get("ts")
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            age_h = (now - ts).total_seconds() / 3600
            if age_h > max_age_hours:
                continue
            row["_age_hours"] = round(age_h, 2)
            cid = row.get("condition_id")
            if not cid:
                continue
            cur = rows.get(cid)
            if cur is None or ts > datetime.fromisoformat(cur["ts"].replace("Z", "+00:00")):
                rows[cid] = row
    candidates = []
    for r in rows.values():
        eb = r.get("edge_bps")
        if eb is None:
            continue
        if abs(eb) < min_edge_bps:
            continue
        if float(r.get("confidence_llm") or 0) < min_confidence:
            continue
        candidates.append(r)
    candidates.sort(key=lambda r: abs(r["edge_bps"]), reverse=True)
    return candidates


# --- Main ---------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-slug", action="append", default=[],
                        help="Event slug to predict on. Repeatable. If omitted, auto-discover with --auto-group.")
    parser.add_argument("--auto-group", action="store_true",
                        help="Auto-discover currently-open bracketed events with --min-event-markets+ bucket markets.")
    parser.add_argument("--min-event-markets", type=int, default=5)
    parser.add_argument("--top", type=int, default=30,
                        help="When using --auto-group: how many markets to predict on (top-N by liquidity).")
    parser.add_argument("--cache-ttl-hours", type=float, default=24.0,
                        help="Skip markets predicted within this window.")
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--rank-only", action="store_true",
                        help="Don't make new LLM calls; just print top candidates from existing predictions.")
    parser.add_argument("--min-edge-bps", type=float, default=300.0,
                        help="Min |model_p - mid| to surface in ranking.")
    parser.add_argument("--min-confidence", type=float, default=0.30)
    parser.add_argument("--limit-llm", type=int, default=None, help="Cap LLM calls in this invocation.")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config = BotConfig.from_env()
    if args.provider:
        config.llm_provider = args.provider
    output_path = Path(args.output or config.llm_predictions_path)

    if args.rank_only:
        candidates = rank_candidates(output_path, min_edge_bps=args.min_edge_bps,
                                     min_confidence=args.min_confidence)
        _print_candidates(candidates)
        return

    fresh_cids = load_predictions_index(output_path, prompt_version="v1",
                                        max_age=timedelta(hours=args.cache_ttl_hours))
    logger.info("Found %d fresh predictions on disk; will skip these.", len(fresh_cids))

    # Determine target markets
    targets: list[dict[str, Any]] = []
    if args.event_slug:
        for slug in args.event_slug:
            ms = fetch_open_markets_by_event_slug(config, slug)
            logger.info("Event slug=%s -> %d markets", slug, len(ms))
            targets.extend(ms)
    elif args.auto_group:
        bracketed = fetch_bracketed_events(config, min_event_markets=args.min_event_markets)
        logger.info("Auto-discovered %d bracketed events", len(bracketed))
        # Pick top-N markets across the top events by liquidity
        for slug, ms in bracketed[:10]:
            for m in ms:
                targets.append(m)
                if len(targets) >= args.top:
                    break
            if len(targets) >= args.top:
                break
    else:
        raise SystemExit("Specify --event-slug (repeatable) or --auto-group.")

    # De-dup by condition_id and skip fresh
    seen: set[str] = set()
    unique_targets = []
    for m in targets:
        cid = str(m.get("conditionId") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        if cid in fresh_cids:
            continue
        unique_targets.append(m)

    logger.info("After dedup + cache filter: %d markets to predict", len(unique_targets))

    if args.limit_llm is not None:
        unique_targets = unique_targets[: args.limit_llm]

    client = LLMProbabilityClient(config)
    n_done = 0
    for i, market in enumerate(unique_targets):
        try:
            row = predict_market(client, market)
        except LLMError as e:
            logger.error("LLM failure on %s: %s", market.get("conditionId"), e)
            continue
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected on %s: %s", market.get("conditionId"), e)
            continue
        append_prediction(output_path, row)
        n_done += 1
        side_marker = "BUY " if row["side"] == "BUY" else ("SELL" if row["side"] == "SELL" else "HOLD")
        edge_str = f"{row['edge_bps']:+.0f}bps" if row.get("edge_bps") is not None else "  n/a"
        logger.info(
            "[%d/%d] %s p_llm=%.2f mid=%s conf=%.2f edge=%s | %s",
            i + 1, len(unique_targets), side_marker, row["p_llm"],
            f"{row['mid']:.2f}" if row.get("mid") is not None else "n/a",
            row["confidence_llm"], edge_str, (row.get("question") or "")[:70],
        )
        if config.llm_predict_pause_seconds > 0:
            time.sleep(config.llm_predict_pause_seconds)

    logger.info("Done. wrote %d new predictions to %s", n_done, output_path)
    candidates = rank_candidates(output_path, min_edge_bps=args.min_edge_bps,
                                 min_confidence=args.min_confidence)
    _print_candidates(candidates)


def _print_candidates(candidates: list[dict[str, Any]]) -> None:
    print(f"\n=== {len(candidates)} candidates above edge/confidence threshold ===")
    if not candidates:
        return
    for r in candidates[:25]:
        mid_str = f"{r['mid']:.3f}" if r.get('mid') is not None else " n/a "
        liq = float(r.get('liquidity') or 0)
        end = (r.get('end_date') or '?')[:10]
        print(
            f"  {r['side']:<4} edge={r['edge_bps']:+7.0f}bps "
            f"p_llm={r['p_llm']:.3f} mid={mid_str} "
            f"conf={r['confidence_llm']:.2f} "
            f"liq=${liq:>7,.0f} end={end} "
            f"| {(r.get('question') or '')[:70]}"
        )


if __name__ == "__main__":
    main()
