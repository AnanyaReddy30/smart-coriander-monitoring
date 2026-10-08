"""Controlled test cases for the rule engine (proposal section 19.6).

Run:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.engine import analyze, load_config, stage_for_day  # noqa: E402

CFG = load_config()
BASE = {"n": 150, "p": 9, "k": 80, "moisture": 30, "temperature": 22, "ph": 6.9,
        "ec": 0.6, "light": 70000, "pressure": 955, "wind": 2}


def run(day=40, hour=12, delta=None, **changes):
    reading = dict(BASE)
    reading.update(changes)
    return analyze(CFG, reading, day, hour=hour, pressure_delta=delta)


def rec(a, nutrient):
    return next((f for f in a["fertilizer"] if f["nutrient"] == nutrient), None)


def param(a, pid):
    return next(p for p in a["parameters"] if p["id"] == pid)


class StageTests(unittest.TestCase):
    def test_stage_boundaries(self):
        expected = {0: "germination", 20: "germination", 21: "vegetative", 50: "vegetative",
                    51: "flowering", 80: "flowering", 81: "seed", 110: "seed", 140: "seed", -3: "germination"}
        for day, stage in expected.items():
            self.assertEqual(stage_for_day(CFG, day)["id"], stage, "day %d" % day)

    def test_ranges_change_with_stage(self):
        # 100 mg/kg N is fine at germination (min 100) but deficient while vegetative (min 125)
        self.assertEqual(param(run(day=10, n=100), "n")["status"], "ok")
        self.assertEqual(param(run(day=40, n=100), "n")["status"], "low")


class NutrientTests(unittest.TestCase):
    def test_all_optimal_needs_nothing(self):
        a = run()
        self.assertEqual(a["fertilizer"], [])
        self.assertEqual(a["overall"]["led"], "green")
        self.assertEqual(a["application_window"]["state"], "none")

    def test_nitrogen_deficiency_gives_urea(self):
        a = run(n=95)                       # 24% below min -> moderate
        f = rec(a, "N")
        self.assertEqual(len(a["fertilizer"]), 1)
        self.assertEqual(f["severity"], "moderate")
        self.assertEqual(f["product_id"], "urea")
        self.assertAlmostEqual(f["nutrient_kg_ha"], 30.0)          # 60 kg total x 0.5 x 1.0
        self.assertAlmostEqual(f["kg_per_ha"], 65.2, places=1)     # 30 / 0.46
        self.assertAlmostEqual(f["kg_per_acre"], 26.4, places=1)

    def test_dose_scales_with_severity(self):
        mild = rec(run(n=115), "N")       # 8% below
        moderate = rec(run(n=95), "N")
        severe = rec(run(n=55), "N")      # 56% below
        self.assertEqual((mild["severity"], moderate["severity"], severe["severity"]), ("mild", "moderate", "severe"))
        self.assertLess(mild["kg_per_ha"], moderate["kg_per_ha"])
        self.assertLess(moderate["kg_per_ha"], severe["kg_per_ha"])
        self.assertEqual(run(n=55)["overall"]["led"], "red")

    def test_phosphorus_uses_dap(self):
        f = rec(run(day=10, p=5), "P")
        self.assertEqual(f["product_id"], "dap")
        self.assertAlmostEqual(f["kg_per_ha"], 15 / 0.46, places=1)

    def test_potassium_uses_mop_normally(self):
        f = rec(run(day=60, k=45), "K")
        self.assertEqual(f["product_id"], "mop")

    def test_potassium_uses_sop_in_saline_soil(self):
        a = run(day=60, k=45, ec=1.8)
        self.assertEqual(rec(a, "K")["product_id"], "sop")
        self.assertTrue(any(x["parameter"] == "ec" for x in a["actions"]))

    def test_dap_nitrogen_credit_reduces_urea(self):
        a = run(n=95, p=4)
        self.assertEqual(a["fertilizer"][0]["nutrient"], "P")            # DAP listed first
        alone = rec(run(n=95), "N")["kg_per_ha"]
        self.assertLess(rec(a, "N")["kg_per_ha"], alone)

    def test_excess_nitrogen_gets_no_nitrogen_fertilizer(self):
        a = run(n=300)
        self.assertEqual(param(a, "n")["status"], "high")
        self.assertIsNone(rec(a, "N"))
        self.assertTrue(any(x["parameter"] == "n" and x["kind"] == "hold" for x in a["actions"]))

    def test_ssp_replaces_dap_when_nitrogen_is_high(self):
        a = run(n=300, p=4)
        self.assertEqual(rec(a, "P")["product_id"], "ssp")


class EnvironmentTests(unittest.TestCase):
    def test_heat_stress_at_flowering(self):
        a = run(day=70, hour=14, temperature=36)
        self.assertEqual(param(a, "temperature")["status"], "high")
        self.assertNotEqual(a["overall"]["led"], "green")

    def test_high_salinity_is_severe_and_red(self):
        a = run(ec=2.4)
        self.assertEqual(param(a, "ec")["severity"], "severe")
        self.assertEqual(a["overall"]["led"], "red")

    def test_light_not_judged_at_night(self):
        a = run(hour=0, light=0)
        self.assertEqual(param(a, "light")["status"], "na")
        self.assertEqual(a["overall"]["led"], "green")

    def test_light_low_at_midday_is_flagged(self):
        self.assertEqual(param(run(hour=12, light=8000), "light")["status"], "low")

    def test_missing_value_is_reported(self):
        reading = dict(BASE); reading["ph"] = None
        a = analyze(CFG, reading, 40, hour=12)
        self.assertEqual(param(a, "ph")["status"], "missing")

    def test_dataset_field_names_are_accepted(self):
        reading = dict(BASE); reading["soil_moisture"] = reading.pop("moisture"); reading["wind_speed"] = reading.pop("wind")
        a = analyze(CFG, reading, 40, hour=12)
        self.assertEqual(param(a, "moisture")["value"], 30.0)
        self.assertEqual(param(a, "wind")["value"], 2.0)


class ApplicationWindowTests(unittest.TestCase):
    def test_dry_soil_apply_then_irrigate(self):
        self.assertEqual(run(n=95, moisture=15)["application_window"]["state"], "irrigate")

    def test_waterlogged_soil_delays_application(self):
        a = run(n=95, moisture=46)
        self.assertEqual(a["application_window"]["state"], "delay")
        self.assertEqual(len(a["fertilizer"]), 1)     # still recommended, just not yet

    def test_storm_delays_application(self):
        a = run(n=95, wind=9.5, delta=-3.5)
        self.assertEqual(param(a, "pressure")["status"], "watch")
        self.assertEqual(a["application_window"]["state"], "delay")

    def test_slow_pressure_change_is_ignored(self):
        self.assertEqual(param(run(delta=-0.8), "pressure")["status"], "ok")


class DisplayTests(unittest.TestCase):
    def test_lcd_lines_fit_16_characters(self):
        for a in (run(), run(n=95), run(n=95, p=4, k=40), run(ec=2.4, ph=8.2, moisture=5)):
            for line in a["display"]["lcd"]:
                self.assertLessEqual(len(line), 16)

    def test_led_strip_has_one_led_per_parameter(self):
        self.assertEqual(len(run()["display"]["leds"]), 10)


if __name__ == "__main__":
    unittest.main()
