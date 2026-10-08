"""SQLite storage for timestamped sensor readings (Stage 8: historical logging).

The table layout is plain enough to move to MySQL or Supabase later: one row per
reading with the sensor values, the stage, the LED colour and the full analysis
stored as JSON.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime

VALUE_COLUMNS = ["n", "p", "k", "moisture", "temperature", "ph", "ec", "light", "pressure", "wind"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    farm_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    day INTEGER NOT NULL,
    stage TEXT NOT NULL,
    n REAL, p REAL, k REAL, moisture REAL, temperature REAL,
    ph REAL, ec REAL, light REAL, pressure REAL, wind REAL,
    led TEXT NOT NULL,
    statuses TEXT NOT NULL,
    analysis TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_readings_farm_ts ON readings (farm_id, ts);
CREATE TABLE IF NOT EXISTS replay_state (
    farm_id TEXT PRIMARY KEY,
    cursor INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------ write
    def add_reading(self, farm_id, ts, day, reading, analysis):
        statuses = {
            "s": {p["id"]: p["status"] for p in analysis["parameters"]},
            "sv": {p["id"]: p["severity"] for p in analysis["parameters"]},
        }
        values = [reading.get(c) for c in VALUE_COLUMNS]
        with self._lock:
            self._db.execute(
                "INSERT INTO readings (farm_id, ts, day, stage, %s, led, statuses, analysis) "
                "VALUES (?, ?, ?, ?, %s, ?, ?, ?)" % (", ".join(VALUE_COLUMNS), ", ".join("?" * len(VALUE_COLUMNS))),
                [farm_id, ts, day, analysis["stage"]["id"]] + values
                + [analysis["overall"]["led"], json.dumps(statuses), json.dumps(analysis)],
            )
            self._db.commit()

    def clear(self, farm_id):
        with self._lock:
            self._db.execute("DELETE FROM readings WHERE farm_id = ?", (farm_id,))
            self._db.execute("DELETE FROM replay_state WHERE farm_id = ?", (farm_id,))
            self._db.commit()

    def set_cursor(self, farm_id, cursor):
        with self._lock:
            self._db.execute(
                "INSERT INTO replay_state (farm_id, cursor) VALUES (?, ?) "
                "ON CONFLICT(farm_id) DO UPDATE SET cursor = excluded.cursor", (farm_id, cursor))
            self._db.commit()

    # ------------------------------------------------------------------- read
    def get_cursor(self, farm_id):
        with self._lock:
            row = self._db.execute("SELECT cursor FROM replay_state WHERE farm_id = ?", (farm_id,)).fetchone()
        return row["cursor"] if row else 0

    def previous_reading(self, farm_id, ts, max_gap_hours=6):
        """Latest stored reading before `ts`, if it is at most `max_gap_hours` earlier."""
        with self._lock:
            row = self._db.execute(
                "SELECT ts, pressure FROM readings WHERE farm_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1",
                (farm_id, ts)).fetchone()
        if not row or row["pressure"] is None:
            return None
        gap = (datetime.fromisoformat(ts) - datetime.fromisoformat(row["ts"])).total_seconds() / 3600.0
        if gap <= 0 or gap > max_gap_hours:
            return None
        return {"pressure": row["pressure"], "gap_hours": gap}

    def count(self, farm_id):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) AS c FROM readings WHERE farm_id = ?", (farm_id,)).fetchone()["c"]

    def latest(self, farm_id):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM readings WHERE farm_id = ? ORDER BY ts DESC LIMIT 1", (farm_id,)).fetchone()
        return self._full(row) if row else None

    def history(self, farm_id, limit=None):
        sql = "SELECT * FROM readings WHERE farm_id = ? ORDER BY ts ASC"
        params = [farm_id]
        if limit:
            sql = ("SELECT * FROM (SELECT * FROM readings WHERE farm_id = ? ORDER BY ts DESC LIMIT ?) "
                   "ORDER BY ts ASC")
            params.append(int(limit))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [self.compact(r) for r in rows]

    # --------------------------------------------------------------- shaping
    @staticmethod
    def compact(row):
        out = {
            "ts": row["ts"], "day": row["day"], "stage": row["stage"], "led": row["led"],
            "v": {c: row[c] for c in VALUE_COLUMNS},
        }
        st = json.loads(row["statuses"])
        out["s"], out["sv"] = st["s"], st["sv"]
        return out

    @staticmethod
    def _full(row):
        out = Store.compact(row)
        out["analysis"] = json.loads(row["analysis"])
        return out
