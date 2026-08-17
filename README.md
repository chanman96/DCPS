# FleetTune

**An onboard cyber-physical system for real-time fleet management, predictive maintenance, and remote ECU optimization — powered by simulated J1939 telemetry.**

FleetTune is a locally-runnable simulator + ops console for a small commercial truck fleet. It generates real J1939 CAN frames from a physics-lite diesel ECU model, streams them into a browser dashboard over WebSockets, and closes the cyber-physical loop by letting the operator remotely retune the ECU (sliders, or by directly editing fuel / timing / boost maps) and watch the vehicle respond within ~2 seconds.

Built to demonstrate a Digital Cyber-Physical System with every step present: sense, communicate, decide, act, sense again.

---

## Install & run

Requires Python 3.10+ (uses PEP 604 union syntax).

```bash
pip install -r requirements.txt
python run.py                    # 8 vehicles, http://127.0.0.1:8000
python run.py --vehicles 15
python run.py --vehicles 3 --port 9000 --time-scale 5
```

Then open the URL printed on stdout. No build step, no npm, no external services — everything runs offline.

Flags:

| Flag           | Default   | Notes                                                       |
| -------------- | --------- | ----------------------------------------------------------- |
| `--vehicles`   | 8         | 1–100 simulated trucks                                      |
| `--host`       | 127.0.0.1 | Bind address                                                |
| `--port`       | 8000      | HTTP + WebSocket port                                       |
| `--time-scale` | 1.0       | >1 speeds the sim up — useful to trigger predictive alerts fast during demo |

---

## What's in the dashboard

Six tabs, one selected vehicle at a time (chosen from the left sidebar or by clicking a map marker):

1. **Fleet map** — every truck on a dark Leaflet map, colored by health status. Click to select.
2. **Vehicle detail** — 12 live gauges, four rolling-window trend charts (RPM/speed, thermals, fuel, boost/load), driver card with fatigue score + bar, active DTC list. Ignition toggle.
3. **CAN stream** — the live J1939 frames coming off the selected vehicle, one row per PGN per broadcast, showing 29-bit CAN ID / priority / PGN / name / source address / data hex. Pause to inspect a moment.
4. **ECU maps** — 4 tune sliders (idle RPM, rev limit, speed governor, fuel trim) plus **three editable 8×8 heatmaps** (fuel duty, timing advance, boost target). Click any cell, type a value, hit enter. Changes push into the ECU model on the next 100 ms tick.
5. **Alerts** — fleet-wide predictive alerts across three categories (maintenance / security / safety). Sorted by severity. Filter chips.
6. **Fault injection** — inject or clear any of 8 fault types on the selected vehicle. Each fault moves through **inactive → developing → active**. Predictive alerts should catch it during the developing phase, before the DTC fires in the active phase.

---

## The DCPS loop, mapped

| Step        | Where it lives in the code                                                                 |
| ----------- | ------------------------------------------------------------------------------------------ |
| Sense       | `ecu_model.Vehicle.tick()` produces coupled telemetry from throttle/load                    |
| Encode      | `j1939.build_frames()` wraps the snapshot in 11 real PGNs with correct SPN scaling         |
| Transport   | FastAPI WebSockets — `/ws/telemetry` (2 Hz fleet summary) and `/ws/frames` (2 Hz per-vehicle raw frames) |
| Decide      | `analyzer.Analyzer.analyze()` runs rolling-window rules per snapshot                        |
| Act         | REST endpoints `/api/tune`, `/api/map`, `/api/ignition`, `/api/fault/{inject,clear}`       |
| Sense again | The next tick immediately reflects the change; you see it in the charts within a beat      |

---

## Demo scripts (things worth showing)

Once running, pick a vehicle in the sidebar and try these — each takes under a minute.

### 1. Closed-loop ECU retune (the money shot)
- Open **Vehicle detail** to watch fuel rate and RPM.
- Open **ECU maps** in a second panel (or flip between tabs).
- Drag **Fuel trim** to `-8%`. Watch fuel-rate drop within 2 seconds.
- Click a mid-RPM / high-load cell in the fuel map, change 70 → 45, enter. Under load, fuel rate drops further — the operator has rewritten the ECU calibration from a browser.

### 2. Predictive maintenance beats the DTC
- On **Fault injection**, inject `Coolant overheat`.
- Bump `--time-scale 5` on launch to compress the 3-minute developing phase.
- Watch the coolant chart drift up. The **predictive alert** ("Coolant trending high under load, +X°C/hr") appears before coolant crosses 105°C. The **DTC** (SPN 110 FMI 0) fires later, once the fault has fully transitioned to active.

### 3. Fuel theft detection
- Turn ignition OFF on the vehicle detail panel.
- Inject the `Fuel siphon (parked)` fault.
- Within ~10 seconds, a critical alert: *"Suspected fuel theft while parked — −X L in ~10 s, ignition off, vehicle stationary,"* with GPS coordinates from the last known position.
- No DTC is raised — this fault is invisible to the vehicle's own diagnostics and only caught by fleet-side anomaly detection.

### 4. Drowsy driver
- Inject the `Drowsy driver` fault.
- The fatigue-score bar climbs from green through amber into red over 30–60 s (faster with `--time-scale 5`).
- Alerts escalate: `Driver fatigue: elevated` (warn, score ≥ 55) → `Driver fatigue: critical` (score ≥ 75). Lane-departure count also ticks up occasionally.

### 5. Full DTC path
- Inject `Cyl 1 injector misfire`. Faster develop time (~60 s at normal speed).
- Watch RPM jitter appear on the chart. Once active, `SPN 1323 FMI 5` appears in the DTC list on the vehicle-detail panel, and the DM1 frame in the CAN stream carries the code.

---

## J1939 PGN reference (what the simulator broadcasts)

| PGN     | Hex    | Name    | Priority | Period (ms) | Key SPNs                                          |
| ------- | ------ | ------- | -------- | ----------- | ------------------------------------------------- |
| 61443   | 0xF003 | EEC2    | 3        | 50          | 91 accel pedal, 92 engine load                    |
| 61444   | 0xF004 | EEC1    | 3        | 100         | 190 engine speed, 513 actual torque               |
| 65253   | 0xFEE5 | HOURS   | 6        | 1000        | 247 total engine hours                            |
| 65262   | 0xFEEE | ET1     | 6        | 1000        | 110 coolant temp, 174 fuel temp, 175 oil temp     |
| 65263   | 0xFEEF | EFL/P1  | 6        | 500         | 94 fuel delivery pressure, 100 oil pressure, 109 coolant pressure |
| 65265   | 0xFEF1 | CCVS1   | 6        | 100         | 84 wheel-based vehicle speed                      |
| 65266   | 0xFEF2 | LFE1    | 3        | 100         | 183 fuel rate, 184/185 fuel economy               |
| 65270   | 0xFEF6 | IC1     | 6        | 500         | 102 boost, 105 intake manifold temp, 173 EGT      |
| 65276   | 0xFEFC | DD      | 6        | 1000        | 96 fuel level, 97 washer fluid                    |
| 65217   | 0xFEC1 | VDHR    | 6        | 1000        | 917 total distance, 918 trip distance             |
| 65226   | 0xFECA | DM1     | 6        | 1000        | Active diagnostic trouble codes                   |

CAN ID format is the standard 29-bit extended identifier: `Priority(3) | Reserved(1) | Data Page(1) | PDU Format(8) | PDU Specific(8) | Source Address(8)`. For broadcast PGNs (PDU Format ≥ 240), PGN = (PF « 8) | PS. Source address is 0x00 (engine ECU) throughout.

---

## Project layout

```
fleettune/
├── requirements.txt
├── run.py                      # CLI entry point
├── README.md
└── fleettune/
    ├── __init__.py
    ├── main.py                 # FastAPI app: REST + WebSocket routes
    ├── simulator.py            # FleetSimulator: 10 Hz tick loop, 2 Hz broadcast
    ├── ecu_model.py            # EcuState, Vehicle, editable 8x8 fuel/timing/boost maps
    ├── driver_model.py         # Driver behavior + fatigue score composite
    ├── faults.py               # 8 injectable faults with developing/active phases
    ├── analyzer.py             # Rolling-window rules for 3 alert categories
    ├── routes.py               # Synthetic GPS loops around 15 city anchors
    ├── j1939.py                # PGN encoders + 29-bit CAN ID builder + DTC catalog
    └── static/
        ├── index.html          # Dashboard shell (6 tabs)
        ├── style.css           # Deep navy blueprint theme, amber accent
        └── app.js              # WebSocket handling, Leaflet, Chart.js, map editor
```

**Roughly 3,200 lines of code total.** No database, no external network calls at runtime, no framework beyond FastAPI and vanilla JS/HTML/CSS.

---

## Extending it

**Add a new PGN**: append a `PgnSpec` to `PGN_REGISTRY` in `j1939.py` with an encoder function that returns 8 bytes. It'll appear in the CAN stream automatically.

**Add a new fault**: subclass `Fault` in `faults.py` with a `_apply(dt, ecu)` method that mutates the ecu context dict, register it in `FAULT_TYPES` and `FAULT_LABELS`. The UI toggle appears with no frontend change.

**Add a new predictive rule**: add a block inside `Analyzer.analyze()` that reads from `hist` and calls `self._touch_alert(...)`. It'll show up in the Alerts tab automatically.

**Swap the map palette**: `heatColor()` in `app.js` — three color stops define the interpolation.

**Persist telemetry**: the current sim keeps only rolling in-memory windows; add a hook in `simulator._broadcast_telemetry()` to write JSON lines to disk or push to a real time-series DB.

---

## Notes on realism

The physics model is *plausible*, not accurate. Coolant equilibrium, fuel-rate scaling, and turbo response are tuned to move the way a real diesel would qualitatively — enough that alerts, tuning changes, and fault injections tell a coherent story on the dashboard — but this isn't a substitute for a real engine simulator.

The **J1939 encoding**, however, is real: SPN bit packing, scaling, offsets, and CAN ID assembly all follow the standard, so frames produced here will decode correctly against any J1939 tool.
