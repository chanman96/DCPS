"""
Pre-saved ECU calibration presets.

Each preset bundles a full map set (fuel/timing/boost) with the four tune scalars, so an
operator can push a whole known calibration in one click instead of editing cell-by-cell.
They're chosen to sit on either side of Vehicle.power_factor (see ecu_model.py): "eco" and
"detuned" both starve the fuel map, forcing the simulated driver to hold more pedal for
longer to hit the same target speed (visible as pedal % and fatigue climbing on Vehicle
detail); "performance" over-fuels it, doing the opposite. That's the demo this module exists
for — a single click that visibly moves driver behavior, not just the calibration numbers.
"""
from __future__ import annotations

from .ecu_model import default_fuel_map, default_timing_map, default_boost_map


def _scale_map(base: list[list[float]], mult: float, bias: float = 0.0,
                lo: float | None = None, hi: float | None = None) -> list[list[float]]:
    out = []
    for row in base:
        new_row = []
        for v in row:
            nv = v * mult + bias
            if lo is not None: nv = max(lo, nv)
            if hi is not None: nv = min(hi, nv)
            new_row.append(round(nv, 1))
        out.append(new_row)
    return out


def _preset(label: str, note: str, fuel_mult: float, fuel_bias: float, timing_bias: float,
            boost_mult: float, tune: dict) -> dict:
    return {
        "label": label,
        "note": note,
        "fuel_map": _scale_map(default_fuel_map(), fuel_mult, fuel_bias, lo=5, hi=100),
        "timing_map": _scale_map(default_timing_map(), 1.0, timing_bias, lo=-5, hi=24),
        "boost_map": _scale_map(default_boost_map(), boost_mult, 0, lo=0, hi=260),
        "tune": tune,
    }


ECU_PRESETS: dict[str, dict] = {
    "stock": _preset(
        "Stock", "Factory calibration — baseline for comparison.",
        1.00, 0, 0, 1.00,
        {"idle_rpm_target": 750, "rev_limit_rpm": 2200,
         "speed_governor_kph": 105, "fuel_trim_pct": 0},
    ),
    "eco": _preset(
        "Eco / fuel-saver",
        "Leaner map, lower governor. Saves fuel, but the driver has to hold more pedal "
        "for longer to hit the same speed — watch pedal % and fatigue climb on Vehicle detail.",
        0.82, -3, -2, 0.88,
        {"idle_rpm_target": 650, "rev_limit_rpm": 1950,
         "speed_governor_kph": 90, "fuel_trim_pct": -8},
    ),
    "performance": _preset(
        "Performance",
        "Richer map, advanced timing, more boost. Less pedal needed for the same speed, "
        "at the cost of higher EGT and fuel burn.",
        1.18, 4, 3, 1.15,
        {"idle_rpm_target": 800, "rev_limit_rpm": 2350,
         "speed_governor_kph": 130, "fuel_trim_pct": 10},
    ),
    "detuned": _preset(
        "Detuned / limp",
        "Heavily restricted map, near the simulator's torque floor. The clearest single "
        "demo of the tuning-to-driver-behavior loop: pedal pins high, speed wanders.",
        0.58, -2, -3, 0.55,
        {"idle_rpm_target": 700, "rev_limit_rpm": 1900,
         "speed_governor_kph": 70, "fuel_trim_pct": -15},
    ),
}

PRESET_ORDER = ["stock", "eco", "performance", "detuned"]
