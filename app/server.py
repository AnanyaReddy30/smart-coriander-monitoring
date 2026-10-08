"""Flask backend for the Smart Coriander Cultivation and Quality Monitoring System.

Two ways data enters the system, through the SAME pipeline:

  * POST /api/readings         what the ESP32 will call once hardware exists
  * POST /api/replay/step|seek replays the coriander dataset as if it were live

So when the hardware arrives, only the data source changes; the rule engine,
database and dashboard stay as they are.
"""
import csv
import json
import os
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

from .engine import analyze, load_config, normalise_reading
from .store import Store

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_DB = os.path.join(ROOT, "instance", "coriander.db")
DEFAULT_DATASET = os.path.join(ROOT, "data", "coriander_sensor_dataset.csv")
DEFAULT_FARMS = os.path.join(ROOT, "data", "farms.json")

CSV_NUMERIC = ["n", "p", "k", "soil_moisture", "temperature", "ph", "ec", "light", "pressure", "wind_speed"]


def _load_dataset(path):
    by_farm = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for col in CSV_NUMERIC:
                row[col] = float(row[col])
            row["day_of_crop"] = int(row["day_of_crop"])
            by_farm.setdefault(row["farm_id"], []).append(row)
    return by_farm


def create_app(db_path=None, dataset_path=None, farms_path=None, config_path=None, fresh=False):
    app = Flask(__name__, static_folder=os.path.join(os.path.dirname(__file__), "static"), static_url_path="")
    app.json.sort_keys = False
    cfg = load_config(config_path)

    db_file = db_path or DEFAULT_DB
    if fresh and db_file != ":memory:" and os.path.exists(db_file):
        os.remove(db_file)
    store = Store(db_file)

    with open(farms_path or DEFAULT_FARMS, encoding="utf-8") as fh:
        farms = {f["id"]: f for f in json.load(fh)}
    dataset = _load_dataset(dataset_path or DEFAULT_DATASET)
    window_h = cfg["pressure_trend_window_hours"]

    # ------------------------------------------------------------- helpers
    def error(message, status=400):
        return jsonify({"error": message}), status

    def ingest(farm_id, payload):
        """Analyse one reading and store it. Returns (analysis, compact_row)."""
        farm = farms[farm_id]
        ts = payload.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dt = datetime.fromisoformat(str(ts).replace("T", " "))
        ts = dt.strftime("%Y-%m-%d %H:%M:%S")
        sowing = datetime.fromisoformat(farm["sowing_date"])
        day = max(0, (dt - sowing).days)

        reading = normalise_reading(payload)
        delta = None
        prev = store.previous_reading(farm_id, ts)
        if prev and reading.get("pressure") is not None:
            delta = (reading["pressure"] - prev["pressure"]) * (window_h / prev["gap_hours"])

        analysis = analyze(cfg, reading, day, hour=dt.hour, pressure_delta=delta)
        store.add_reading(farm_id, ts, day, reading, analysis)
        compact = {
            "ts": ts, "day": day, "stage": analysis["stage"]["id"], "led": analysis["overall"]["led"],
            "v": {k: reading.get(k) for k in reading},
            "s": {p["id"]: p["status"] for p in analysis["parameters"]},
            "sv": {p["id"]: p["severity"] for p in analysis["parameters"]},
        }
        return analysis, compact

    def replay_payload(farm_id, replace):
        rows = dataset.get(farm_id, [])
        cursor = store.get_cursor(farm_id)
        latest = store.latest(farm_id)
        return {
            "farm_id": farm_id, "cursor": cursor, "total": len(rows), "finished": cursor >= len(rows),
            "latest": latest, "replace": replace,
        }

    # -------------------------------------------------------------- routes
    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/config")
    def get_config():
        return jsonify(cfg)

    @app.get("/api/farms")
    def get_farms():
        out = []
        for fid, farm in farms.items():
            item = dict(farm)
            item["total_readings"] = len(dataset.get(fid, []))
            item["cursor"] = store.get_cursor(fid)
            out.append(item)
        return jsonify(out)

    @app.post("/api/readings")
    def post_reading():
        """Endpoint for the ESP32 (or any client). Same JSON as the dataset columns."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error("Send a JSON object with farm_id and sensor values.")
        farm_id = body.get("farm_id")
        if farm_id not in farms:
            return error("Unknown farm_id. Add it to data/farms.json first.", 404)
        try:
            analysis, compact = ingest(farm_id, body)
        except ValueError as exc:
            return error("Bad timestamp: %s" % exc)
        return jsonify({"analysis": analysis, "stored": compact}), 201

    @app.get("/api/latest")
    def latest():
        farm_id = request.args.get("farm_id")
        if farm_id not in farms:
            return error("Unknown farm_id.", 404)
        row = store.latest(farm_id)
        if not row:
            return error("No readings stored yet for this farm.", 404)
        return jsonify(row)

    @app.get("/api/history")
    def history():
        farm_id = request.args.get("farm_id")
        if farm_id not in farms:
            return error("Unknown farm_id.", 404)
        limit = request.args.get("limit", type=int)
        payload = replay_payload(farm_id, True)
        payload["rows"] = store.history(farm_id, limit)
        return jsonify(payload)

    @app.post("/api/analyze")
    def analyze_only():
        """Analyse a hand-entered reading without storing it (used by the test-case panel)."""
        body = request.get_json(silent=True) or {}
        reading = body.get("reading")
        if not isinstance(reading, dict):
            return error("Send {\"reading\": {...}, \"day\": n}.")
        try:
            day = int(body.get("day", 0))
            hour = body.get("hour")
            hour = int(hour) if hour is not None else None
            delta = body.get("pressure_delta")
            delta = float(delta) if delta is not None else None
        except (TypeError, ValueError):
            return error("day, hour and pressure_delta must be numbers.")
        return jsonify(analyze(cfg, reading, day, hour=hour, pressure_delta=delta))

    @app.post("/api/replay/step")
    def replay_step():
        body = request.get_json(silent=True) or {}
        farm_id = body.get("farm_id")
        if farm_id not in dataset:
            return error("Unknown farm_id.", 404)
        try:
            steps = max(1, min(int(body.get("steps", 1)), 400))
        except (TypeError, ValueError):
            return error("steps must be a number.")
        rows = dataset[farm_id]
        cursor = store.get_cursor(farm_id)
        added = []
        for _ in range(steps):
            if cursor >= len(rows):
                break
            row = rows[cursor]
            cursor += 1
            _, compact = ingest(farm_id, row)
            added.append(compact)
        store.set_cursor(farm_id, cursor)
        payload = replay_payload(farm_id, False)
        payload["added"] = added
        return jsonify(payload)

    @app.post("/api/replay/seek")
    def replay_seek():
        """Jump to the end of a given day: clears the farm's stored data and replays up to it."""
        body = request.get_json(silent=True) or {}
        farm_id = body.get("farm_id")
        if farm_id not in dataset:
            return error("Unknown farm_id.", 404)
        try:
            day = int(body.get("day", -1))
        except (TypeError, ValueError):
            return error("day must be a number.")
        store.clear(farm_id)
        rows = dataset[farm_id]
        cursor = 0
        added = []
        while cursor < len(rows) and rows[cursor]["day_of_crop"] <= day:
            _, compact = ingest(farm_id, rows[cursor])
            added.append(compact)
            cursor += 1
        store.set_cursor(farm_id, cursor)
        payload = replay_payload(farm_id, True)
        payload["added"] = added
        return jsonify(payload)

    @app.post("/api/replay/reset")
    def replay_reset():
        body = request.get_json(silent=True) or {}
        farm_id = body.get("farm_id")
        if farm_id not in dataset:
            return error("Unknown farm_id.", 404)
        store.clear(farm_id)
        payload = replay_payload(farm_id, True)
        payload["added"] = []
        return jsonify(payload)

    app.store = store
    app.cfg = cfg
    app.dataset = dataset
    return app
