"""Raw estimates in SQLite. Segments and reports are always recomputed from them, so thresholds
can be changed after the fact."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .head import HeadEstimate, Side

COMMIT_EVERY = 10


class Storage:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            " ts REAL NOT NULL, side TEXT NOT NULL, score REAL, method TEXT)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts)")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(samples)")}
        if "motion" not in columns:  # databases from before sleep tracking
            self.conn.execute("ALTER TABLE samples ADD COLUMN motion REAL")
        self._pending = 0

    def add(self, ts: datetime, estimate: HeadEstimate, motion: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO samples (ts, side, score, method, motion) VALUES (?, ?, ?, ?, ?)",
            (ts.timestamp(), estimate.side.value, estimate.score, estimate.code, motion),
        )
        self._pending += 1
        if self._pending >= COMMIT_EVERY:
            self.flush()

    def flush(self) -> None:
        self.conn.commit()
        self._pending = 0

    def samples(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> list[tuple[datetime, Side, float | None]]:
        query = "SELECT ts, side, score FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts"
        lo = since.timestamp() if since else 0.0
        hi = until.timestamp() if until else float("inf")
        return [
            (datetime.fromtimestamp(ts), Side(side), score)
            for ts, side, score in self.conn.execute(query, (lo, hi))
        ]

    def activity(
        self, since: datetime | None = None, until: datetime | None = None
    ) -> list[tuple[datetime, Side, float | None]]:
        """(time, side, motion) for sleep tracking. motion is None where it was not measured."""
        query = "SELECT ts, side, motion FROM samples WHERE ts >= ? AND ts < ? ORDER BY ts"
        lo = since.timestamp() if since else 0.0
        hi = until.timestamp() if until else float("inf")
        return [
            (datetime.fromtimestamp(ts), Side(side), motion)
            for ts, side, motion in self.conn.execute(query, (lo, hi))
        ]

    def close(self) -> None:
        self.flush()
        self.conn.close()
