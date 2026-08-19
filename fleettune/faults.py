"""
Fault library.

Each fault is a state machine with three phases:
  - inactive: default, no effect
  - developing: slow drift, no DTC yet (this is what predictive maintenance catches)
  - active: full effect, DTC raised

Faults are attached to a Vehicle and their tick(dt) method mutates ECU state.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import random

from .j1939 import DTC_CATALOG


class Phase(str, Enum):
    INACTIVE = "inactive"
    DEVELOPING = "developing"
    ACTIVE = "active"


@dataclass
class Fault:
    kind: str
    phase: Phase = Phase.INACTIVE
    elapsed_s: float = 0.0
    # Time budget from developing -> active
    develop_seconds: float = 90.0

    def start_developing(self):
        self.phase = Phase.DEVELOPING
        self.elapsed_s = 0.0

    def clear(self):
        self.phase = Phase.INACTIVE
        self.elapsed_s = 0.0

    def tick(self, dt: float, ecu: dict):
        if self.phase == Phase.INACTIVE:
            return
        self.elapsed_s += dt
        if self.phase == Phase.DEVELOPING and self.elapsed_s >= self.develop_seconds:
            self.phase = Phase.ACTIVE
            self.elapsed_s = 0.0
        self._apply(dt, ecu)

    def _apply(self, dt: float, ecu: dict):  # override in subclasses
        pass

    def dtc(self) -> dict | None:
        if self.phase != Phase.ACTIVE:
            return None
        info = DTC_CATALOG.get(self.kind)
        if not info:
            return None
        return {"spn": info["spn"], "fmi": info["fmi"], "desc": info["desc"],
                "occurrence": 1, "lamp": 0x40}


# ---------------- Concrete faults ----------------

@dataclass
class CoolantOverheat(Fault):
    kind: str = "coolant_overheat"
    develop_seconds: float = 180.0

    def _apply(self, dt, ecu):
        # Developing: slow drift +0.02°C/s above equilibrium; Active: +0.15°C/s
        rate = 0.02 if self.phase == Phase.DEVELOPING else 0.15
        ecu["coolant_bias_c"] = ecu.get("coolant_bias_c", 0) + rate * dt


@dataclass
class OilPressureDecay(Fault):
    kind: str = "oil_pressure_low"
    develop_seconds: float = 240.0

    def _apply(self, dt, ecu):
        rate = 0.5 if self.phase == Phase.DEVELOPING else 3.0  # kPa/s decay
        ecu["oil_pressure_bias_kpa"] = ecu.get("oil_pressure_bias_kpa", 0) - rate * dt


@dataclass
class InjectorMisfire(Fault):
    kind: str = "injector_misfire"
    develop_seconds: float = 60.0

    def _apply(self, dt, ecu):
        # RPM roughness + torque loss
        amp = 8 if self.phase == Phase.DEVELOPING else 45
        ecu["rpm_noise"] = amp
        if self.phase == Phase.ACTIVE:
            ecu["torque_loss_pct"] = 12.0


@dataclass
class TurboBoostLoss(Fault):
    kind: str = "turbo_boost_loss"
    develop_seconds: float = 150.0

    def _apply(self, dt, ecu):
        loss = 0.15 if self.phase == Phase.DEVELOPING else 0.55
        ecu["boost_loss_frac"] = loss


@dataclass
class DpfRegenRequired(Fault):
    kind: str = "dpf_regen_required"
    develop_seconds: float = 300.0

    def _apply(self, dt, ecu):
        # Soot load increases, back pressure grows, fuel economy drops
        ecu["soot_load_g"] = ecu.get("soot_load_g", 0) + 0.02 * dt
        if self.phase == Phase.ACTIVE:
            ecu["back_pressure_bias_kpa"] = 30
            ecu["fuel_econ_penalty"] = 0.12


@dataclass
class AlternatorFault(Fault):
    kind: str = "alternator_fault"
    develop_seconds: float = 120.0

    def _apply(self, dt, ecu):
        drop = 0.3 if self.phase == Phase.DEVELOPING else 1.2
        ecu["battery_voltage_bias_v"] = ecu.get("battery_voltage_bias_v", 0) - drop * dt


@dataclass
class FuelSiphon(Fault):
    """Removes fuel while the vehicle is stationary. No DTC — only anomaly detection catches this."""
    kind: str = "fuel_siphon"
    develop_seconds: float = 5.0
    liters_per_second: float = 0.15   # ~9 L/min siphon rate

    def _apply(self, dt, ecu):
        # Only siphons when parked (engine off / idle-off) and not moving
        if ecu.get("vehicle_speed_kph", 0) < 1 and not ecu.get("ignition_on", True):
            ecu["fuel_siphon_liters"] = ecu.get("fuel_siphon_liters", 0) + self.liters_per_second * dt

    def dtc(self):
        return None   # never raises a DTC — that's the whole point


@dataclass
class FuelTheft(Fault):
    """Continuous drain from a tapped line / bypassed sender — unlike FuelSiphon this runs
    whether the truck is parked or moving, mimicking theft during a delivery run rather than
    only while parked overnight. No DTC — only the fuel-theft anomaly detector catches this."""
    kind: str = "fuel_theft"
    develop_seconds: float = 20.0
    liters_per_second: float = 0.08   # ~5 L/min tap

    def _apply(self, dt, ecu):
        rate = self.liters_per_second * (0.35 if self.phase == Phase.DEVELOPING else 1.0)
        ecu["fuel_siphon_liters"] = ecu.get("fuel_siphon_liters", 0) + rate * dt

    def dtc(self):
        return None   # theft, not a diagnostic condition


@dataclass
class DrowsyDriver(Fault):
    """Modifies driver behavior: reduced steering micro-corrections, occasional lane departures."""
    kind: str = "drowsy_driver"
    develop_seconds: float = 30.0

    def _apply(self, dt, ecu):
        # Drives a "fatigue push" that the driver model reads
        push = 25 if self.phase == Phase.DEVELOPING else 60
        ecu["fatigue_push"] = push
        # Occasional micro-sleep / lane departure
        if self.phase == Phase.ACTIVE and random.random() < 0.008:
            ecu["lane_departure_pending"] = True

    def dtc(self):
        return None   # behavioral, not diagnostic


# ---------------- Registry ----------------

FAULT_TYPES: dict[str, type[Fault]] = {
    "coolant_overheat":   CoolantOverheat,
    "oil_pressure_low":   OilPressureDecay,
    "injector_misfire":   InjectorMisfire,
    "turbo_boost_loss":   TurboBoostLoss,
    "dpf_regen_required": DpfRegenRequired,
    "alternator_fault":   AlternatorFault,
    "fuel_siphon":        FuelSiphon,
    "fuel_theft":         FuelTheft,
    "drowsy_driver":      DrowsyDriver,
}


FAULT_LABELS = {
    "coolant_overheat":   "Coolant overheat",
    "oil_pressure_low":   "Oil pressure decay",
    "injector_misfire":   "Cyl 1 injector misfire",
    "turbo_boost_loss":   "Turbo boost loss",
    "dpf_regen_required": "DPF regen required",
    "alternator_fault":   "Alternator undervoltage",
    "fuel_siphon":        "Fuel siphon (parked)",
    "fuel_theft":         "Fuel theft (line tap, any state)",
    "drowsy_driver":      "Drowsy driver",
}
