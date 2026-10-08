"""Checks that the dataset, the engine and the API work together.

The dataset checks compare the engine's output with the events injected by the
generator. Both were written by the same team, so this proves the pipeline is
consistent end to end; it is not evidence about accuracy on real sensor data.
"""
import csv
import json
import os
import sys
import unittest

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from app.engine import analyze, load_config  # noqa: E402
from app.server import create_app  # noqa: E402

CFG = load_config()
DATASET = os.path.join(ROOT, "data", "coriander_sensor_dataset.csv")
FARMS = os.path.join(ROOT, "data", "farms.json")

EVENT_EXPECTATION = {          # injected event -> (parameter, expected status)
    "n_deficiency": ("n", "low"), "p_deficiency": ("p", "low"), "k_deficiency": ("k", "low"),
    "n_excess": ("n", "high"), "ec_high": ("ec", "high"), "ph_high": ("ph", "high"),
    "heat_stress": ("temperature", "high"),
}


def analysed_rows():
    """Yield (farm_id, row, analysis) exactly as the server would compute them."""
    prev = {}
    with open(DATASET, newline="") as fh:
        for row in csv.DictReader(fh):
            farm = row["farm_id"]
            hour = int(row["timestamp"][11:13])
            delta = float(row["pressure"]) - float(prev[farm]["pressure"]) if farm in prev else None
            prev[farm] = row
            yield farm, row, analyze(CFG, row, int(row["day_of_crop"]), hour=hour, pressure_delta=delta)


def status(a, pid):
    return next(p["status"] for p in a["parameters"] if p["id"] == pid)


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.items = list(analysed_rows())
        with open(FARMS) as fh:
            cls.farms = json.load(fh)

    def test_dataset_shape(self):
        self.assertEqual(len(self.items), 4 * 110 * 8)
        with open(DATASET, newline="") as fh:
            header = next(csv.reader(fh))
        for col in ("farm_id", "timestamp", "day_of_crop", "n", "p", "k", "soil_moisture", "temperature",
                    "ph", "ec", "light", "pressure", "wind_speed", "injected_event"):
            self.assertIn(col, header)

    def test_every_injected_event_is_detected(self):
        for farm in self.farms:
            for ev in farm["events"]:
                pid, expected = EVENT_EXPECTATION[ev["name"]]
                lo, hi = ev["from_day"] + 3, ev["to_day"] - 5
                rows = [(r, a) for f, r, a in self.items if f == farm["id"] and lo <= int(r["day_of_crop"]) <= hi]
                if ev["name"] == "heat_stress":       # judged at the hot part of the day only
                    rows = [(r, a) for r, a in rows if int(r["timestamp"][11:13]) in (12, 15)]
                hits = sum(status(a, pid) == expected for r, a in rows)
                self.assertGreaterEqual(hits / len(rows), 0.85,
                                        "%s / %s detected in only %d of %d rows" % (farm["id"], ev["name"], hits, len(rows)))

    def test_well_managed_farm_has_no_nutrient_alarms(self):
        rows = [a for f, r, a in self.items if f == "farm_a"]
        bad = sum(1 for a in rows if any(status(a, p) in ("low", "high") for p in ("n", "p", "k")))
        self.assertLessEqual(bad / len(rows), 0.02)

    def test_farm_b_recovers_after_urea(self):
        rows = [a for f, r, a in self.items if f == "farm_b" and 58 <= int(r["day_of_crop"]) <= 72]
        ok = sum(status(a, "n") == "ok" for a in rows)
        self.assertGreaterEqual(ok / len(rows), 0.95)

    def test_stages_are_tagged_from_the_day(self):
        for f, r, a in self.items:
            d = int(r["day_of_crop"])
            expected = "germination" if d <= 20 else "vegetative" if d <= 50 else "flowering" if d <= 80 else "seed"
            self.assertEqual(a["stage"]["id"], expected)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(db_path=":memory:")
        self.c = self.app.test_client()

    reading = {"farm_id": "farm_b", "timestamp": "2025-12-10 12:00:00", "n": 90, "p": 9, "k": 80,
               "soil_moisture": 28, "temperature": 21, "ph": 6.8, "ec": 0.6, "light": 70000,
               "pressure": 955.0, "wind_speed": 2.0}

    def test_hardware_style_post_is_analysed_and_stored(self):
        r = self.c.post("/api/readings", json=self.reading)
        self.assertEqual(r.status_code, 201)
        a = r.get_json()["analysis"]
        self.assertEqual(a["day"], 39)
        self.assertEqual(a["stage"]["id"], "vegetative")
        self.assertEqual(a["fertilizer"][0]["product_id"], "urea")
        latest = self.c.get("/api/latest?farm_id=farm_b").get_json()
        self.assertEqual(latest["ts"], "2025-12-10 12:00:00")

    def test_bad_requests(self):
        self.assertEqual(self.c.post("/api/readings", json={"farm_id": "nope"}).status_code, 404)
        self.assertEqual(self.c.post("/api/readings", data="not json").status_code, 400)
        self.assertEqual(self.c.get("/api/latest?farm_id=farm_b").status_code, 404)   # nothing stored yet

    def test_pressure_trend_comes_from_previous_stored_reading(self):
        self.c.post("/api/readings", json=self.reading)
        later = dict(self.reading, timestamp="2025-12-10 15:00:00", pressure=951.0)
        a = self.c.post("/api/readings", json=later).get_json()["analysis"]
        self.assertEqual(next(p for p in a["parameters"] if p["id"] == "pressure")["status"], "watch")

    def test_replay_step_seek_and_reset(self):
        r = self.c.post("/api/replay/step", json={"farm_id": "farm_a", "steps": 5}).get_json()
        self.assertEqual((r["cursor"], len(r["added"])), (5, 5))
        r = self.c.post("/api/replay/seek", json={"farm_id": "farm_a", "day": 10}).get_json()
        self.assertEqual(r["cursor"], 88)                    # days 0..10 at 8 readings a day
        hist = self.c.get("/api/history?farm_id=farm_a").get_json()
        self.assertEqual(len(hist["rows"]), 88)
        r = self.c.post("/api/replay/reset", json={"farm_id": "farm_a"}).get_json()
        self.assertEqual((r["cursor"], r["added"]), (0, []))

    def test_replay_finishes(self):
        r = self.c.post("/api/replay/seek", json={"farm_id": "farm_c", "day": 500}).get_json()
        self.assertTrue(r["finished"])
        self.assertEqual(r["cursor"], 880)

    def test_analyze_endpoint_does_not_store(self):
        r = self.c.post("/api/analyze", json={"reading": {"n": 55, "p": 9, "k": 80}, "day": 40, "hour": 12})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["fertilizer"][0]["severity"], "severe")
        self.assertEqual(self.c.get("/api/latest?farm_id=farm_b").status_code, 404)


if __name__ == "__main__":
    unittest.main()
