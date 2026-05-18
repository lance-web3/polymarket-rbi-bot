"""One-shot: backfill existing predictions in `data/llm_predictions.jsonl` with
the NO token_id, so the LLMProbabilityStrategy can route SELL signals to BUY-NO
trades.

Reads each prediction row, looks up its condition_id via Gamma (`condition_ids`
filter), extracts both YES + NO clobTokenIds, and rewrites the JSONL with
`no_token_id` populated. Skips rows that already have it.

No LLM calls. Pure metadata refresh.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import requests

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.market_discovery import parse_jsonish_list
from polymarket_rbi_bot.config import BotConfig

logger = logging.getLogger(__name__)


def fetch_market(host: str, cid: str, *, timeout: int = 30) -> dict[str, Any] | None:
    for closed_flag in ("false", "true"):
        try:
            r = requests.get(
                f"{host}/markets",
                params={"condition_ids": cid, "closed": closed_flag, "limit": 5},
                timeout=timeout,
            )
        except requests.RequestException as e:
            logger.warning("gamma fetch failed for cid=%s closed=%s: %s", cid, closed_flag, e)
            continue
        if r.status_code != 200:
            continue
        for m in r.json():
            if str(m.get("conditionId") or "").strip() == cid:
                return m
    return None


def extract_no_token_id(market: dict[str, Any]) -> str | None:
    token_ids = parse_jsonish_list(market.get("clobTokenIds"))
    outcomes = parse_jsonish_list(market.get("outcomes"))
    for idx, tid in enumerate(token_ids):
        outcome = str(outcomes[idx]).strip().lower() if idx < len(outcomes) else ""
        if outcome == "no":
            return str(tid)
    # Fallback: assume index 1 is NO if labels missing
    if len(token_ids) >= 2:
        return str(token_ids[1])
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--pause-seconds", type=float, default=0.3)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    config = BotConfig.from_env()
    in_path = Path(args.input or config.llm_predictions_path)
    out_path = Path(args.output or args.input or config.llm_predictions_path)

    rows: list[dict[str, Any]] = []
    with in_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    logger.info("Loaded %d rows from %s", len(rows), in_path)

    # Cache cid → no_token_id so we don't refetch on repeated cids
    cache: dict[str, str | None] = {}
    n_backfilled = 0
    n_already = 0
    n_failed = 0
    for row in rows:
        if row.get("no_token_id"):
            n_already += 1
            continue
        cid = row.get("condition_id")
        if not cid:
            n_failed += 1
            continue
        if cid not in cache:
            market = fetch_market(config.gamma_host, cid)
            if market is None:
                cache[cid] = None
            else:
                cache[cid] = extract_no_token_id(market)
            if args.pause_seconds > 0:
                time.sleep(args.pause_seconds)
        no_tid = cache.get(cid)
        if no_tid is None:
            n_failed += 1
            continue
        row["no_token_id"] = no_tid
        n_backfilled += 1

    logger.info(
        "Backfilled=%d already_set=%d failed=%d (cache_size=%d)",
        n_backfilled, n_already, n_failed, len(cache),
    )

    if args.dry_run:
        print("--dry-run: would rewrite", out_path, "with", len(rows), "rows")
        return

    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, default=str) + "\n")
    tmp.replace(out_path)
    logger.info("Wrote %d rows to %s", len(rows), out_path)


if __name__ == "__main__":
    main()
