"""Raw estimates in SQLite. Segments and reports are always recomputed from them, so thresholds
can be changed after the fact."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .head import HeadEstimate, Side
from .thermometer import Reading



class Storage:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # The run loop, the bot and the thermometers each write through their own connection;
        # one that finds the database busy waits for it instead of failing.
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # In WAL mode this is still safe against a crash of the program; it saves a disk flush on
        # every commit, and the run loop commits every frame.
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            " ts REAL NOT NULL, side TEXT NOT NULL, score REAL, method TEXT)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts)")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(samples)")}
        if "motion" not in columns:  # databases from before sleep tracking
            try:
                self.conn.execute("ALTER TABLE samples ADD COLUMN motion REAL")
            except sqlite3.OperationalError:  # another connection added it a moment ago
                pass
        # Sleep outside the crib, entered by the parents in Telegram.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS naps ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, start REAL NOT NULL, end REAL NOT NULL)"
        )
        # A nap still going on ("fell asleep" pressed, "woke up" not yet) is open; its end is its
        # start until it is closed.
        if "open" not in {row[1] for row in self.conn.execute("PRAGMA table_info(naps)")}:
            try:
                self.conn.execute("ALTER TABLE naps ADD COLUMN open INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:  # another connection added it a moment ago
                pass
        # Thermometer readings, by the address the thermometer has on this computer.
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS climate ("
            " ts REAL NOT NULL, address TEXT NOT NULL, temperature REAL, humidity REAL, battery REAL)"
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS climate_ts ON climate(address, ts)")

    def add(self, ts: datetime, estimate: HeadEstimate, motion: float | None = None) -> None:
        # Committed at once: an open write transaction would make the bot and the thermometers
        # wait, up to 30 s and then fail, whenever the camera stalls between frames.
        self.conn.execute(
            "INSERT INTO samples (ts, side, score, method, motion) VALUES (?, ?, ?, ?, ?)",
            (ts.timestamp(), estimate.side.value, estimate.score, estimate.code, motion),
        )
        self.conn.commit()

    def flush(self) -> None:
        self.conn.commit()

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

    def add_nap(self, start: datetime, end: datetime) -> int:
        """The id of the new nap, or of the same one already there: a button pressed twice, or a
        press that came again, must not log the sleep twice."""
        same = self.conn.execute(
            "SELECT id FROM naps WHERE start = ? AND end = ? AND open = 0", (start.timestamp(), end.timestamp())
        ).fetchone()
        if same is not None:
            return int(same[0])
        cursor = self.conn.execute(
            "INSERT INTO naps (start, end) VALUES (?, ?)", (start.timestamp(), end.timestamp())
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def open_nap(self, start: datetime) -> tuple[int, bool]:
        """(id, True) of a new open nap, or (id, False) of the one already open: there is one
        baby, so there is one sleep going on at most."""
        row = self.conn.execute("SELECT id FROM naps WHERE open = 1").fetchone()
        if row is not None:
            return int(row[0]), False
        cursor = self.conn.execute(
            "INSERT INTO naps (start, end, open) VALUES (?, ?, 1)", (start.timestamp(), start.timestamp())
        )
        self.conn.commit()
        return int(cursor.lastrowid), True

    def close_nap(self, nap_id: int, end: datetime) -> None:
        self.conn.execute("UPDATE naps SET end = ?, open = 0 WHERE id = ?", (end.timestamp(), nap_id))
        self.conn.commit()

    def update_nap(self, nap_id: int, start: datetime, end: datetime) -> None:
        self.conn.execute(
            "UPDATE naps SET start = ?, end = ? WHERE id = ?", (start.timestamp(), end.timestamp(), nap_id)
        )
        self.conn.commit()

    def delete_nap(self, nap_id: int) -> None:
        self.conn.execute("DELETE FROM naps WHERE id = ?", (nap_id,))
        self.conn.commit()

    @staticmethod
    def _nap_row(nap_id: int, start: float, end: float, is_open: int) -> tuple[int, datetime, datetime | None]:
        return nap_id, datetime.fromtimestamp(start), None if is_open else datetime.fromtimestamp(end)

    def nap(self, nap_id: int) -> tuple[int, datetime, datetime | None] | None:
        """(id, start, end); end is None while the nap is open."""
        row = self.conn.execute("SELECT id, start, end, open FROM naps WHERE id = ?", (nap_id,)).fetchone()
        return None if row is None else self._nap_row(*row)

    def current_nap(self) -> tuple[int, datetime, None] | None:
        """The open nap, if there is one."""
        row = self.conn.execute("SELECT id, start, end, open FROM naps WHERE open = 1").fetchone()
        return None if row is None else self._nap_row(*row)

    def naps(
        self, since: datetime | None = None, until: datetime | None = None, limit: int | None = None
    ) -> list[tuple[int, datetime, datetime | None]]:
        """(id, start, end) of the naps that overlap the period, latest first, an open one (end
        None) before all."""
        query = ("SELECT id, start, end, open FROM naps WHERE (end > ? OR open = 1) AND start < ?"
                 " ORDER BY open DESC, end DESC")
        lo = since.timestamp() if since else 0.0
        hi = until.timestamp() if until else float("inf")
        if limit is not None:
            query += f" LIMIT {int(limit)}"
        return [self._nap_row(*row) for row in self.conn.execute(query, (lo, hi))]

    def add_climate(self, address: str, reading: Reading) -> None:
        self.conn.execute(
            "INSERT INTO climate (ts, address, temperature, humidity, battery) VALUES (?, ?, ?, ?, ?)",
            (reading.time.timestamp(), address, reading.temperature, reading.humidity, reading.battery),
        )
        self.flush()

    def latest_climate(self) -> dict[str, Reading]:
        """The last reading of each thermometer."""
        query = (
            "SELECT address, ts, temperature, humidity, battery FROM climate"
            " WHERE (address, ts) IN (SELECT address, MAX(ts) FROM climate GROUP BY address)"
        )
        return {
            address: Reading(datetime.fromtimestamp(ts), temperature, None if humidity is None else int(humidity), battery)
            for address, ts, temperature, humidity, battery in self.conn.execute(query)
        }

    def close(self) -> None:
        self.flush()
        self.conn.close()
