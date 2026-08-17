"""
Fleet simulator — runs N vehicles on a common tick loop.
"""
from __future__ import annotations
import asyncio
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


TICK_HZ = 10   # 10 Hz ECU tick
STREAM_HZ = 2  # push telemetry to clients at 2 Hz


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


class FleetSimulator:
    def __init__(self, n_vehicles: int, time_scale: float = 1.0):
        self.time_scale = time_scale
        self.vehicles: dict[str, VehicleRuntime] = {}
        self.analyzer = Analyzer()
        self.frame_stream_subscribers: set[asyncio.Queue] = set()
        self.telemetry_subscribers: set[asyncio.Queue] = set()
        self._running = False
        self._build_fleet(n_vehicles)

    # ---------- Fleet construction ----------

    def _build_fleet(self, n: int):
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
        if "idle_rpm_target" in params:   e.idle_rpm_target   = max(500, min(1100, float(params["idle_rpm_target"])))
        if "rev_limit_rpm" in params:     e.rev_limit_rpm     = max(1800, min(2400, float(params["rev_limit_rpm"])))
        if "speed_governor_kph" in params:e.speed_governor_kph= max(60, min(140, float(params["speed_governor_kph"])))
        if "fuel_trim_pct" in params:     e.fuel_trim_pct     = max(-15, min(15, float(params["fuel_trim_pct"])))
        return True

    def apply_map(self, vid: str, which: str, matrix: list[list[float]]) -> bool:
        rt = self.vehicles.get(vid)
        if not rt: return False
        e = rt.vehicle.ecu
        if which == "fuel":     e.fuel_map = matrix
        elif which == "timing": e.timing_map = matrix
        elif which == "boost":  e.boost_map = matrix
        else: return False
        return True

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
        tick_dt = 1 / TICK_HZ * self.time_scale
        stream_every = TICK_HZ // STREAM_HZ
        counter = 0
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
                # 5. Aggregate DTCs from active faults
                active_dtcs = [f.dtc() for f in rt.faults.values() if f.dtc()]
                active_dtcs = [d for d in active_dtcs if d]
                rt.vehicle.set_active_dtcs(active_dtcs)
                # 6. Build J1939 frames
                snap = rt.vehicle.snapshot()
                rt.last_frames = build_frames(snap, source_address=0x00)

            counter += 1
            if counter % stream_every == 0:
                await self._broadcast_telemetry()
                await self._broadcast_frames()
                # Analyzer
                for rt in self.vehicles.values():
                    snap = rt.vehicle.snapshot()
                    self.analyzer.analyze(rt.vid, snap, rt.driver.snapshot(), rt.lat, rt.lng)

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0, 1 / TICK_HZ - elapsed))

    def stop(self):
        self._running = False

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
            "driver": rt.driver.snapshot(),
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
