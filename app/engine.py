"""Rule-based analysis engine for the Smart Coriander Cultivation and Quality
Monitoring System.

Given one sensor reading and the crop's day count, the engine
  1. finds the growth stage,
  2. classifies every parameter as optimal / low / high against that stage's
     ideal range,
  3. works out what is lacking and which fertilizer (type and quantity) to
     apply,
  4. decides whether conditions are suitable for applying fertilizer now,
  5. builds the short text and colours for an on-site LCD / LED strip.

It is deliberately rule-based (no machine learning), so every recommendation can
be traced back to a measured value and a configured coriander requirement.
All ranges live in config/coriander_config.json.
"""
import json
import math
import os

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "coriander_config.json"
)

PARAM_ORDER = ["n", "p", "k", "moisture", "temperature", "ph", "ec", "light", "pressure", "wind"]

# Names the ESP32 payload / dataset use -> internal names
FIELD_ALIASES = {"soil_moisture": "moisture", "wind_speed": "wind"}

NUTRIENT_OXIDE = {"n": "N", "p": "P2O5", "k": "K2O"}
SEVERITY_RANK = {"mild": 0, "moderate": 1, "severe": 2}

HOW_TO_APPLY = {
    "N": "Broadcast or side-dress, then irrigate lightly. If the deficit is still there after 7-10 days, repeat once.",
    "P": "Place near the root zone and irrigate lightly; phosphorus moves slowly in soil.",
    "K": "Side-dress and irrigate lightly.",
}


def load_config(path=None):
    with open(path or DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def stage_for_day(cfg, day):
    """Return the stage dict for a day count. Days outside the cycle are clamped."""
    stages = cfg["stages"]
    if day is None or day < stages[0]["start_day"]:
        return stages[0]
    for st in stages:
        if st["start_day"] <= day <= st["end_day"]:
            return st
    return stages[-1]


def normalise_reading(reading):
    """Map payload field names to internal names and coerce to floats (or None)."""
    out = {}
    for key, val in (reading or {}).items():
        out[FIELD_ALIASES.get(key, key)] = val
    clean = {}
    for pid in PARAM_ORDER:
        val = out.get(pid)
        try:
            val = float(val)
            if math.isnan(val) or math.isinf(val):
                val = None
        except (TypeError, ValueError):
            val = None
        clean[pid] = val
    return clean


def _severity(cfg, deviation):
    sev = cfg["severity"]
    if deviation <= sev["mild_max_fraction"]:
        return "mild"
    if deviation <= sev["moderate_max_fraction"]:
        return "moderate"
    return "severe"


def _status_label(kind, status):
    if status == "low":
        return "Deficient" if kind == "nutrient" else "Low"
    if status == "high":
        return "Excess" if kind == "nutrient" else "High"
    return {"ok": "Optimal", "na": "Not evaluated", "watch": "Falling fast", "missing": "No data"}[status]


def _analyse_parameters(cfg, reading, stage, hour, pressure_delta):
    params = {}
    for pid in PARAM_ORDER:
        pcfg = cfg["parameters"][pid]
        rng = pcfg["ranges"][stage["id"]]
        lo, hi = rng.get("min"), rng.get("max")
        val = reading.get(pid)
        entry = {
            "id": pid, "label": pcfg["label"], "short": pcfg["short"], "unit": pcfg["unit"],
            "kind": pcfg["kind"], "sensor": pcfg["sensor"], "value": val,
            "min": lo, "max": hi, "status": "ok", "severity": None, "deviation": 0.0,
        }

        if val is None:
            entry["status"] = "missing"
        elif pid == "light" and not _is_daytime(pcfg, val, hour):
            entry["status"] = "na"
        elif pid == "pressure":
            limit = pcfg["falling_fast_hpa_per_3h"]
            if pressure_delta is not None and pressure_delta <= limit:
                entry["status"] = "watch"
                entry["severity"] = "moderate"
        else:
            if lo is not None and val < lo:
                entry["status"] = "low"
                entry["deviation"] = (lo - val) / lo if lo else 1.0
            elif hi is not None and val > hi:
                entry["status"] = "high"
                entry["deviation"] = (val - hi) / hi if hi else 1.0
            if entry["status"] in ("low", "high"):
                entry["severity"] = _severity(cfg, entry["deviation"])

        entry["status_label"] = _status_label(entry["kind"], entry["status"])
        params[pid] = entry
    return params


def _is_daytime(pcfg, value, hour):
    if hour is not None:
        h0, h1 = pcfg["daytime_hours"]
        return h0 <= hour <= h1
    return value >= 5000  # no clock available: treat very dim readings as night


def _fertilizer_recommendations(cfg, params):
    plan = cfg["fertilizer_plan"]
    prods = cfg["products"]
    acre = plan["kg_per_acre_per_kg_per_ha"]

    needs = {}
    for pid, oxide in NUTRIENT_OXIDE.items():
        entry = params[pid]
        if entry["status"] == "low":
            base = plan["recommended_total_kg_ha"][oxide] * plan["corrective_fraction_of_total"][oxide]
            needs[pid] = base * plan["severity_multiplier"][entry["severity"]]

    def make(pid, product_id, kg_ha, nutrient_kg, note, supplies=None):
        entry = params[pid]
        return {
            "nutrient": entry["short"], "nutrient_label": entry["label"],
            "status": entry["status"], "severity": entry["severity"],
            "reading": entry["value"], "min": entry["min"], "unit": entry["unit"],
            "product_id": product_id, "product": prods[product_id]["name"],
            "kg_per_ha": round(kg_ha, 1), "kg_per_acre": round(kg_ha * acre, 1),
            "nutrient_kg_ha": round(nutrient_kg, 1), "oxide": NUTRIENT_OXIDE[pid],
            "supplies": supplies or {}, "how": HOW_TO_APPLY[entry["short"]], "note": note,
        }

    recs = []
    n_credit = 0.0

    if "p" in needs:
        n_high = params["n"]["status"] == "high"
        pid_prod = "ssp" if n_high else "dap"
        kg = needs["p"] / prods[pid_prod]["P2O5"]
        supplies, note = {}, ""
        if pid_prod == "dap":
            n_credit = kg * prods["dap"]["N"]
            supplies = {"N": round(n_credit, 1)}
            note = "DAP also supplies about %.1f kg N/ha." % n_credit
        else:
            note = "Single super phosphate chosen instead of DAP because nitrogen is already high."
        recs.append(make("p", pid_prod, kg, needs["p"], note, supplies))

    if "n" in needs:
        left = max(0.0, needs["n"] - n_credit)
        note = ""
        if n_credit > 0:
            note = "Reduced from %.1f kg N/ha because DAP already supplies about %.1f." % (needs["n"], n_credit)
        if left < 0.5:
            rec = make("n", "urea", 0.0, 0.0, "Nitrogen need is covered by the DAP above.")
        else:
            rec = make("n", "urea", left / prods["urea"]["N"], left, note)
        recs.append(rec)

    if "k" in needs:
        saline = params["ec"]["status"] == "high"
        prod = "sop" if saline else "mop"
        note = "Sulphate of potash chosen instead of MOP because salinity is high." if saline else ""
        recs.append(make("k", prod, needs["k"] / prods[prod]["K2O"], needs["k"], note))

    # Show phosphorus first when DAP is used, because the urea dose is then reduced by DAP's nitrogen.
    order = {"N": 0, "P": 1, "K": 2}
    if any(r["product_id"] == "dap" for r in recs):
        order = {"P": 0, "N": 1, "K": 2}
    recs.sort(key=lambda r: order[r["nutrient"]])
    return recs


def _field_actions(cfg, params):
    actions = []
    for pid in PARAM_ORDER:
        e = params[pid]
        pcfg = cfg["parameters"][pid]
        if e["kind"] == "nutrient":
            if e["status"] == "high":
                actions.append({"parameter": pid, "label": e["label"], "severity": e["severity"],
                                "kind": "hold", "text": pcfg["excess_advice"]})
            continue
        if e["status"] == "low" and "low_advice" in pcfg:
            actions.append({"parameter": pid, "label": e["label"], "severity": e["severity"],
                            "kind": "act", "text": pcfg["low_advice"]})
        elif e["status"] == "high" and "high_advice" in pcfg:
            actions.append({"parameter": pid, "label": e["label"], "severity": e["severity"],
                            "kind": "act", "text": pcfg["high_advice"]})
        elif pid == "pressure" and e["status"] == "watch":
            actions.append({"parameter": pid, "label": e["label"], "severity": "moderate", "kind": "act",
                            "text": "Pressure is falling fast, so rain or a storm is likely. Hold fertilizer application and irrigation."})
    actions.sort(key=lambda a: -SEVERITY_RANK.get(a["severity"], 0))
    return actions


def _application_window(params, recs):
    reasons = []
    if params["moisture"]["status"] == "high":
        reasons.append("soil is too wet")
    if params["wind"]["status"] == "high":
        reasons.append("wind is strong")
    if params["pressure"]["status"] == "watch":
        reasons.append("pressure is falling fast (rain or storm likely)")

    if not recs:
        return {"state": "none", "message": "No fertilizer is needed right now.", "reasons": reasons}
    if reasons:
        return {"state": "delay",
                "message": "Wait before applying fertilizer: " + "; ".join(reasons) + ".",
                "reasons": reasons}
    if params["moisture"]["status"] == "low":
        return {"state": "irrigate",
                "message": "Good to apply, and irrigate lightly afterwards because the soil is dry.",
                "reasons": []}
    return {"state": "ok", "message": "Conditions are suitable to apply fertilizer now.", "reasons": []}


def _led_colour(params):
    colour = "green"
    for e in params.values():
        if e["status"] in ("low", "high", "watch"):
            colour = "amber"
            if e["severity"] == "severe":
                return "red"
    return colour


def _lcd_lines(stage, day, recs, params, led):
    line1 = ("D%03d %s" % (day if day is not None else 0, stage["short"]))[:16]
    if recs:
        r = recs[0]
        short = {"urea": "UREA", "dap": "DAP", "ssp": "SSP", "mop": "MOP", "sop": "SOP"}[r["product_id"]]
        line2 = "%s LOW %s %d/ac" % (r["nutrient"], short, round(r["kg_per_acre"]))
    else:
        line2 = "ALL OK"
        for pid in PARAM_ORDER:
            e = params[pid]
            if e["status"] in ("low", "high", "watch"):
                word = "FALL" if e["status"] == "watch" else e["status"].upper()
                line2 = ("%s %s" % (e["short"].upper(), word))
                break
    return [line1, line2[:16]]


def _plain_name(entry):
    """'Nitrogen (N)' -> 'nitrogen', 'Soil pH' -> 'soil pH' (used in headlines)."""
    name = entry["label"].split(" (")[0]
    return name.lower().replace(" ph", " pH")


def analyze(cfg, reading, day, hour=None, pressure_delta=None):
    """Analyse one reading. `reading` may use dataset/ESP32 field names."""
    reading = normalise_reading(reading)
    stage = stage_for_day(cfg, day)
    params = _analyse_parameters(cfg, reading, stage, hour, pressure_delta)
    recs = _fertilizer_recommendations(cfg, params)
    actions = _field_actions(cfg, params)
    window = _application_window(params, recs)
    led = _led_colour(params)

    issues = [e for e in params.values() if e["status"] in ("low", "high", "watch")]
    if not issues:
        headline = "All monitored parameters are within the ideal range for this stage."
    else:
        names = ", ".join("%s %s" % (_plain_name(e), e["status_label"].lower()) for e in issues[:5])
        more = "" if len(issues) <= 5 else " and %d more" % (len(issues) - 5)
        headline = "%d issue%s: %s%s." % (len(issues), "" if len(issues) == 1 else "s", names, more)

    return {
        "day": day,
        "stage": {"id": stage["id"], "label": stage["label"], "short": stage["short"]},
        "overall": {"led": led, "issue_count": len(issues), "headline": headline},
        "parameters": [params[pid] for pid in PARAM_ORDER],
        "fertilizer": recs,
        "actions": actions,
        "application_window": window,
        "display": {
            "led": led,
            "leds": [_param_led(params[pid]) for pid in PARAM_ORDER],
            "lcd": _lcd_lines(stage, day, recs, params, led),
        },
    }


def _param_led(entry):
    if entry["status"] in ("low", "high", "watch"):
        return "red" if entry["severity"] == "severe" else "amber"
    if entry["status"] in ("na", "missing"):
        return "off"
    return "green"
