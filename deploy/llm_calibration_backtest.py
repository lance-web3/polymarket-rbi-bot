"""Stage A — backwards-looking calibration test for the LLM probability engine.

Pulls resolved Polymarket markets (closed between user-supplied start/end dates),
runs each through the LLM probability wrapper, fetches the market's own price 7
days before resolution as a baseline, and computes per-niche Brier scores:

  Brier(LLM)            — model probability vs realized outcome
  Brier(market_p_t7)    — Polymarket's own price at T-7d vs realized outcome
  Brier(base_rate)      — empirical base rate of YES on the sample
  Brier(always_0.5)     — uninformed prior

The plan's A → B decision gate fires when at least one niche has ≥20 markets,
Brier(LLM) ≤ Brier(market_p_t7) − 0.005, and Brier(LLM) < 0.22.

This script is resumable: it appends to the calibration JSONL and skips condition
IDs already present on re-run.

Usage:
    python -m deploy.llm_calibration_backtest \
        --start 2026-02-01 --end 2026-05-10 \
        --min-liquidity 1000 --max-markets 200

    # later, get the per-niche table:
    python -m deploy.llm_calibration_backtest --summary-only
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.market_discovery import (
    GammaMarketDiscoveryClient,
    extract_yes_token,
    parse_jsonish_list,
)
from data.polymarket_client import PolymarketHistoryClient
from polymarket_rbi_bot.calibration import (
    brier_score,
    calibration_curve,
    reference_brier_baselines,
)
from polymarket_rbi_bot.config import BotConfig
from polymarket_rbi_bot.llm_client import LLMError, LLMProbabilityClient

logger = logging.getLogger(__name__)


# --- niche classification heuristic ---------------------------------------------

NICHES = [
    "politics_election",
    "regulatory_policy",
    "sports_outright",
    "crypto",
    "awards_entertainment",
    "corporate_event",
    "breaking_news",
    "scheduled_event",
    "other",
]


def classify_niche_heuristic(question: str, description: str, tags: list[str] | None) -> str:
    """Cheap pre-LLM niche guess. The LLM will also self-classify; we'll use both."""
    text = " ".join(filter(None, [question or "", description or "", " ".join(tags or [])])).lower()
    if any(k in text for k in (
        "presidential", "election", "primary", "senate", "house seat",
        "governor", "prime minister", "chancellor",
    )):
        return "politics_election"
    if any(k in text for k in (
        "fed cut", "fomc", "interest rate", "rate hike", "cpi", "tariff",
        "sec ", "fda ", "ban ", "regulation", "approve ", "approval",
    )):
        return "regulatory_policy"
    if any(k in text for k in (
        "nba finals", "stanley cup", "super bowl", "premier league",
        "champions league", "world cup", "win the", "tournament",
    )):
        return "sports_outright"
    if any(k in text for k in (
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol ",
        "crypto", "token", "dao ", "airdrop",
    )):
        return "crypto"
    if any(k in text for k in (
        "oscar", "grammy", "golden globe", "best picture", "best actor",
        "tony", "emmy", "song of the year",
    )):
        return "awards_entertainment"
    if any(k in text for k in (
        "earnings", "ipo", "merger", "acquisition", "guidance",
        "stock split", "ceo ", "ceo resign",
    )):
        return "corporate_event"
    if any(k in text for k in (
        "ceasefire", "attack", "indicted", "convicted", "sentenced", "killed",
        "assassination", "earthquake", "hurricane",
    )):
        return "breaking_news"
    if any(k in text for k in ("by ", "before ", "on or before", "scheduled")):
        return "scheduled_event"
    return "other"


# --- market loading -------------------------------------------------------------

def _parse_iso_date(s: str) -> datetime | None:
    if not s:
        return None
    s = str(s).strip()
    # Polymarket's closedTime field uses formats like:
    #   "2026-04-12 16:31:01+00"      (space, 2-digit tz)
    #   "2026-04-12T16:31:01.123Z"    (T, fractional, Z)
    #   "2026-04-12T16:31:01Z"
    # Python's fromisoformat is strict; normalize first.
    s = s.replace(" ", "T")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Handle "+00" (no minutes) by appending ":00"
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit() and ":" not in s[-3:]:
        s = s + ":00"
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:  # noqa: BLE001
        return None


_TRANSIENT_QUESTION_PATTERNS = (
    # intraday crypto / price-tick markets
    " up or down -", "above $", "above 7", "above 8", "above 9", "above 10",
    "dip to $", "dip to 7", "dip to 8", "dip to 9",
    # esports per-game / per-map / BO3
    "counter-strike:", "dota 2:", "league of legends:", "valorant:",
    "rocket league:", "starcraft", " bo3) ", " bo5) ", " - game ", " - map ",
    "any player penta kill", "kills over/under", "set 1 games o/u",
    "match o/u", "completed match:", " over/under ", "game handicap:",
    # individual sports matches / props
    "halftime?", "draw at halftime", "doubles):", "doubles)",
    # weather
    "will the highest temperature", "will the lowest temperature",
    # FDV / launch micro-markets
    " fdv above $", "one day after launch?",
)


def _is_transient_micro_market(question: str) -> bool:
    q = (question or "").lower()
    return any(pat in q for pat in _TRANSIENT_QUESTION_PATTERNS)


def fetch_closed_markets(
    config: BotConfig,
    *,
    start: datetime,
    end: datetime,
    page_size: int = 100,  # Gamma caps at 100/page
    max_pages: int = 50,
    min_volume: float = 1000.0,
    min_horizon_days: float = 7.0,
) -> list[dict[str, Any]]:
    """Pull closed markets with closedTime in [start, end], lifetime volume ≥ threshold,
    and market horizon (closedTime - startDate) ≥ min_horizon_days.

    Filters out transient micro-markets (5-min "Up or Down", per-game player props,
    single-day sports matches, weather forecasts, intraday crypto props).
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    import requests as _req
    for page in range(max_pages):
        params = {
            "limit": page_size,
            "closed": "true",
            "archived": "false",
            "order": "closedTime",
            "ascending": "false",
            "offset": page * page_size,
            "closed_time_min": start.strftime("%Y-%m-%d"),
            "closed_time_max": end.strftime("%Y-%m-%d"),
        }
        resp = _req.get(f"{config.gamma_host}/markets", params=params, timeout=30)
        resp.raise_for_status()
        raw = resp.json()
        page_markets = raw if isinstance(raw, list) else raw.get("data", [])
        if not page_markets:
            break
        added_this_page = 0
        transient_skipped = 0
        volume_skipped = 0
        horizon_skipped = 0
        for m in page_markets:
            cid = str(m.get("conditionId") or m.get("condition_id") or "").strip()
            if not cid or cid in seen:
                continue
            seen.add(cid)
            question = m.get("question") or ""
            if _is_transient_micro_market(question):
                transient_skipped += 1
                continue
            try:
                vol = float(m.get("volumeNum") or m.get("volume") or 0.0)
            except (TypeError, ValueError):
                vol = 0.0
            if vol < min_volume:
                volume_skipped += 1
                continue
            ct_dt = _parse_iso_date(m.get("closedTime") or "")
            if ct_dt is None:
                end_iso = m.get("endDate") or ""
                if end_iso.startswith("2028-01-01"):
                    continue
                ct_dt = _parse_iso_date(end_iso) if end_iso else None
            if ct_dt is None or not (start <= ct_dt <= end):
                continue
            # horizon filter
            sd_dt = _parse_iso_date(m.get("startDate") or m.get("createdAt") or "")
            if sd_dt is not None:
                horizon_days = (ct_dt - sd_dt).total_seconds() / 86400.0
                if horizon_days < min_horizon_days:
                    horizon_skipped += 1
                    continue
            out.append(m)
            added_this_page += 1
        logger.info(
            "page=%d fetched=%d added=%d (transient=%d vol=%d horizon=%d) total=%d",
            page, len(page_markets), added_this_page, transient_skipped, volume_skipped, horizon_skipped, len(out),
        )
        if len(page_markets) < page_size:
            break
    return out


# --- per-market processing ------------------------------------------------------

def extract_resolved_outcome(market: dict[str, Any], yes_token: dict[str, Any]) -> int | None:
    """Return 1 if YES resolved true, 0 if YES resolved false, None if unresolved/ambiguous."""
    outcome_prices = parse_jsonish_list(market.get("outcomePrices"))
    outcomes = parse_jsonish_list(market.get("outcomes"))
    if len(outcome_prices) < 2 or len(outcomes) < 2:
        return None
    yes_idx = None
    for i, o in enumerate(outcomes):
        if str(o).strip().lower() == "yes":
            yes_idx = i
            break
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
    return None  # ambiguous / refunded


def fetch_market_p_t7(
    history_client: PolymarketHistoryClient,
    token_id: str,
    resolution_dt: datetime,
    *,
    lookback_days_start: int = 14,
    lookback_days_end: int = 6,
    fidelity_minutes: int = 60,
) -> float | None:
    start = resolution_dt - timedelta(days=lookback_days_start)
    end = resolution_dt - timedelta(days=lookback_days_end)
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    try:
        history = history_client.fetch_price_history(
            token_id=token_id,
            start_ts=start_ts,
            end_ts=end_ts,
            fidelity=fidelity_minutes,
            include_market_metadata=False,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("history fetch failed for token=%s: %s", token_id, e)
        return None
    if not history:
        return None
    closes = [float(row["close"]) for row in history if "close" in row]
    if not closes:
        return None
    return statistics.median(closes)


# --- I/O ------------------------------------------------------------------------

def load_existing_rows(path: Path) -> dict[str, dict[str, Any]]:
    by_cid: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return by_cid
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            cid = str(row.get("condition_id") or "")
            if cid:
                by_cid[cid] = row
    return by_cid


def append_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")


# --- summary --------------------------------------------------------------------

def summarize(
    rows: list[dict[str, Any]],
    output_path: Path,
    *,
    min_n_for_niche: int = 5,
    gate_min_n: int = 20,
    gate_brier_improvement: float = 0.005,
    gate_brier_absolute: float = 0.22,
) -> dict[str, Any]:
    by_niche_llm: dict[str, list[tuple[float, int]]] = defaultdict(list)
    by_niche_market: dict[str, list[tuple[float, int]]] = defaultdict(list)
    confidence_by_niche: dict[str, list[float]] = defaultdict(list)
    rows_used = []
    for r in rows:
        if r.get("status") != "ok":
            continue
        p_llm = r.get("p_llm")
        p_mkt = r.get("market_p_t7")
        y = r.get("resolved_outcome")
        if p_llm is None or p_mkt is None or y is None:
            continue
        niche = r.get("niche_llm") or r.get("niche_heuristic") or "other"
        by_niche_llm[niche].append((float(p_llm), int(y)))
        by_niche_market[niche].append((float(p_mkt), int(y)))
        if r.get("confidence_llm") is not None:
            confidence_by_niche[niche].append(float(r["confidence_llm"]))
        rows_used.append((float(p_llm), float(p_mkt), int(y), niche))

    summary: dict[str, Any] = {"per_niche": {}, "overall": {}, "gate": {}}
    for niche in sorted(by_niche_llm):
        pairs_llm = by_niche_llm[niche]
        pairs_mkt = by_niche_market[niche]
        n = len(pairs_llm)
        if n < min_n_for_niche:
            summary["per_niche"][niche] = {"n": n, "skipped": "below min_n"}
            continue
        llm_preds = [p for p, _ in pairs_llm]
        mkt_preds = [p for p, _ in pairs_mkt]
        outcomes = [y for _, y in pairs_llm]
        ref = reference_brier_baselines(outcomes)
        brier_llm = brier_score(llm_preds, outcomes)
        brier_mkt = brier_score(mkt_preds, outcomes)
        summary["per_niche"][niche] = {
            "n": n,
            "brier_llm": round(brier_llm, 4),
            "brier_market_p_t7": round(brier_mkt, 4),
            "brier_base_rate": round(ref["always_base_rate"], 4),
            "brier_always_p_half": round(ref["always_p_zero_dot_five"], 4),
            "base_rate_yes": round(ref["base_rate"], 4),
            "mean_confidence_llm": round(statistics.fmean(confidence_by_niche[niche]), 3) if confidence_by_niche[niche] else None,
            "llm_minus_market_brier": round(brier_llm - brier_mkt, 4),
            "calibration_curve_llm": calibration_curve(llm_preds, outcomes, bins=5),
        }

    # overall
    if rows_used:
        all_llm = [t[0] for t in rows_used]
        all_mkt = [t[1] for t in rows_used]
        all_y = [t[2] for t in rows_used]
        ref = reference_brier_baselines(all_y)
        summary["overall"] = {
            "n": len(rows_used),
            "brier_llm": round(brier_score(all_llm, all_y), 4),
            "brier_market_p_t7": round(brier_score(all_mkt, all_y), 4),
            "brier_base_rate": round(ref["always_base_rate"], 4),
            "brier_always_p_half": round(ref["always_p_zero_dot_five"], 4),
            "base_rate_yes": round(ref["base_rate"], 4),
            "niche_counts": dict(Counter(t[3] for t in rows_used)),
        }

    # A -> B gate
    passing = []
    for niche, stats in summary["per_niche"].items():
        if not isinstance(stats, dict) or "brier_llm" not in stats:
            continue
        if (
            stats["n"] >= gate_min_n
            and stats["brier_llm"] <= stats["brier_market_p_t7"] - gate_brier_improvement
            and stats["brier_llm"] < gate_brier_absolute
        ):
            passing.append(niche)
    summary["gate"] = {
        "gate_min_n": gate_min_n,
        "gate_brier_improvement": gate_brier_improvement,
        "gate_brier_absolute": gate_brier_absolute,
        "passing_niches": passing,
        "verdict": "GO" if passing else "NO_GO",
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2) + "\n")
    return summary


# --- main -----------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=str, default="2026-02-01", help="endDate window start (inclusive, UTC).")
    parser.add_argument("--end", type=str, default="2026-05-10", help="endDate window end (inclusive, UTC).")
    parser.add_argument("--min-volume", "--min-liquidity", dest="min_volume", type=float, default=1000.0,
                        help="Lifetime $ volume filter (Polymarket liquidity is null on resolved markets).")
    parser.add_argument("--max-markets", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=100, help="Gamma /markets caps at 100/page.")
    parser.add_argument("--output", type=str, default=None, help="JSONL output path (default from config).")
    parser.add_argument("--summary-output", type=str, default=None)
    parser.add_argument("--summary-only", action="store_true", help="Recompute summary from existing JSONL without making any new LLM calls.")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default=None, help="Override LLM_PROVIDER for this run.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch markets and write resolved-outcome+market_p_t7 rows, but skip LLM calls.")
    parser.add_argument("--limit-llm", type=int, default=None, help="Cap LLM calls in this invocation (cost guard).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config = BotConfig.from_env()
    if args.provider:
        config.llm_provider = args.provider
    output_path = Path(args.output or config.llm_calibration_output_path)
    summary_path = Path(args.summary_output or config.llm_calibration_summary_path)

    if args.summary_only:
        rows = list(load_existing_rows(output_path).values())
        summary = summarize(rows, summary_path)
        print(json.dumps(summary, indent=2))
        return

    start_dt = _parse_iso_date(args.start + "T00:00:00Z")
    end_dt = _parse_iso_date(args.end + "T23:59:59Z")
    if not (start_dt and end_dt):
        raise SystemExit("Invalid --start or --end date.")

    existing = load_existing_rows(output_path)
    logger.info("Resuming with %d existing rows", len(existing))

    logger.info("Fetching closed markets closedTime ∈ [%s, %s], min_volume=%s ...", start_dt, end_dt, args.min_volume)
    candidates = fetch_closed_markets(
        config,
        start=start_dt,
        end=end_dt,
        page_size=args.page_size,
        max_pages=args.max_pages,
        min_volume=args.min_volume,
    )
    logger.info("Fetched %d candidate closed markets in window", len(candidates))

    if args.max_markets:
        candidates = candidates[: args.max_markets]
        logger.info("Capped to first %d candidates", len(candidates))

    history_client = PolymarketHistoryClient(host=config.host)
    llm_client = None if args.dry_run else LLMProbabilityClient(config)

    llm_calls_made = 0
    new_rows = 0
    skipped_no_yes = 0
    skipped_no_outcome = 0
    skipped_no_history = 0
    for i, market in enumerate(candidates):
        cid = str(market.get("conditionId") or market.get("condition_id") or "")
        if not cid:
            continue
        if cid in existing:
            continue

        yes_token = extract_yes_token(market)
        if not yes_token or not yes_token.get("token_id"):
            skipped_no_yes += 1
            continue
        outcome = extract_resolved_outcome(market, yes_token)
        if outcome is None:
            skipped_no_outcome += 1
            continue
        # Use closedTime (real resolution timestamp) for the T-7d lookup.
        # Fall back to endDate if closedTime missing AND endDate is not the 2028 sentinel.
        ct_raw = market.get("closedTime")
        end_iso = market.get("endDate") or ""
        resolution_dt = None
        if ct_raw:
            resolution_dt = _parse_iso_date(str(ct_raw).replace(" ", "T"))
        if resolution_dt is None and end_iso and not end_iso.startswith("2028-01-01"):
            resolution_dt = _parse_iso_date(end_iso)
        if resolution_dt is None:
            continue

        market_p_t7 = fetch_market_p_t7(history_client, yes_token["token_id"], resolution_dt)
        if market_p_t7 is None:
            skipped_no_history += 1
            # still record the row so the summary can show skip-counts
            row = {
                "condition_id": cid,
                "status": "no_history",
                "question": market.get("question"),
                "end_date": end_iso,
                "resolved_outcome": outcome,
            }
            append_row(output_path, row)
            existing[cid] = row
            continue

        question = market.get("question") or ""
        description = market.get("description") or ""
        tags_list = parse_jsonish_list(market.get("tags"))
        tags = [str(t.get("label") if isinstance(t, dict) else t) for t in tags_list]
        niche_heuristic = classify_niche_heuristic(question, description, tags)

        row: dict[str, Any] = {
            "condition_id": cid,
            "token_id": yes_token["token_id"],
            "question": question,
            "description_preview": (description or "")[:300],
            "end_date": end_iso,
            "closed_time": str(ct_raw) if ct_raw else None,
            "resolution_dt": resolution_dt.isoformat(),
            "volume": market.get("volumeNum") or market.get("volume"),
            "category": market.get("category"),
            "tags": tags,
            "niche_heuristic": niche_heuristic,
            "resolved_outcome": outcome,
            "market_p_t7": market_p_t7,
        }

        if args.dry_run:
            row["status"] = "dry_run"
            append_row(output_path, row)
            existing[cid] = row
            new_rows += 1
            continue

        if args.limit_llm is not None and llm_calls_made >= args.limit_llm:
            logger.info("Reached --limit-llm=%d, stopping", args.limit_llm)
            break

        try:
            pred = llm_client.predict(
                question=question,
                description=description,
                niche_hint=niche_heuristic,
                condition_id=cid,
            )
        except LLMError as e:
            logger.error("LLM error for cid=%s: %s", cid, e)
            row["status"] = "llm_error"
            row["error"] = str(e)
            append_row(output_path, row)
            existing[cid] = row
            continue
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected LLM error for cid=%s", cid)
            row["status"] = "llm_error"
            row["error"] = str(e)
            append_row(output_path, row)
            existing[cid] = row
            continue

        llm_calls_made += 1
        row.update(
            {
                "status": "ok",
                "p_llm": pred["p_yes"],
                "confidence_llm": pred["confidence"],
                "niche_llm": pred["niche_classification"],
                "evidence_llm": pred.get("top_evidence"),
                "what_would_flip": pred.get("what_would_flip"),
                "reasoning_llm": (pred.get("reasoning") or "")[:600],
                "_meta": pred.get("_meta"),
            }
        )
        append_row(output_path, row)
        existing[cid] = row
        new_rows += 1
        logger.info(
            "[%d/%d] %s | %s | p_llm=%.3f conf=%.2f p_t7=%.3f y=%d",
            i + 1, len(candidates), niche_heuristic, (question or "")[:80],
            row["p_llm"], row["confidence_llm"], market_p_t7, outcome,
        )
        if config.llm_predict_pause_seconds > 0:
            time.sleep(config.llm_predict_pause_seconds)

    logger.info(
        "Done. new_rows=%d llm_calls=%d skipped_no_yes=%d skipped_no_outcome=%d skipped_no_history=%d",
        new_rows, llm_calls_made, skipped_no_yes, skipped_no_outcome, skipped_no_history,
    )

    rows = list(load_existing_rows(output_path).values())
    summary = summarize(rows, summary_path)
    print(json.dumps(summary["gate"], indent=2))
    print(f"\nWrote {len(rows)} rows to {output_path}")
    print(f"Wrote summary to {summary_path}")
    print("Per-niche table:")
    for niche, stats in summary["per_niche"].items():
        if isinstance(stats, dict) and "brier_llm" in stats:
            print(
                f"  {niche:>25s}  n={stats['n']:>4d}  "
                f"brier_llm={stats['brier_llm']:.4f}  brier_mkt={stats['brier_market_p_t7']:.4f}  "
                f"Δ={stats['llm_minus_market_brier']:+.4f}"
            )


if __name__ == "__main__":
    main()
