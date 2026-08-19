"""
Fleet simulator — runs N vehicles on a common tick loop.
"""
from __future__ import annotations
import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import random
import time

from .ecu_model import EcuState, Vehicle
from .driver_model import Driver, DRIVER_NAMES
from .faults import Fault, FAULT_TYPES
from .routes import generate_route, haversine_km, interpolate
from .j1939 import build_frames, J1939Frame
from .analyzer import Analyzer
from .excel_logger import ExcelLogger, LOG_INTERVAL_S


TICK_HZ = 10   # 10 Hz ECU tick
STREAM_HZ = 2  # push telemetry to clients at 2 Hz
LOG_TICKS = round(LOG_INTERVAL_S * TICK_HZ)   # excel log cadence, in sim ticks
FLUSH_EVERY_N_LOGS = 10                        # ~20s of sim time between disk flushes

# Harsh-event thresholds — roughly the ~0.3-0.4g deceleration/acceleration bands fleet
# telematics platforms use, expressed as kph/s since that's the unit this model already tracks.
HARSH_BRAKE_KPH_S = -11.0
HARSH_ACCEL_KPH_S = 11.0
HARSH_EVENT_WINDOW_S = 600   # "recent" harsh events considered for the driver score (10 min)


@dataclass
class VehicleRuntime:
    vid: str
    label: str
    vehicle: Vehicle
    driver: Driver
    route: dict
    # Position along the route
    segment: int = 0
    segment_progress: float = 0.0
    lat: float = 0.0
    lng: float = 0.0
    heading_deg: float = 0.0
    faults: dict[str, Fault] = field(default_factory=dict)
    # Ephemeral fault interop dict
    _ecu_ctx: dict = field(default_factory=dict)
    last_frames: list[J1939Frame] = field(default_factory=list)
    # Driving-behavior tracking (harsh events, idling, ECU-tune change log)
    prev_speed_kph: float = 0.0
    harsh_brake_ts: deque = field(default_factory=lambda: deque(maxlen=200))
    harsh_accel_ts: deque = field(default_factory=lambda: deque(maxlen=200))
    idle_seconds: float = 0.0
    idle_fuel_l: float = 0.0
    tune_events: deque = field(default_factory=lambda: deque(maxlen=25))


class FleetSimulator:
    def __init__(self, n_vehicles: int, time_scale: float = 1.0):
        self.time_scale = time_scale
        self.n_vehicles = n_vehicles
        self.vehicles: dict[str, VehicleRuntime] = {}
        self.analyzer = Analyzer()
        self.excel_logger = ExcelLogger()
        self.frame_stream_subscribers: set[asyncio.Queue] = set()
        self.telemetry_subscribers: set[asyncio.Queue] = set()
        self._running = False
        self._task: asyncio.Task | None = None
        self._log_flush_counter = 0
        # Build initial fleet
        self._build_fleet(n_vehicles)

    # ---------- Runtime control ----------

    def start(self) -> bool:
        """Start the simulator run loop in the event loop. Returns True if started, False if already running."""
        if self._running:
            return False
        # create_task must be called from within an event loop; caller is expected to call from async context
        self._running = True
        try:
            self._task = asyncio.create_task(self.run())
        except RuntimeError:
            # Not in an event loop — leave _running flag set and rely on caller to schedule run
            self._task = None
        return True

    def is_running(self) -> bool:
        return bool(self._running)

    def reconfigure(self, n_vehicles: int | None = None, time_scale: float | None = None):
        """Adjust fleet size and time scale at runtime. Replaces the vehicle set when n_vehicles is provided."""
        if n_vehicles is not None:
            self.n_vehicles = n_vehicles
            self._build_fleet(n_vehicles)
        if time_scale is not None:
            self.time_scale = time_scale

    def get_config(self) -> dict:
        return {
            "n_vehicles": getattr(self, "n_vehicles", None),
            "time_scale": getattr(self, "time_scale", None),
            "running": self._running,
        }

    # ---------- Fleet construction ----------

    def _build_fleet(self, n: int):
        # Replace the vehicle dictionary so reconfiguration replaces the fleet instead of appending
        self.vehicles = {}
        rng = random.Random(42)
        profiles = ["eco", "normal", "normal", "aggressive"]
        for i in range(n):
            vid = f"TRK-{i+1:03d}"
            route = generate_route(seed=1000 + i)
            ecu_state = EcuState(
                fuel_level_pct=rng.uniform(45, 92),
                engine_total_hours=rng.uniform(2000, 25000),
                total_vehicle_distance_km=rng.uniform(50000, 900000),
            )
            vehicle = Vehicle(vid, ecu_state)
            driver = Driver(
                name=DRIVER_NAMES[i % len(DRIVER_NAMES)],
                profile=rng.choice(profiles),
                continuous_drive_s=rng.uniform(0, 3600 * 3),
            )
            rt = VehicleRuntime(
                vid=vid,
                label=f"{vid} · {route['region']}",
                vehicle=vehicle, driver=driver, route=route,
                lat=route["waypoints"][0][0], lng=route["waypoints"][0][1],
            )
            # Initialize each fault type in INACTIVE state
            for kind, cls in FAULT_TYPES.items():
                rt.faults[kind] = cls()
            self.vehicles[vid] = rt

    # ---------- Public control ----------

    def list_vehicles(self) -> list[dict]:
        return [{
            "id": rt.vid, "label": rt.label,
            "region": rt.route["region"],
            "driver": rt.driver.name,
            "profile": rt.driver.profile,
        } for rt in self.vehicles.values()]

    def inject_fault(self, vid: str, kind: str) -> bool:
        rt = self.vehicles.get(vid)
        if not rt or kind not in rt.faults: return False
        rt.faults[kind].start_developing()
        return True

    def clear_fault(self, vid: str, kind: str) -> bool:
        rt = self.vehicles.get(vid)
        if not rt or kind not in rt.faults: return False
        rt.faults[kind].clear()
        # Reset biases the fault applied
        for k in ("coolant_bias_c", "oil_pressure_bias_kpa", "boost_loss_frac",
                  "rpm_noise", "torque_loss_pct", "battery_voltage_bias_v",
                  "back_pressure_bias_kpa", "fuel_econ_penalty", "soot_load_g",
                  "fatigue_push"):
            rt._ecu_ctx.pop(k, None)
        return True

    def set_ignition(self, vid: str, on: bool) -> bool:
        rt = self.vehicles.get(vid)
        if not rt: return False
        rt.vehicle.ecu.ignition_on = on
        return True

    def apply_tune(self, vid: str, params: dict) -> bool:
        rt = self.vehicles.get(vid)
        if not rt: return False
        e = rt.vehicle.ecu
        changes = []
        if "idle_rpm_target" in params:
            old = e.idle_rpm_target
            e.idle_rpm_target = max(500, min(1100, float(params["idle_rpm_target"])))
            changes.append(f"idle RPM {old:.0f}→{e.idle_rpm_target:.0f}")
        if "rev_limit_rpm" in params:
            old = e.rev_limit_rpm
            e.rev_limit_rpm = max(1800, min(2400, float(params["rev_limit_rpm"])))
            changes.append(f"rev limit {old:.0f}→{e.rev_limit_rpm:.0f}")
        if "speed_governor_kph" in params:
            old = e.speed_governor_kph
            e.speed_governor_kph = max(60, min(140, float(params["speed_governor_kph"])))
            changes.append(f"governor {old:.0f}→{e.speed_governor_kph:.0f} kph")
        if "fuel_trim_pct" in params:
            old = e.fuel_trim_pct
            e.fuel_trim_pct = max(-15, min(15, float(params["fuel_trim_pct"])))
            changes.append(f"fuel trim {old:+.1f}→{e.fuel_trim_pct:+.1f}%")
        if changes:
            self._log_tune_event(rt, "tune", "; ".join(changes))
        return True

    def apply_map(self, vid: str, which: str, matrix: list[list[float]]) -> bool:
        rt = self.vehicles.get(vid)
        if not rt: return False
        e = rt.vehicle.ecu
        old = {"fuel": e.fuel_map, "timing": e.timing_map, "boost": e.boost_map}.get(which)
        if old is None: return False
        changed = []
        for r in range(len(matrix)):
            for c in range(len(matrix[r])):
                ov = old[r][c] if r < len(old) and c < len(old[r]) else None
                nv = matrix[r][c]
                if ov is not None and abs(nv - ov) > 0.05:
                    changed.append((r, c, ov, nv))
        if which == "fuel":     e.fuel_map = matrix
        elif which == "timing": e.timing_map = matrix
        elif which == "boost":  e.boost_map = matrix
        if changed:
            r, c, ov, nv = changed[0]
            extra = f" (+{len(changed) - 1} more cells)" if len(changed) > 1 else ""
            self._log_tune_event(rt, f"{which}_map",
                                  f"{which} map RPM-bin {c}/Load-bin {r}: {ov:.1f}→{nv:.1f}{extra}")
        return True

    def _log_tune_event(self, rt: VehicleRuntime, field: str, detail: str):
        rt.tune_events.append({
            "ts": time.time(),
            "field": field,
            "detail": detail,
            "fatigue_at_change": round(rt.driver.fatigue_score, 1),
            "pedal_at_change": round(rt.vehicle.ecu.accel_pedal_pct, 1),
            "speed_at_change": round(rt.vehicle.ecu.vehicle_speed_kph, 1),
        })

    def get_tune_events(self, vid: str) -> list[dict]:
        rt = self.vehicles.get(vid)
        if not rt: return []
        return list(rt.tune_events)

    def get_maps(self, vid: str) -> dict | None:
        rt = self.vehicles.get(vid)
        if not rt: return None
        e = rt.vehicle.ecu
        return {"fuel": e.fuel_map, "timing": e.timing_map, "boost": e.boost_map,
                "tune": {"idle_rpm_target": e.idle_rpm_target,
                          "rev_limit_rpm": e.rev_limit_rpm,
                          "speed_governor_kph": e.speed_governor_kph,
                          "fuel_trim_pct": e.fuel_trim_pct}}

    def fault_status(self, vid: str) -> dict:
        rt = self.vehicles.get(vid)
        if not rt: return {}
        return {kind: f.phase.value for kind, f in rt.faults.items()}

    # ---------- Route follower ----------

    def _advance_position(self, rt: VehicleRuntime, dt: float):
        wps = rt.route["waypoints"]
        # Target speed varies by profile & random slowdowns
        base = 55 * rt.driver.target_speed_factor()
        target = base + random.uniform(-8, 8)
        brake = 0
        # Random micro-stops (traffic)
        if random.random() < 0.001:
            brake = 0.7
            target = 0
        rt.vehicle.set_driver_targets(target_speed_kph=target, brake=brake)

        # Advance along waypoints based on actual speed
        km_this_tick = rt.vehicle.ecu.vehicle_speed_kph * dt / 3600
        remaining = km_this_tick
        while remaining > 0 and rt.segment < len(wps) - 1:
            a = wps[rt.segment]
            b = wps[rt.segment + 1]
            seg_km = haversine_km(a, b)
            seg_remaining_km = seg_km * (1 - rt.segment_progress)
            if remaining < seg_remaining_km and seg_km > 0:
                rt.segment_progress += remaining / seg_km
                remaining = 0
            else:
                remaining -= seg_remaining_km
                rt.segment += 1
                rt.segment_progress = 0
        if rt.segment >= len(wps) - 1:
            rt.segment = 0
            rt.segment_progress = 0
        a = wps[rt.segment]
        b = wps[min(rt.segment + 1, len(wps) - 1)]
        rt.lat, rt.lng = interpolate(a, b, rt.segment_progress)
        import math
        rt.heading_deg = (math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) + 360) % 360

    # ---------- Tick ----------

    async def run(self):
        self._running = True
        self.excel_logger.start()
        tick_dt = 1 / TICK_HZ * self.time_scale
        stream_every = TICK_HZ // STREAM_HZ
        counter = 0
        try:
            while self._running:
                t0 = time.monotonic()
                now = datetime.now(timezone.utc)
                for rt in self.vehicles.values():
                    # 1. Route -> driver targets
                    self._advance_position(rt, tick_dt)
                    # 2. Faults tick (mutate ecu context)
                    for f in rt.faults.values():
                        f.tick(tick_dt, rt._ecu_ctx)
                    # 3. ECU tick
                    rt.vehicle.tick(tick_dt, rt._ecu_ctx)
                    # 4. Driver tick
                    rt.driver.tick(tick_dt, rt._ecu_ctx, now)
                    # 5. Driving-behavior tracking (harsh events, idling)
                    self._track_behavior(rt, tick_dt)
                    # 6. Aggregate DTCs from active faults
                    active_dtcs = [f.dtc() for f in rt.faults.values() if f.dtc()]
                    active_dtcs = [d for d in active_dtcs if d]
                    rt.vehicle.set_active_dtcs(active_dtcs)
                    # 7. Build J1939 frames
                    snap = rt.vehicle.snapshot()
                    rt.last_frames = build_frames(snap, source_address=0x00)

                counter += 1
                if counter % stream_every == 0:
                    await self._broadcast_telemetry()
                    await self._broadcast_frames()
                    # Analyzer
                    for rt in self.vehicles.values():
                        snap = rt.vehicle.snapshot()
                        self.analyzer.analyze(rt.vid, snap, self._behavior_snapshot(rt), rt.lat, rt.lng)
                if counter % LOG_TICKS == 0:
                    self._log_excel_snapshot(now)

                elapsed = time.monotonic() - t0
                await asyncio.sleep(max(0, 1 / TICK_HZ - elapsed))
        finally:
            self.excel_logger.stop()

    def stop(self):
        self._running = False

    # ---------- Driving behavior tracking ----------

    def _track_behavior(self, rt: VehicleRuntime, dt: float):
        e = rt.vehicle.ecu
        speed = e.vehicle_speed_kph
        if dt > 0:
            d_speed = (speed - rt.prev_speed_kph) / dt   # kph/s, a rough deceleration/accel proxy
            if d_speed <= HARSH_BRAKE_KPH_S and rt.prev_speed_kph > 5:
                rt.harsh_brake_ts.append(time.time())
            elif d_speed >= HARSH_ACCEL_KPH_S and e.accel_pedal_pct > 70:
                rt.harsh_accel_ts.append(time.time())
        rt.prev_speed_kph = speed

        if e.ignition_on and speed < 2:
            rt.idle_seconds += dt
            rt.idle_fuel_l += e.fuel_rate_lph * dt / 3600

    def _behavior_snapshot(self, rt: VehicleRuntime) -> dict:
        """Driver snapshot extended with harsh-event / idle / composite driver-score metrics."""
        now = time.time()
        recent_brakes = [t for t in rt.harsh_brake_ts if now - t <= HARSH_EVENT_WINDOW_S]
        recent_accels = [t for t in rt.harsh_accel_ts if now - t <= HARSH_EVENT_WINDOW_S]
        score = 100.0
        score -= rt.driver.fatigue_score * 0.35
        score -= min(30, (len(recent_brakes) + len(recent_accels)) * 3)
        drive_s = max(1.0, rt.driver.continuous_drive_s)
        idle_frac = rt.idle_seconds / (rt.idle_seconds + drive_s)
        score -= min(15, idle_frac * 100 * 0.3)
        d = rt.driver.snapshot()
        d.update({
            "driver_score": max(0, min(100, round(score, 1))),
            "harsh_brake_count": len(recent_brakes),
            "harsh_accel_count": len(recent_accels),
            "idle_minutes": round(rt.idle_seconds / 60, 2),
            "idle_fuel_l": round(rt.idle_fuel_l, 2),
        })
        return d

    # ---------- Excel telemetry logging ----------

    def _log_excel_snapshot(self, now: datetime):
        if not self.excel_logger.is_running():
            return
        for rt in self.vehicles.values():
            snap = rt.vehicle.snapshot()
            beh = self._behavior_snapshot(rt)
            row = [
                now.isoformat(), rt.vid, rt.driver.name, rt.driver.profile,
                round(rt.lat, 6), round(rt.lng, 6), round(rt.heading_deg, 1),
                round(snap["engine_rpm"], 1), snap["current_gear"], round(snap["vehicle_speed_kph"], 1),
                round(snap["engine_load_pct"], 1), round(snap["accel_pedal_pct"], 1),
                round(snap["coolant_temp_c"], 1), round(snap["oil_temp_c"], 1),
                round(snap["fuel_temp_c"], 1), round(snap["intake_manifold_temp_c"], 1),
                round(snap["exhaust_gas_temp_c"], 1), round(snap["boost_pressure_kpa"], 1),
                round(snap["air_inlet_pressure_kpa"], 1),
                round(snap["fuel_delivery_pressure_kpa"], 1), round(snap["engine_oil_pressure_kpa"], 1),
                round(snap["engine_oil_level_pct"], 1),
                round(snap["fuel_rate_lph"], 2), round(snap["fuel_economy_kmpl"], 2),
                round(snap["inst_fuel_economy_kmpl"], 2),
                round(snap["fuel_level_pct"], 1), round(snap["total_vehicle_distance_km"], 2),
                round(snap["trip_distance_km"], 2),
                round(snap["battery_voltage"], 2), snap["ignition_on"], len(snap["active_dtcs"]),
                beh["fatigue_score"], beh["continuous_drive_h"], beh["driver_score"],
                beh["harsh_brake_count"], beh["harsh_accel_count"],
                beh["idle_minutes"], beh["idle_fuel_l"],
            ]
            self.excel_logger.log_snapshot(rt.vid, row)
        self._log_flush_counter += 1
        if self._log_flush_counter % FLUSH_EVERY_N_LOGS == 0:
            self.excel_logger.flush()

    # ---------- Streaming ----------

    async def _broadcast_telemetry(self):
        payload = {"type": "telemetry", "ts": time.time(), "vehicles": [
            self._vehicle_summary(rt) for rt in self.vehicles.values()
        ], "alerts": self.analyzer.all_alerts()}
        for q in list(self.telemetry_subscribers):
            try: q.put_nowait(payload)
            except asyncio.QueueFull: pass

    async def _broadcast_frames(self):
        for rt in self.vehicles.values():
            payload = {
                "type": "frames",
                "vehicle_id": rt.vid,
                "ts": time.time(),
                "frames": [{
                    "can_id": f.can_id_hex, "pgn": f.pgn,
                    "priority": f.priority, "sa": f.source_address,
                    "data": f.data_hex,
                    "name": _pgn_name(f.pgn),
                } for f in rt.last_frames],
            }
            for q in list(self.frame_stream_subscribers):
                try: q.put_nowait(payload)
                except asyncio.QueueFull: pass

    def _vehicle_summary(self, rt: VehicleRuntime) -> dict:
        snap = rt.vehicle.snapshot()
        # Truncate/round for wire
        rounded = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in snap.items()}
        return {
            "id": rt.vid, "label": rt.label,
            "lat": rt.lat, "lng": rt.lng, "heading_deg": rt.heading_deg,
            "telemetry": rounded,
            "driver": self._behavior_snapshot(rt),
            "faults": self.fault_status(rt.vid),
        }

    # ---------- Subscribers ----------

    def subscribe_telemetry(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self.telemetry_subscribers.add(q)
        return q

    def unsubscribe_telemetry(self, q):
        self.telemetry_subscribers.discard(q)

    def subscribe_frames(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self.frame_stream_subscribers.add(q)
        return q

    def unsubscribe_frames(self, q):
        self.frame_stream_subscribers.discard(q)


def _pgn_name(pgn: int) -> str:
    from .j1939 import PGN_REGISTRY
    for s in PGN_REGISTRY:
        if s.pgn == pgn: return s.name
    return "?"
