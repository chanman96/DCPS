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
            self._decay_thermals(dt)
            self._apply_common_effects(coolant_bias, oil_pressure_bias, battery_bias, fuel_siphon)
            self._sync_out(ecu_dict)
            return

        # ---- RPM/speed dynamics ----
        target_rpm = e.idle_rpm_target + self._driver_throttle * (min(e.rev_limit_rpm, 2200) - e.idle_rpm_target)
        # First-order tracking
        e.engine_rpm += (target_rpm - e.engine_rpm) * min(1, dt / 0.4)
        e.engine_rpm += self.rng.gauss(0, rpm_noise * 0.3)
        e.engine_rpm = max(0, e.engine_rpm)

        # Speed: derived from RPM (simplified single ratio at highway)
        gear_ratio = 0.037   # kph per rpm at cruise gear
        target_speed = min(e.speed_governor_kph, e.engine_rpm * gear_ratio - 8 * self._brake * 5)
        e.vehicle_speed_kph += (target_speed - e.vehicle_speed_kph) * min(1, dt / 1.5)
        e.vehicle_speed_kph = max(0, e.vehicle_speed_kph - self._brake * 20 * dt)

        # ---- Engine load ----
        rpm_frac = max(0, (e.engine_rpm - e.idle_rpm_target) / (e.rev_limit_rpm - e.idle_rpm_target))
        raw_load = 10 + 90 * (self._driver_throttle * (0.6 + 0.4 * rpm_frac))
        e.engine_load_pct += (raw_load - e.engine_load_pct) * min(1, dt / 0.8)
        e.engine_load_pct = max(0, min(100, e.engine_load_pct - torque_loss))
        e.accel_pedal_pct = self._driver_throttle * 100
        e.engine_throttle_valve_pos_pct = e.accel_pedal_pct * 0.9

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
            "active_dtcs": self._active_dtcs,
            "source_addr": 0x00,
        }

    def set_active_dtcs(self, dtcs: list[dict]):
        self._active_dtcs = dtcs
