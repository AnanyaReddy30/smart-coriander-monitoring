#!/usr/bin/env python3
"""Generate the coriander sensor dataset used in place of live hardware.

The data is SIMULATED, not measured. It represents one seed-coriander crop cycle
(110 days, one reading every 3 hours) on four farms, each built to show a
different situation the system must handle:

  farm_a  well managed         - stays mostly optimal, one storm, one N top-dress
  farm_b  nitrogen deficient   - N falls during vegetative growth, farmer corrects at day 56
  farm_c  low phosphorus/potash - P low all season, K falls from mid-vegetative stage
  farm_d  saline, over-fed, hot - high EC and pH, excess N early, heat spell, dry/wet swings

Nutrient, pH and EC trajectories are set by hand-picked key points; temperature,
light, pressure and wind follow simple physical shapes plus noise. Every row has an
`injected_event` label so the analysis can be checked against known truth.

The column names and units match what the ESP32 would send, so replacing this file
with real logs later needs no change to the rest of the system.

Run:  python data/generate_dataset.py
"""
import csv
import json
import math
import os
import random
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
START = datetime(2025, 11, 1, 0, 0, 0)     # sowing date (rabi season)
DAYS = 110
STEP_H = 3
STEPS = DAYS * 24 // STEP_H
BASE_PRESSURE_HPA = 955.0                  # site altitude ~500 m (change for your site)

COLUMNS = ["farm_id", "timestamp", "day_of_crop", "n", "p", "k", "soil_moisture",
           "temperature", "ph", "ec", "light", "pressure", "wind_speed", "injected_event"]

# Mean daily temperature (deg C) through the rabi season
MEAN_TEMP = [(0, 24), (20, 21), (40, 19), (60, 20), (80, 23), (95, 25.5), (110, 27)]

FARMS = [
    {
        "id": "farm_a", "name": "Farm A - well managed", "seed": 11,
        "description": "Balanced nutrients, sensible irrigation and a top-dress at day 33. One storm at day 43.",
        "n": [(0, 190), (20, 170), (32, 150), (33, 190), (50, 165), (80, 130), (110, 105)],
        "p": [(0, 12), (30, 10), (60, 9), (110, 7.5)],
        "k": [(0, 95), (60, 80), (110, 72)],
        "ph": [(0, 6.9), (110, 6.9)],
        "ec": [(0, 0.45), (110, 0.7)],
        "heat": (0, 0, 0.0),
        "irrigation": "good",
        "storms": [(43, 14, 8.0, 8.5, 9.0)],   # day, hour, pressure dip hPa, wind peak m/s, rain % VWC
        "events": [],
    },
    {
        "id": "farm_b", "name": "Farm B - nitrogen deficient", "seed": 22,
        "description": "Nitrogen runs short from day 21. Urea is applied at day 56 and the crop recovers.",
        "n": [(0, 120), (15, 108), (22, 98), (40, 82), (55, 62), (56, 128), (80, 112), (110, 95)],
        "p": [(0, 11), (110, 8)],
        "k": [(0, 95), (60, 80), (110, 72)],
        "ph": [(0, 6.8), (110, 6.8)],
        "ec": [(0, 0.5), (110, 0.75)],
        "heat": (0, 0, 0.0),
        "irrigation": "good",
        "storms": [],
        "events": [("n_deficiency", 21, 55)],
    },
    {
        "id": "farm_c", "name": "Farm C - low phosphorus and potash", "seed": 33,
        "description": "Phosphorus is low all season and potash falls below range from about day 40. Nothing is corrected.",
        "n": [(0, 190), (20, 170), (32, 150), (33, 190), (50, 165), (80, 130), (110, 105)],
        "p": [(0, 5.5), (20, 5), (60, 4.2), (110, 3.6)],
        "k": [(0, 68), (30, 62), (50, 52), (80, 46), (110, 42)],
        "ph": [(0, 6.3), (110, 6.3)],
        "ec": [(0, 0.5), (110, 0.8)],
        "heat": (0, 0, 0.0),
        "irrigation": "good",
        "storms": [(70, 15, 7.0, 7.5, 8.0)],
        "events": [("p_deficiency", 0, 110), ("k_deficiency", 46, 110)],
    },
    {
        "id": "farm_d", "name": "Farm D - saline, over-fed and hot", "seed": 44,
        "description": "Saline water and alkaline soil, excess nitrogen early on, a heat spell in days 76-92 and uneven irrigation.",
        "n": [(0, 300), (20, 285), (45, 265), (70, 200), (110, 140)],
        "p": [(0, 14), (110, 9)],
        "k": [(0, 100), (110, 80)],
        "ph": [(0, 7.7), (40, 7.9), (110, 8.2)],
        "ec": [(0, 1.0), (20, 1.3), (60, 1.9), (110, 2.3)],
        "heat": (76, 92, 6.0),
        "irrigation": "poor",
        "storms": [],
        "events": [("n_excess", 0, 32), ("ec_high", 0, 110), ("ph_high", 0, 110), ("heat_stress", 76, 92)],
    },
]

STAGE_MOISTURE = [(0, 20, 25, 42), (21, 50, 22, 38), (51, 80, 18, 35), (81, 110, 12, 28)]  # start,end,min,max


def interp(points, x):
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def gauss(t, centre, width):
    return math.exp(-((t - centre) / width) ** 2)


def stage_moisture_band(day):
    for a, b, lo, hi in STAGE_MOISTURE:
        if a <= day <= b:
            return lo, hi
    return STAGE_MOISTURE[-1][2], STAGE_MOISTURE[-1][3]


def simulate_farm(farm):
    rng = random.Random(farm["seed"])
    rows = []

    cloud = 0.85
    weather_p = 0.0
    n_err = p_err = k_err = 0.0
    moisture = 30.0
    last_irrigation_day = -99
    irrigation_days = {2: 10.0, 15: 22.0, 30: 8.0, 44: 8.0, 66: 18.0, 84: 14.0, 96: 10.0} if farm["irrigation"] == "poor" else {}
    hours_since_start = 0.0

    for step in range(STEPS):
        ts = START + timedelta(hours=STEP_H * step)
        t_h = STEP_H * step
        day = t_h // 24
        hour = ts.hour
        events = []

        # --- storms (pressure dip, wind gust, rain) ---------------------------
        dip = wind_add = rain = 0.0
        cloud_hit = 0.0
        for s_day, s_hour, depth, wpeak, rain_total in farm["storms"]:
            t0 = s_day * 24 + s_hour
            dip += depth * gauss(t_h, t0, 8.0)
            wind_add += (wpeak - 2.5) * gauss(t_h, t0 + 2, 4.0)
            rain += rain_total * 0.30 * gauss(t_h, t0 + 3, 4.0)
            cloud_hit = max(cloud_hit, gauss(t_h, t0, 10.0))
            if abs(t_h - t0) <= 12:
                events.append("storm")

        # --- temperature ----------------------------------------------------
        mean_t = interp(MEAN_TEMP, t_h / 24.0)
        h_start, h_end, boost = farm["heat"]
        if boost > 0 and h_start <= day <= h_end:
            mean_t += boost
            events.append("heat_stress")
        diurnal = 4.0 * math.sin(math.pi * (hour - 8) / 12.0) if 8 <= hour <= 20 else -4.0 * 0.6
        temp = mean_t + diurnal + rng.gauss(0, 0.5)

        # --- light ----------------------------------------------------------
        if step % 8 == 0:
            cloud = min(1.0, max(0.35, 0.8 * cloud + 0.2 * rng.uniform(0.45, 1.0) + rng.gauss(0, 0.05)))
        sun = max(0.0, math.sin(math.pi * (hour - 6) / 12.0)) ** 1.2
        lux = 88000 * sun * cloud * (1 - 0.6 * cloud_hit) * (1 + rng.gauss(0, 0.03))
        lux = max(0.0, lux)

        # --- pressure -------------------------------------------------------
        weather_p = 0.97 * weather_p + rng.gauss(0, 0.35)
        tide = 0.8 * math.sin(2 * math.pi * (hour - 4) / 12.0)
        pressure = BASE_PRESSURE_HPA + weather_p + tide - dip + rng.gauss(0, 0.1)

        # --- wind -----------------------------------------------------------
        wind_diurnal = 1.2 * max(0.0, math.sin(math.pi * (hour - 8) / 12.0)) if 8 <= hour <= 20 else 0.0
        wind = 1.6 + wind_diurnal + abs(rng.gauss(0, 0.6)) + wind_add
        wind = max(0.0, wind)

        # --- soil moisture --------------------------------------------------
        lo, hi = stage_moisture_band(day)
        light_frac = sun * cloud
        et = 0.03 + 0.40 * light_frac * (temp / 22.0)
        moisture -= et
        moisture += rain
        if farm["irrigation"] == "good":
            if hour == 6 and moisture < lo + 2.5 and day - last_irrigation_day >= 4:
                moisture = (lo + hi) / 2.0 + rng.uniform(-1.5, 1.5)
                last_irrigation_day = day
        else:
            if hour == 6 and day in irrigation_days and last_irrigation_day != day:
                moisture += irrigation_days[day]
                last_irrigation_day = day
        moisture = min(max(moisture, 5.0), 60.0)
        soil_moist = moisture + rng.gauss(0, 0.25)

        # --- nutrients (key-point curves + slow measurement drift) -----------
        n_err = 0.9 * n_err + rng.gauss(0, 1.6)
        p_err = 0.9 * p_err + rng.gauss(0, 0.12)
        k_err = 0.9 * k_err + rng.gauss(0, 0.9)
        d = t_h / 24.0
        n_val = max(0.0, interp(farm["n"], d) + n_err)
        p_val = max(0.0, interp(farm["p"], d) + p_err)
        k_val = max(0.0, interp(farm["k"], d) + k_err)

        # --- pH and EC ------------------------------------------------------
        ph_val = interp(farm["ph"], d) + rng.gauss(0, 0.03)
        dryness = max(0.0, (30.0 - moisture) / 30.0)
        ec_val = interp(farm["ec"], d) * (1 + 0.15 * dryness) + rng.gauss(0, 0.03)
        ec_val = max(0.05, ec_val)

        # --- ground-truth labels -------------------------------------------
        for name, d0, d1 in farm["events"]:
            if d0 <= day <= d1 and name != "heat_stress":
                events.append(name)
        events = sorted(set(events))

        rows.append({
            "farm_id": farm["id"],
            "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "day_of_crop": int(day),
            "n": round(n_val, 1), "p": round(p_val, 2), "k": round(k_val, 1),
            "soil_moisture": round(soil_moist, 1),
            "temperature": round(temp, 1),
            "ph": round(ph_val, 2),
            "ec": round(ec_val, 2),
            "light": int(round(lux)),
            "pressure": round(pressure, 1),
            "wind_speed": round(wind, 1),
            "injected_event": ";".join(events) if events else "none",
        })
    return rows


def main():
    all_rows = []
    for farm in FARMS:
        all_rows.extend(simulate_farm(farm))

    csv_path = os.path.join(HERE, "coriander_sensor_dataset.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(all_rows)

    farms_meta = [{
        "id": f["id"], "name": f["name"], "description": f["description"],
        "sowing_date": START.strftime("%Y-%m-%d"),
        "events": [{"name": n, "from_day": a, "to_day": b} for n, a, b in f["events"]],
    } for f in FARMS]
    with open(os.path.join(HERE, "farms.json"), "w", encoding="utf-8") as fh:
        json.dump(farms_meta, fh, indent=2)

    print("Wrote %d rows to %s" % (len(all_rows), csv_path))


if __name__ == "__main__":
    main()
