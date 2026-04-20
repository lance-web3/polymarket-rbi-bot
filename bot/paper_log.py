from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_LOG_PATH = Path("data/paper_trades.jsonl")


def append_paper_log(entry: dict[str, Any], path: str | Path = DEFAULT_LOG_PATH) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
