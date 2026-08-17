"""
Rolling-window analyzer.

Three alert families:
  1) Predictive maintenance — trend detection on thermals, oil, boost, DPF, alternator
  2) Fuel theft — anomalous drops in fuel level
  3) Drowsiness — fatigue score thresholds + short-term lane departures

Each alert has: severity (info/warn/critical), category, title, detail, first_seen_ts.
Alerts are keyed by (vehicle_id, kind) so they update rather than duplicate.
"""
from __future__ import annotations
from collections import deque, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean
import time


ALERT_TTL_S = 120  # auto-clear an alert if underlying condition hasn't re-triggered in this long


@dataclass
class Alert:
    vehicle_id: str
    kind: str
    category: str       # "maintenance" | "security" | "safety"
    severity: str       # "info" | "warn" | "critical"
    title: str
    detail: str
    first_seen: float
    last_seen: float
    lat: float | None = None
    lng: float | None = None

    def to_dict(self):
        return {
            "vehicle_id": self.vehicle_id,
            "kind": self.kind,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "first_seen_iso": datetime.fromtimestamp(self.first_seen, tz=timezone.utc).isoformat(),
            "last_seen_iso": datetime.fromtimestamp(self.last_seen, tz=timezone.utc).isoformat(),
            "lat": self.lat, "lng": self.lng,
        }


class VehicleHistory:
    """Small rolling buffers per vehicle for trend analysis."""
    def __init__(self):
        # ~5 min at 2 Hz snapshot
        self.coolant = deque(maxlen=600)
        self.oil_pressure = deque(maxlen=600)
        self.boost = deque(maxlen=600)
        self.load = deque(maxlen=600)
        self.rpm = deque(maxlen=600)
        self.fuel_level = deque(maxlen=600)
        self.fuel_econ = deque(maxlen=600)
        self.speed = deque(maxlen=600)
        self.battery = deque(maxlen=600)
        self.ignition = deque(maxlen=600)
        self.timestamps = deque(maxlen=600)

    def push(self, ts: float, snap: dict):
        self.timestamps.append(ts)
        self.coolant.append(snap["coolant_temp_c"])
        self.oil_pressure.append(snap["engine_oil_pressure_kpa"])
        self.boost.append(snap["boost_pressure_kpa"])
        self.load.append(snap["engine_load_pct"])
        self.rpm.append(snap["engine_rpm"])
        self.fuel_level.append(snap["fuel_level_pct"])
        self.fuel_econ.append(snap["inst_fuel_economy_kmpl"])
        self.speed.append(snap["vehicle_speed_kph"])
        self.battery.append(snap["battery_voltage"])
        self.ignition.append(snap["ignition_on"])


class Analyzer:
    def __init__(self):
        self.history: dict[str, VehicleHistory] = defaultdict(VehicleHistory)
        self.alerts: dict[tuple[str, str], Alert] = {}

    def _touch_alert(self, key, factory) -> Alert:
        now = time.time()
        if key in self.alerts:
            a = self.alerts[key]
            a.last_seen = now
            return a
        a = factory(now)
        self.alerts[key] = a
        return a

    def analyze(self, vehicle_id: str, snap: dict, driver: dict,
                lat: float | None = None, lng: float | None = None) -> list[Alert]:
        now = time.time()
        hist = self.history[vehicle_id]
        hist.push(now, snap)

        emitted: list[Alert] = []

        # ---- 1. Coolant trend (thermostat / cooling) ----
        if len(hist.coolant) >= 240:
            window = list(hist.coolant)[-240:]
            load_window = list(hist.load)[-240:]
            # trend °C over last 2 min under steady moderate+ load
            if mean(load_window) > 25:
                delta = window[-1] - window[0]
                rate_per_hr = delta / (240 * 0.5 / 60) * 60  # samples span 2 min
                if rate_per_hr > 4 and window[-1] > 88:
                    def make(ts): return Alert(
                        vehicle_id, "coolant_trend", "maintenance",
                        "warn" if window[-1] < 100 else "critical",
                        "Coolant trending high under load",
                        f"+{rate_per_hr:.1f}°C/hr, currently {window[-1]:.1f}°C. "
                        "Likely thermostat or cooling loop degradation.",
                        ts, ts, lat, lng)
                    emitted.append(self._touch_alert((vehicle_id, "coolant_trend"), make))

        # ---- 2. Oil pressure decay ----
        if len(hist.oil_pressure) >= 240:
            op = list(hist.oil_pressure)[-240:]
            rpm_w = list(hist.rpm)[-240:]
            if mean(rpm_w) > 1000:
                delta = op[-1] - op[0]
                if delta < -25:
                    def make(ts): return Alert(
                        vehicle_id, "oil_pressure_decay", "maintenance",
                        "warn" if op[-1] > 200 else "critical",
                        "Oil pressure trending down at cruise RPM",
                        f"Δ {delta:.0f} kPa over 2 min, currently {op[-1]:.0f} kPa. "
                        "Investigate oil pump, bearings, or oil dilution.",
                        ts, ts, lat, lng)
                    emitted.append(self._touch_alert((vehicle_id, "oil_pressure_decay"), make))

        # ---- 3. Boost below expected ----
        if len(hist.boost) >= 60:
            bw = list(hist.boost)[-60:]
            lw = list(hist.load)[-60:]
            expected = 20 + 1.8 * mean(lw)
            actual = mean(bw)
            if mean(lw) > 40 and actual < expected * 0.6:
                def make(ts): return Alert(
                    vehicle_id, "boost_low", "maintenance", "warn",
                    "Boost pressure below expected for load",
                    f"Actual {actual:.0f} kPa vs expected ~{expected:.0f} kPa. "
                    "Possible turbo, actuator, or intake leak.",
                    ts, ts, lat, lng)
                emitted.append(self._touch_alert((vehicle_id, "boost_low"), make))

        # ---- 4. Alternator undervoltage ----
        if snap["battery_voltage"] < 24.0:
            def make(ts): return Alert(
                vehicle_id, "battery_low", "maintenance",
                "critical" if snap["battery_voltage"] < 22 else "warn",
                "Charging system undervoltage",
                f"Battery at {snap['battery_voltage']:.1f} V. Suspect alternator or wiring.",
                ts, ts, lat, lng)
            emitted.append(self._touch_alert((vehicle_id, "battery_low"), make))

        # ---- 5. Fuel theft — key check ----
        if len(hist.fuel_level) >= 20:
            recent = list(hist.fuel_level)[-20:]
            speeds = list(hist.speed)[-20:]
            ignitions = list(hist.ignition)[-20:]
            drop_pct = recent[0] - recent[-1]
            if drop_pct > 3 and max(speeds) < 2 and not any(ignitions):
                liters_dropped = drop_pct / 100 * 400  # assume 400L tank
                def make(ts): return Alert(
                    vehicle_id, "fuel_theft_parked", "security", "critical",
                    "Suspected fuel theft while parked",
                    f"-{liters_dropped:.0f} L in ~10 s, ignition off, vehicle stationary.",
                    ts, ts, lat, lng)
                emitted.append(self._touch_alert((vehicle_id, "fuel_theft_parked"), make))

            # Anomalous drop while moving vs expected consumption
            if len(hist.fuel_level) >= 60:
                fuel_60 = list(hist.fuel_level)[-60:]
                fuel_delta_l = (fuel_60[0] - fuel_60[-1]) / 100 * 400
                # Expected fuel used = integral of fuel rate over 30s (rough, since 2Hz)
                fr = list(snap.get("_recent_fuel_rate", [snap["fuel_rate_lph"]]))
                # rough expected: L/h * (30s/3600)
                expected_l = snap["fuel_rate_lph"] * (30 / 3600)
                if fuel_delta_l > max(0.6, expected_l * 3.5) and mean(list(hist.speed)[-60:]) > 5:
                    def make(ts): return Alert(
                        vehicle_id, "fuel_theft_moving", "security", "warn",
                        "Fuel level dropping faster than consumption",
                        f"Tank -{fuel_delta_l:.1f} L in 30s, expected ~{expected_l:.2f} L from injection. "
                        "Investigate line leak or sender fault.",
                        ts, ts, lat, lng)
                    emitted.append(self._touch_alert((vehicle_id, "fuel_theft_moving"), make))

        # ---- 6. Drowsiness ----
        score = driver.get("fatigue_score", 0)
        cont_h = driver.get("continuous_drive_h", 0)
        if score >= 75:
            def make(ts): return Alert(
                vehicle_id, "drowsy_critical", "safety", "critical",
                "Driver fatigue: critical",
                f"Fatigue score {score:.0f}/100, {cont_h:.1f} h continuous driving. "
                "Recommend immediate rest stop.",
                ts, ts, lat, lng)
            emitted.append(self._touch_alert((vehicle_id, "drowsy_critical"), make))
        elif score >= 55:
            def make(ts): return Alert(
                vehicle_id, "drowsy_warn", "safety", "warn",
                "Driver fatigue: elevated",
                f"Fatigue score {score:.0f}/100 after {cont_h:.1f} h. Monitor for rest break.",
                ts, ts, lat, lng)
            emitted.append(self._touch_alert((vehicle_id, "drowsy_warn"), make))

        # ---- 7. DPF regen needed (proxy: sustained low economy + high EGT) ----
        if snap["exhaust_gas_temp_c"] > 500 and len(hist.fuel_econ) >= 120:
            recent_econ = mean(list(hist.fuel_econ)[-120:])
            if 0 < recent_econ < 1.5:
                def make(ts): return Alert(
                    vehicle_id, "dpf_regen", "maintenance", "warn",
                    "DPF regeneration likely needed",
                    f"EGT {snap['exhaust_gas_temp_c']:.0f}°C, economy {recent_econ:.2f} km/L. "
                    "Schedule active regen at next stop.",
                    ts, ts, lat, lng)
                emitted.append(self._touch_alert((vehicle_id, "dpf_regen"), make))

        return emitted

    def all_alerts(self) -> list[dict]:
        # Expire stale
        now = time.time()
        expired = [k for k, a in self.alerts.items() if now - a.last_seen > ALERT_TTL_S]
        for k in expired:
            del self.alerts[k]
        return [a.to_dict() for a in sorted(self.alerts.values(),
                                             key=lambda a: (
                                                 {"critical": 0, "warn": 1, "info": 2}[a.severity],
                                                 -a.last_seen))]

    def clear_vehicle(self, vehicle_id: str):
        keys = [k for k in self.alerts if k[0] == vehicle_id]
        for k in keys:
            del self.alerts[k]
