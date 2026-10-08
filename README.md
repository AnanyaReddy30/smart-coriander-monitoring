# Smart Coriander Cultivation and Quality Monitoring System

A dataset-driven prototype. It replays a coriander sensor dataset as if it were live and tells the farmer, **at each growth stage, what is lacking and which fertilizer to apply**. It uses a rule-based engine only (no machine learning), as in the project proposal.

There is no hardware yet, so the sensor readings come from a simulated dataset. The system is built so real ESP32 readings can replace the dataset later without changing anything else.

## Run it

```bash
pip install -r requirements.txt
python run.py --fresh
```

Open http://127.0.0.1:5000. Use `--port 8000` for another port. `--fresh` clears previously replayed data.

Run the tests:

```bash
python -m unittest discover -s tests -v
```

## What the dashboard shows

| Tab | What it is for |
|---|---|
| Live monitoring | Pick a farm, press Play. The **season grid** colours every parameter on every day of the 110-day crop cycle. **Right now** names what is lacking and the fertilizer type and quantity (kg per acre and per hectare), and says whether conditions allow applying it. The **on-site display preview** mirrors the LED strip and 16x2 LCD from the hardware design. |
| Test cases | 13 controlled cases (proposal section 19.6): deficient, optimal and excess readings, fertilizer choice, and delay rules. Each shows whether the result matches what was expected. You can also type your own values. |
| Ideal ranges and method | Every stage-wise range, the dose calculation, product table and sources. |

A suggested 3-minute demo: Farm B (nitrogen deficiency) to day 35, then Play to day 56 to see the fix; Farm C for phosphorus and potash; Farm D for salinity, excess nitrogen and heat; then the Test cases tab.

## The four farms

| Farm | Situation built into the data |
|---|---|
| A, well managed | Stays nearly all green. One storm at day 43 shows the falling-pressure and strong-wind alerts. |
| B, nitrogen deficient | N falls below range from day 21. Urea is applied at day 56 and N recovers. |
| C, low phosphorus and potash | P is low all season, K falls below range from about day 40. Nothing is corrected. |
| D, saline, over-fed, hot | High EC and pH, excess N early, a heat spell in days 76-92, and uneven irrigation. |

## About the dataset (please state this in your report)

`data/coriander_sensor_dataset.csv` is **simulated, not measured**. No public dataset combining NPK, moisture, pH, EC, light, pressure and wind for coriander exists, so `data/generate_dataset.py` generates one: 4 farms x 110 days x 8 readings a day = 3,520 rows.

- Nutrient, pH and EC curves are set by hand for each farm; temperature, light, pressure, wind and soil moisture follow simple physical shapes plus noise.
- Every row carries an `injected_event` label (for example `n_deficiency`) so the engine can be checked against known truth.
- The tests confirm the engine detects each injected event. Because the same team wrote the data and the rules, that shows the pipeline is consistent, **not** that it would be accurate on real field data.
- Regenerate with `python data/generate_dataset.py`. It is seeded, so the output is repeatable.

## How a recommendation is worked out

1. The growth stage comes from the sowing date and the day count (germination 0-20, vegetative 21-50, flowering 51-80, seed development 81-110).
2. Each of the 10 parameters is compared with that stage's range in `config/coriander_config.json` and marked low, optimal or high. Light is judged only in the daytime; pressure only matters when it falls fast.
3. The distance outside the range sets the severity: mild up to 15%, moderate up to 40%, severe beyond.
4. For a deficient nutrient, one correction = recommended total dose x 0.5 x severity multiplier (0.5, 1.0, 1.5). The recommended totals are 60:30:20 kg N:P2O5:K2O per hectare, from the ICAR-NRCSS Ajmer trial on variety ACr-2.
5. The nutrient dose is divided by the product's nutrient content to give the product quantity: urea (46% N), DAP (18% N, 46% P2O5), SSP (16% P2O5), MOP (60% K2O), SOP (50% K2O).
6. Product rules: DAP's nitrogen is subtracted from the urea dose; SSP replaces DAP if nitrogen is already high; SOP replaces MOP if salinity is high (coriander is salt-sensitive).
7. Application is delayed when soil is waterlogged, wind is strong, or pressure is falling fast (rain likely). If the soil is dry, it advises light irrigation afterwards.

This does not convert a probe reading straight into kg/ha: RS-485 NPK probes are indicative, and doing so would overstate the need. The severity multipliers are a design choice you can defend or change. A stronger future version would use the soil-test crop-response equations from the BHU and PLOS One studies listed in the config.

## Values you must verify before any field use

Every range in `config/coriander_config.json` is marked `"verify": true`. They are working values built from Indian soil-fertility rating classes (for N, P, K) and general coriander agronomy (for the rest), not final agronomic recommendations. Confirm them with ICAR, the Spices Board or your state agricultural university, and calibrate the sensors, before anyone applies fertilizer based on this output. The prototype assumes **seed coriander**; leaf (cilantro) would need different stages and ranges.

## Connecting real hardware later

The ESP32 posts JSON to the same endpoint the replay uses. The field names match the CSV columns:

```json
POST /api/readings
{"farm_id": "field_1", "n": 92, "p": 8.5, "k": 71, "soil_moisture": 27.4,
 "temperature": 21.3, "ph": 6.8, "ec": 0.62, "light": 64000,
 "pressure": 954.8, "wind_speed": 2.1}
```

`timestamp` is optional (the server clock is used). Steps:

1. Add your field to `data/farms.json` with its `sowing_date` (copy an existing entry; use an id that has no rows in the CSV, for example `field_1`).
2. Restart the server. A farm with no dataset rows shows as a live farm: replay controls are hidden and the page refreshes every 5 seconds.
3. Point the ESP32 at `http://<server-ip>:5000/api/readings`.

Minimal Arduino sketch (ESP32 core, ArduinoJson 7). It has not been compiled or run on hardware:

```cpp
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

void sendReading(float n, float p, float k, float moisture, float temp,
                 float ph, float ec, float lux, float hpa, float wind) {
  HTTPClient http;
  http.begin("http://192.168.1.50:5000/api/readings");   // your server
  http.addHeader("Content-Type", "application/json");
  JsonDocument doc;
  doc["farm_id"] = "field_1";
  doc["n"] = n; doc["p"] = p; doc["k"] = k;
  doc["soil_moisture"] = moisture; doc["temperature"] = temp;
  doc["ph"] = ph; doc["ec"] = ec; doc["light"] = lux;
  doc["pressure"] = hpa; doc["wind_speed"] = wind;
  String body; serializeJson(doc, body);
  http.POST(body);
  http.end();
}
```

## Project layout

```
config/coriander_config.json   stage-wise ranges, fertilizer plan, products, sources
data/generate_dataset.py       builds the dataset and farms.json
data/coriander_sensor_dataset.csv
app/engine.py                  rule engine (stage, classification, fertilizer, display text)
app/store.py                   SQLite logging of readings and analyses (proposal Stage 8)
app/server.py                  Flask API: /api/readings, /api/replay/*, /api/analyze, /api/history ...
app/static/                    dashboard (index.html, style.css, app.js), no external libraries
tests/                         34 tests: engine cases, dataset events, API
run.py                         start the app
```

## Known limits

- Simulated data, so nothing here validates the system on real crops or real sensors.
- Ranges and severity multipliers need agronomic verification (above).
- Soil moisture, pH and EC ranges depend on how the real sensors are calibrated.
- Single crop (coriander, seed), single soil profile, no weather forecast; "storm" is inferred only from the pressure trend.
- The dashboard loads two fonts from Google Fonts; offline it falls back to system fonts with no loss of function.
