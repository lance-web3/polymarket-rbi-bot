from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export top-ranked fillability markets as a watchlist JSON.")
    parser.add_argument("--shortlist", default="data/fillability_shortlist.json")
    parser.add_argument("--out", default="data/fillability_watchlist.json")
    parser.add_argument("--top", type=int, default=4)
    parser.add_argument("--min-score", type=float, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    payload = json.loads(Path(args.shortlist).read_text())
    rows = payload.get("ranked") or []
    watchlist = []
    for row in rows:
        score = row.get("fillability_score")
        if args.min_score is not None and (score is None or float(score) < args.min_score):
            continue
        watchlist.append(
            {
                "token_id": row.get("token_id"),
                "question": row.get("question"),
                "outcome": row.get("outcome"),
                "fillability_score": row.get("fillability_score"),
                "rank": row.get("rank"),
                "source": "fillability_shortlist",
            }
        )
        if len(watchlist) >= args.top:
            break
    Path(args.out).write_text(json.dumps(watchlist, indent=2))
    print(json.dumps({"saved": args.out, "count": len(watchlist), "watchlist": watchlist}, indent=2))


if __name__ == "__main__":
    main()
