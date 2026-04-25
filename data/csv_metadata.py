"""Read market metadata from quote-backtest CSVs.

The newer quote-backtest CSVs already carry `question`, `market_slug`,
`market_family`, `liquidity`, `endDate`, `createdAt`, `condition_id`,
`token_id` per row (see deploy/build_quote_snapshot_csv.py). For the
backtest engine's family-filter check we only need a market-shaped dict
once per run, so we read the first row and adapt the keys to match what
Gamma's `/markets` endpoint returns (which is what `MarketFilter` expects).

If the CSV doesn't carry the metadata columns, returns None and the caller
should treat the run as "no family info" — engine will fall through.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any


def extract_market_metadata(csv_path: Path) -> dict[str, Any] | None:
    if not csv_path.exists():
        return None
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        first = next(reader, None)
    if not first:
        return None
    question = (first.get("question") or "").strip()
    if not question:
        return None
    return {
        "question": question,
        "slug": (first.get("market_slug") or "").strip() or None,
        "conditionId": (first.get("condition_id") or "").strip() or None,
        "clobTokenIds": [first.get("token_id")] if first.get("token_id") else [],
        "outcomes": [first.get("outcome")] if first.get("outcome") else [],
        "liquidity": _safe_float(first.get("liquidity")),
        "endDate": (first.get("endDate") or "").strip() or None,
        "createdAt": (first.get("createdAt") or "").strip() or None,
        "market_family": (first.get("market_family") or "").strip() or None,
    }


def _safe_float(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
