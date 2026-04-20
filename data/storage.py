from __future__ import annotations

import csv
import sqlite3
from pathlib import Path


def save_rows_to_csv(path: str | Path, rows: list[dict[str, object]]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError("No rows to save")

    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def save_rows_to_sqlite(db_path: str | Path, table: str, rows: list[dict[str, object]]) -> None:
    destination = Path(db_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        raise ValueError("No rows to save")

    columns = list(rows[0].keys())
    column_defs = ", ".join(f"{column} TEXT" for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    quoted_columns = ", ".join(columns)

    with sqlite3.connect(destination) as connection:
        connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({column_defs})")
        connection.executemany(
            f"INSERT INTO {table} ({quoted_columns}) VALUES ({placeholders})",
            [[str(row.get(column, "")) for column in columns] for row in rows],
        )
        connection.commit()
