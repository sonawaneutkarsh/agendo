# Agendo — 2-Minute Demo Script

- **0:00–0:20 — Setup:**
  Open `http://localhost:8000`. Show the ESP32 test bench (`esp32-bench-01`) with 4 healthy pins (`GPIO4` I2C_SDA 3300mV pull-up on, `GPIO5` I2C_SCL 3300mV pull-up on, `GPIO12` ADC_IN 1800mV, `GPIO13` STATUS_LED) and 2 calibration rows.
  *"Gemini agent diagnosing a brownout. It may tune `pin_configs`, but not drop tables."*

- **0:20–0:50 — The Bad Write:**
  Run `python -m demo.run_demo` (or with `--live-gemini`). The agent reconfigures `GPIO12` to `OUTPUT_HIGH` at `3300mV` (wrong — it is an input-only sensor pin), disables `GPIO4`/`GPIO5` pull-ups, and wipes the 2 calibration rows.
  Refresh the UI: the bench dashboard flashes red — **I2C Bus: OFFLINE**, **GPIO12 ADC: MISCONFIGURED**, **Calibration: MISSING**.

- **0:50–1:25 — Inspection:**
  In the Agendo Action Timeline, point to the card **"Gemini: Reconfigure power pins"**. Click **Inspect Diff** to expand the color-coded JSON diffs (red = old state, green = new state) showing the exact row mutations captured by `agendo_capture()`.

- **1:25–1:50 — The Undo:**
  Click **Revert**. Agendo runs compensating SQL in reverse `seq` order inside a single atomic transaction, flips the card to **REVERTED**, displays the measured revert time and `REVERT NOTICE`, and triggers the mocked serial re-push of the desired pin config back to `esp32-bench-01`. The bench dashboard turns green.

- **1:50–2:00 — Punchline:**
  *"Permissions stop the agent from doing what it's not allowed to do. Agendo fixes what it was allowed to do, when it gets it wrong."*
