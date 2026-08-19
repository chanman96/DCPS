"""
Physics-lite ECU model for a Class-8 diesel truck.

Produces coupled, plausible telemetry:
  - Throttle -> target RPM (governed by rev limit and idle floor)
  - RPM + gear -> vehicle speed
  - Load and RPM look up fuel injection from an 8x8 FUEL MAP (editable)
  - Timing and boost also come from 8x8 maps (editable) - "ECU map change on the fly"
  - Load + RPM drive coolant/oil/EGT toward equilibrium with thermal inertia

This is intentionally not a Ricardo-grade simulation. It's a demo model that
produces the right shapes, moves the right way when you edit the maps, and
generates telemetry that decodes correctly against real J1939 tools.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import math
import random


MAP_SIZE = 8

# ---------- Default ECU maps (RPM index x LOAD index) ----------

def default_fuel_map() -> list[list[float]]:
    """Fuel injection duty [%] as a function of RPM and load.
    Rows = load bins (0..7 = 0..100% engine load)
    Cols = RPM bins (0..7 = 600..2200 rpm)
    """
    rows = []
    for load_i in range(MAP_SIZE):
        load = load_i / (MAP_SIZE - 1)
        row = []
        for rpm_i in range(MAP_SIZE):
            rpm_frac = rpm_i / (MAP_SIZE - 1)
            # Rich in mid-RPM high-load, lean at idle low-load
            val = 20 + 55 * load + 15 * rpm_frac - 10 * (rpm_frac - 0.5)**2
            row.append(round(val, 1))
        rows.append(row)
    return rows


def default_timing_map() -> list[list[float]]:
    """Injection timing advance [°BTDC]. Higher = more torque + more NOx + risk of knock."""
    rows = []
    for load_i in range(MAP_SIZE):
        load = load_i / (MAP_SIZE - 1)
        row = []
        for rpm_i in range(MAP_SIZE):
            rpm_frac = rpm_i / (MAP_SIZE - 1)
            val = 6 + 10 * rpm_frac - 3 * load
            row.append(round(val, 1))
        rows.append(row)
    return rows


def default_boost_map() -> list[list[float]]:
    """Boost target [kPa]. Turbo output that the VGT/wastegate aims for."""
    rows = []
    for load_i in range(MAP_SIZE):
        load = load_i / (MAP_SIZE - 1)
        row = []
        for rpm_i in range(MAP_SIZE):
            rpm_frac = rpm_i / (MAP_SIZE - 1)
            val = 20 + 180 * load * (0.4 + 0.6 * rpm_frac)
            row.append(round(val, 1))
        rows.append(row)
    return rows


# ---------- Transmission (digital-twin gear model) ----------
#
# Automated 10-speed Class-8 gearbox, modeled as a speed-banded gear selector.
# Gear is a *derived* readout (like a real TCU broadcasting SPN 523 over J1939):
# it doesn't feed back into the RPM/speed physics loop, so it can't destabilize
# the rest of the model — it only produces a brief torque-interruption dip in
# engine load while a shift is "in flight", which ripples visibly into fuel/boost.

GEAR_MAX_KPH = [10, 16, 23, 31, 41, 53, 67, 82, 97, 10**6]  # upper speed bound per gear (1..10)
SHIFT_DURATION_S = 0.6     # clutch/AMT torque-interruption window
SHIFT_COOLDOWN_S = 1.0     # minimum time between shifts (prevents gear hunting)


def _gear_for_speed(speed_kph: float) -> int:
    """0 = Neutral (stationary), 1..10 = forward gears."""
    if speed_kph < 1.5:
        return 0
    for gear, cap in enumerate(GEAR_MAX_KPH, start=1):
        if speed_kph <= cap:
            return gear
    return 10


def _bin_indices(rpm: float, load_pct: float) -> tuple[int, int]:
    """Nearest (load, rpm) bin for a given engine state — same grid as the calibration maps,
    but a single nearest cell rather than _map_lookup's bilinear blend, since this feeds a
    dwell-time histogram rather than an interpolated readout."""
    rpm_norm = max(0, min(1, (rpm - 600) / 1600)) * (MAP_SIZE - 1)
    load_norm = max(0, min(1, load_pct / 100)) * (MAP_SIZE - 1)
    return int(round(load_norm)), int(round(rpm_norm))


USAGE_DECAY_TAU_S = 180.0  # recent-weighted so the heatmap tracks current driving, not all-session history


def _map_lookup(m: list[list[float]], rpm: float, load_pct: float) -> float:
    """Bilinear lookup on an 8x8 map. RPM in [600, 2200], load in [0, 100]."""
    rpm_norm = max(0, min(1, (rpm - 600) / 1600)) * (MAP_SIZE - 1)
    load_norm = max(0, min(1, load_pct / 100)) * (MAP_SIZE - 1)
    r0 = int(math.floor(rpm_norm)); r1 = min(MAP_SIZE - 1, r0 + 1)
    l0 = int(math.floor(load_norm)); l1 = min(MAP_SIZE - 1, l0 + 1)
    fr = rpm_norm - r0; fl = load_norm - l0
    a = m[l0][r0] * (1 - fr) + m[l0][r1] * fr
    b = m[l1][r0] * (1 - fr) + m[l1][r1] * fr
    return a * (1 - fl) + b * fl


# ---------- ECU snapshot / state ----------

@dataclass
class EcuState:
    # Live values (these become telemetry)
    engine_rpm: float = 750
    vehicle_speed_kph: float = 0
    engine_load_pct: float = 5
    accel_pedal_pct: float = 0
    coolant_temp_c: float = 40
    oil_temp_c: float = 40
    fuel_temp_c: float = 30
    intake_manifold_temp_c: float = 25
    exhaust_gas_temp_c: float = 150
    boost_pressure_kpa: float = 0
    air_inlet_pressure_kpa: float = 100
    air_filter_diff_pressure_kpa: float = 1.5
    fuel_delivery_pressure_kpa: float = 450
    engine_oil_pressure_kpa: float = 350
    engine_oil_level_pct: float = 92
    engine_oil_filter_diff_pressure_kpa: float = 40
    crankcase_pressure_kpa: float = 1.0
    coolant_pressure_kpa: float = 110
    coolant_level_pct: float = 95
    fuel_rate_lph: float = 0.6
    fuel_economy_kmpl: float = 0
    inst_fuel_economy_kmpl: float = 0
    engine_throttle_valve_pos_pct: float = 8
    fuel_level_pct: float = 78
    fuel_level_2_pct: float = 78
    washer_fluid_pct: float = 60
    fuel_filter_diff_pressure_kpa: float = 12
    cab_interior_temp_c: float = 26
    engine_total_hours: float = 12580
    total_vehicle_distance_km: float = 452301
    trip_distance_km: float = 0
    battery_voltage: float = 27.8
    ignition_on: bool = True

    # Transmission (digital-twin gear readout)
    current_gear: int = 0
    gear_shifting: bool = False

    # Aggregated fuel stats
    fuel_capacity_l: float = 400
    fuel_used_l_since_start: float = 0

    # Editable ECU maps
    fuel_map: list[list[float]] = field(default_factory=default_fuel_map)
    timing_map: list[list[float]] = field(default_factory=default_timing_map)
    boost_map: list[list[float]] = field(default_factory=default_boost_map)

    # Tunable scalars
    idle_rpm_target: float = 750
    rev_limit_rpm: float = 2200
    speed_governor_kph: float = 105
    fuel_trim_pct: float = 0     # global +/- fuel scale

    # Internal working / fault biases
    _fault_biases: dict = field(default_factory=dict)
    # (populated by fault._apply via ecu dict — mirrored below)


class Vehicle:
    def __init__(self, vid: str, ecu: EcuState):
        self.vid = vid
        self.ecu = ecu
        self.rng = random.Random(hash(vid) & 0xFFFFFFFF)
        # Simulated driver input (target throttle 0..1)
        self._driver_throttle = 0.0
        self._brake = 0.0
        self._active_dtcs: list[dict] = []
        # Rolling fuel-used for economy
        self._fuel_econ_window_l = 0
        self._fuel_econ_window_km = 0
        self._econ_reset_s = 0
        # Transmission shift state
        self._gear_target = 0
        self._gear_shift_timer = 0.0
        self._gear_cooldown = 0.0
        # Fuel-map derived power factor (ECU tuning -> driver throttle response)
        self.power_factor = 1.0
        # Driver usage heatmap: recent-weighted dwell time per RPM/load bin, same grid as
        # the calibration maps — shows which cells this specific driver actually operates in.
        self.usage_seconds: list[list[float]] = [[0.0] * MAP_SIZE for _ in range(MAP_SIZE)]

    # ---- Driver inputs (called by Simulator with route info) ----

    def set_driver_targets(self, target_speed_kph: float, brake: float):
        self._brake = max(0, min(1, brake))
        # Convert target speed -> throttle via simple PI on error
        err = target_speed_kph - self.ecu.vehicle_speed_kph
        self._driver_throttle = max(0, min(1, 0.25 + err * 0.02))
        if brake > 0.05:
            self._driver_throttle = 0

    # ---- ECU tick ----

    def tick(self, dt: float, ecu_dict: dict):
        """dt in seconds. ecu_dict is a mutable dict used for fault interop; we sync back."""
        e = self.ecu
        # Pull fault biases in
        e._fault_biases = {
            k: v for k, v in ecu_dict.items()
            if k not in ("vehicle_speed_kph", "ignition_on")
        }
        rpm_noise = ecu_dict.get("rpm_noise", 0)
        torque_loss = ecu_dict.get("torque_loss_pct", 0)
        boost_loss = ecu_dict.get("boost_loss_frac", 0)
        coolant_bias = ecu_dict.get("coolant_bias_c", 0)
        oil_pressure_bias = ecu_dict.get("oil_pressure_bias_kpa", 0)
        battery_bias = ecu_dict.get("battery_voltage_bias_v", 0)
        fuel_siphon = ecu_dict.get("fuel_siphon_liters", 0)
        econ_penalty = ecu_dict.get("fuel_econ_penalty", 0)

        if not e.ignition_on:
            # Engine off: decay everything
            e.engine_rpm = max(0, e.engine_rpm - 200 * dt)
            e.vehicle_speed_kph = max(0, e.vehicle_speed_kph - 25 * dt)
            e.engine_load_pct = 0
            e.accel_pedal_pct = 0
            e.fuel_rate_lph = 0
            e.current_gear = 0
            e.gear_shifting = False
            self._decay_thermals(dt)
            self._apply_common_effects(coolant_bias, oil_pressure_bias, battery_bias, fuel_siphon)
            # A tapped line / siphon still drains the tank while parked with ignition off —
            # this branch used to skip that entirely, silently no-opping the fault.
            if fuel_siphon:
                tank_liters = e.fuel_level_pct / 100 * e.fuel_capacity_l
                tank_liters = max(0, tank_liters - fuel_siphon)
                e.fuel_level_pct = tank_liters / e.fuel_capacity_l * 100
                e.fuel_level_2_pct = e.fuel_level_pct
                ecu_dict["fuel_siphon_liters"] = 0
            self._sync_out(ecu_dict)
            return

        # ---- ECU tuning -> driver throttle response ("act" step feeding back onto the driver) ----
        # A leaner/detuned fuel map makes less torque available per unit throttle, so for the
        # same target speed the driver has to hold more pedal for longer to get there. That shows
        # up downstream as pedal position pinned high and speed variance the driver model reads
        # as fatigue — this is the closed loop the ECU-maps tab visualizes.
        avg_fuel_duty = sum(sum(row) for row in e.fuel_map) / (MAP_SIZE * MAP_SIZE)
        self.power_factor = max(0.55, min(1.3, avg_fuel_duty / 53.9))

        # ---- RPM/speed dynamics ----
        target_rpm = min(
            e.rev_limit_rpm,
            e.idle_rpm_target + self._driver_throttle * (min(e.rev_limit_rpm, 2200) - e.idle_rpm_target) * self.power_factor,
        )
        # First-order tracking
        e.engine_rpm += (target_rpm - e.engine_rpm) * min(1, dt / 0.4)
        e.engine_rpm += self.rng.gauss(0, rpm_noise * 0.3)
        e.engine_rpm = max(0, e.engine_rpm)

        # Speed: derived from RPM (simplified single ratio at highway)
        gear_ratio = 0.037   # kph per rpm at cruise gear
        target_speed = min(e.speed_governor_kph, e.engine_rpm * gear_ratio - 8 * self._brake * 5)
        e.vehicle_speed_kph += (target_speed - e.vehicle_speed_kph) * min(1, dt / 1.5)
        e.vehicle_speed_kph = max(0, e.vehicle_speed_kph - self._brake * 20 * dt)

        # ---- Transmission: automated shift, derived from road speed ----
        self._gear_cooldown = max(0, self._gear_cooldown - dt)
        target_gear = _gear_for_speed(e.vehicle_speed_kph)
        if self._gear_shift_timer > 0:
            self._gear_shift_timer = max(0, self._gear_shift_timer - dt)
            if self._gear_shift_timer == 0:
                e.current_gear = self._gear_target
        elif target_gear != e.current_gear and self._gear_cooldown <= 0:
            self._gear_target = target_gear
            self._gear_shift_timer = SHIFT_DURATION_S
            self._gear_cooldown = SHIFT_COOLDOWN_S
        e.gear_shifting = self._gear_shift_timer > 0

        # ---- Engine load ----
        rpm_frac = max(0, (e.engine_rpm - e.idle_rpm_target) / (e.rev_limit_rpm - e.idle_rpm_target))
        raw_load = 10 + 90 * (self._driver_throttle * (0.6 + 0.4 * rpm_frac))
        e.engine_load_pct += (raw_load - e.engine_load_pct) * min(1, dt / 0.8)
        if e.gear_shifting:
            e.engine_load_pct *= 0.4   # clutch/AMT torque interruption while a shift is in flight
        e.engine_load_pct = max(0, min(100, e.engine_load_pct - torque_loss))
        e.accel_pedal_pct = self._driver_throttle * 100
        e.engine_throttle_valve_pos_pct = e.accel_pedal_pct * 0.9

        # ---- Driver usage heatmap ----
        decay = math.exp(-dt / USAGE_DECAY_TAU_S)
        for row in self.usage_seconds:
            for i in range(MAP_SIZE):
                row[i] *= decay
        li, ri = _bin_indices(e.engine_rpm, e.engine_load_pct)
        self.usage_seconds[li][ri] += dt

        # ---- ECU MAP LOOKUPS ("act" step visible here) ----
        fuel_duty = _map_lookup(e.fuel_map, e.engine_rpm, e.engine_load_pct)
        boost_target = _map_lookup(e.boost_map, e.engine_rpm, e.engine_load_pct)
        timing = _map_lookup(e.timing_map, e.engine_rpm, e.engine_load_pct)  # informational

        fuel_duty *= (1 + e.fuel_trim_pct / 100)
        # Fuel rate (L/h) from duty and RPM
        e.fuel_rate_lph = max(0.3, fuel_duty * e.engine_rpm * 4e-5 * (1 + econ_penalty))
        # Boost with turbo loss
        boost_actual = boost_target * (1 - boost_loss)
        e.boost_pressure_kpa += (boost_actual - e.boost_pressure_kpa) * min(1, dt / 1.2)
        e.boost_pressure_kpa = max(0, e.boost_pressure_kpa)

        # ---- Thermals (coupled with load) ----
        equilibrium_coolant = 60 + 0.4 * e.engine_load_pct + 0.005 * e.engine_rpm
        e.coolant_temp_c += (equilibrium_coolant - e.coolant_temp_c) * min(1, dt / 40)
        e.oil_temp_c += (e.coolant_temp_c + 12 - e.oil_temp_c) * min(1, dt / 60)
        equilibrium_egt = 200 + 5 * e.engine_load_pct + 0.1 * timing * e.engine_load_pct
        e.exhaust_gas_temp_c += (equilibrium_egt - e.exhaust_gas_temp_c) * min(1, dt / 8)
        e.intake_manifold_temp_c = 25 + e.boost_pressure_kpa * 0.15
        e.fuel_temp_c += (25 + e.engine_load_pct * 0.2 - e.fuel_temp_c) * min(1, dt / 120)

        # Oil pressure ~ RPM + oil temp effect
        base_oil = 80 + 0.15 * e.engine_rpm - (max(0, e.oil_temp_c - 90)) * 1.5
        e.engine_oil_pressure_kpa += (base_oil - e.engine_oil_pressure_kpa) * min(1, dt / 2)

        # Fuel delivery pressure follows load
        e.fuel_delivery_pressure_kpa = 400 + 1.2 * e.engine_load_pct

        # Fuel level: consume from tank, add siphon losses
        fuel_used = (e.fuel_rate_lph / 3600) * dt
        e.fuel_used_l_since_start += fuel_used
        tank_liters = e.fuel_level_pct / 100 * e.fuel_capacity_l
        tank_liters -= fuel_used
        tank_liters -= fuel_siphon
        # Reset siphon accumulator each tick (fault re-adds if still active)
        if fuel_siphon:
            ecu_dict["fuel_siphon_liters"] = 0
        tank_liters = max(0, min(e.fuel_capacity_l, tank_liters))
        e.fuel_level_pct = tank_liters / e.fuel_capacity_l * 100
        e.fuel_level_2_pct = e.fuel_level_pct

        # Trip distance
        km_this_tick = e.vehicle_speed_kph * dt / 3600
        e.trip_distance_km += km_this_tick
        e.total_vehicle_distance_km += km_this_tick

        # Rolling fuel economy (km/L over 60s window)
        self._econ_reset_s += dt
        self._fuel_econ_window_l += fuel_used
        self._fuel_econ_window_km += km_this_tick
        if self._econ_reset_s > 60 and self._fuel_econ_window_l > 0.01:
            e.fuel_economy_kmpl = self._fuel_econ_window_km / self._fuel_econ_window_l
            self._econ_reset_s = 0
            self._fuel_econ_window_km *= 0.3
            self._fuel_econ_window_l *= 0.3
        if fuel_used > 0:
            e.inst_fuel_economy_kmpl = km_this_tick / max(fuel_used, 1e-6)

        # Engine hours
        e.engine_total_hours += dt / 3600

        # Apply fault biases to output values
        self._apply_common_effects(coolant_bias, oil_pressure_bias, battery_bias, 0)

        self._sync_out(ecu_dict)

    def _decay_thermals(self, dt: float):
        e = self.ecu
        e.coolant_temp_c += (25 - e.coolant_temp_c) * min(1, dt / 300)
        e.oil_temp_c += (25 - e.oil_temp_c) * min(1, dt / 400)
        e.exhaust_gas_temp_c += (25 - e.exhaust_gas_temp_c) * min(1, dt / 60)
        e.boost_pressure_kpa = 0
        e.fuel_rate_lph = 0

    def _apply_common_effects(self, coolant_bias, oil_pressure_bias, battery_bias, fuel_siphon):
        e = self.ecu
        e.coolant_temp_c += coolant_bias
        e.coolant_temp_c = min(140, e.coolant_temp_c)
        e.engine_oil_pressure_kpa = max(0, e.engine_oil_pressure_kpa + oil_pressure_bias)
        e.battery_voltage = max(18, 27.8 + battery_bias)

    def _sync_out(self, ecu_dict: dict):
        """Update the interop dict with current live values (fault code needs speed etc.)."""
        ecu_dict["vehicle_speed_kph"] = self.ecu.vehicle_speed_kph
        ecu_dict["ignition_on"] = self.ecu.ignition_on

    # ---- Snapshot for J1939 encoder + JSON telemetry ----

    def snapshot(self) -> dict:
        e = self.ecu
        return {
            "engine_rpm": e.engine_rpm,
            "vehicle_speed_kph": e.vehicle_speed_kph,
            "engine_load_pct": e.engine_load_pct,
            "accel_pedal_pct": e.accel_pedal_pct,
            "coolant_temp_c": e.coolant_temp_c,
            "oil_temp_c": e.oil_temp_c,
            "fuel_temp_c": e.fuel_temp_c,
            "intake_manifold_temp_c": e.intake_manifold_temp_c,
            "exhaust_gas_temp_c": e.exhaust_gas_temp_c,
            "boost_pressure_kpa": e.boost_pressure_kpa,
            "air_inlet_pressure_kpa": e.air_inlet_pressure_kpa,
            "air_filter_diff_pressure_kpa": e.air_filter_diff_pressure_kpa,
            "fuel_delivery_pressure_kpa": e.fuel_delivery_pressure_kpa,
            "engine_oil_pressure_kpa": e.engine_oil_pressure_kpa,
            "engine_oil_level_pct": e.engine_oil_level_pct,
            "engine_oil_filter_diff_pressure_kpa": e.engine_oil_filter_diff_pressure_kpa,
            "crankcase_pressure_kpa": e.crankcase_pressure_kpa,
            "coolant_pressure_kpa": e.coolant_pressure_kpa,
            "coolant_level_pct": e.coolant_level_pct,
            "fuel_rate_lph": e.fuel_rate_lph,
            "fuel_economy_kmpl": e.fuel_economy_kmpl,
            "inst_fuel_economy_kmpl": e.inst_fuel_economy_kmpl,
            "engine_throttle_valve_pos_pct": e.engine_throttle_valve_pos_pct,
            "fuel_level_pct": e.fuel_level_pct,
            "fuel_level_2_pct": e.fuel_level_2_pct,
            "washer_fluid_pct": e.washer_fluid_pct,
            "fuel_filter_diff_pressure_kpa": e.fuel_filter_diff_pressure_kpa,
            "cab_interior_temp_c": e.cab_interior_temp_c,
            "engine_total_hours": e.engine_total_hours,
            "total_vehicle_distance_km": e.total_vehicle_distance_km,
            "trip_distance_km": e.trip_distance_km,
            "battery_voltage": e.battery_voltage,
            "ignition_on": e.ignition_on,
            "current_gear": e.current_gear,
            "gear_shifting": e.gear_shifting,
            "ecu_power_factor": round(self.power_factor, 3),
            "active_dtcs": self._active_dtcs,
            "source_addr": 0x00,
        }

    def set_active_dtcs(self, dtcs: list[dict]):
        self._active_dtcs = dtcs

    def usage_map(self) -> list[list[float]]:
        """0-100 heatmap of where this driver actually operates, normalized against their
        own busiest cell (not the fleet's) so it reads clearly regardless of how long the
        vehicle has been running. Read-only — a behavior readout, not a calibration surface."""
        flat_max = max((v for row in self.usage_seconds for v in row), default=0)
        if flat_max <= 0:
            return [[0.0] * MAP_SIZE for _ in range(MAP_SIZE)]
        return [[round(v / flat_max * 100, 1) for v in row] for row in self.usage_seconds]
