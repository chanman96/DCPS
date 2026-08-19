"""
J1939 encoding: PGN/SPN definitions and CAN frame construction.

Produces proper 29-bit extended CAN identifiers of the form:
  Priority(3) | Reserved(1) | Data Page(1) | PDU Format(8) | PDU Specific(8) | Source Address(8)

For broadcast PGNs (PDU Format >= 240), PGN = (PF << 8) | PS.
For destination-specific PGNs (PDU Format < 240), PGN = PF << 8 and PS = destination address.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable


# ---------- Encoding helpers ----------

def _clip_uint(value: float, bits: int) -> int:
    max_val = (1 << bits) - 1
    v = int(round(value))
    if v < 0:
        return 0
    if v > max_val:
        return max_val
    return v


def _u8(v: float) -> int:
    return _clip_uint(v, 8)


def _u16_le(v: float) -> tuple[int, int]:
    n = _clip_uint(v, 16)
    return n & 0xFF, (n >> 8) & 0xFF


def _u32_le(v: float) -> tuple[int, int, int, int]:
    n = _clip_uint(v, 32)
    return n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF, (n >> 24) & 0xFF


# ---------- Frame ----------

@dataclass
class J1939Frame:
    can_id: int          # 29-bit extended
    pgn: int
    source_address: int
    priority: int
    data: bytes          # 8 bytes

    @property
    def can_id_hex(self) -> str:
        return f"{self.can_id:08X}"

    @property
    def data_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)


def build_can_id(priority: int, pgn: int, source_address: int) -> int:
    """Build 29-bit J1939 CAN ID from priority, PGN, and source address."""
    pdu_format = (pgn >> 8) & 0xFF
    pdu_specific = pgn & 0xFF
    data_page = (pgn >> 16) & 0x1
    reserved = 0

    return (
        ((priority & 0x7) << 26)
        | ((reserved & 0x1) << 25)
        | ((data_page & 0x1) << 24)
        | (pdu_format << 16)
        | (pdu_specific << 8)
        | (source_address & 0xFF)
    )


# ---------- PGN definitions ----------
#
# Each PGN encoder takes an ECU snapshot (dict) and returns 8 bytes.
# Where a field is not applicable, we emit 0xFF (J1939 "not available").

def encode_eec1(s: dict) -> bytes:
    """PGN 61444 (0xF004) EEC1 — Engine speed & torque. 100 ms broadcast, priority 3."""
    torque_pct = _u8(s["engine_load_pct"] + 125)  # SPN 513, offset -125, 1%/bit
    rpm_lo, rpm_hi = _u16_le(s["engine_rpm"] / 0.125)  # SPN 190, 0.125 rpm/bit
    return bytes([
        0xF0,                          # byte 0: engine torque mode + starter mode (packed)
        _u8(s["engine_load_pct"] + 125),  # byte 1: driver's demand engine torque
        torque_pct,                    # byte 2: actual engine torque
        rpm_lo, rpm_hi,                # bytes 3-4: engine speed
        s.get("source_addr", 0),       # byte 5: source address of controlling device
        0xFF, 0xFF,                    # bytes 6-7: reserved / engine starter mode
    ])


def encode_eec2(s: dict) -> bytes:
    """PGN 61443 (0xF003) EEC2 — Accelerator pedal & engine load. 50 ms, priority 3."""
    return bytes([
        0xF0,                          # byte 0: accelerator pedal pos idle switches
        _u8(s["accel_pedal_pct"] / 0.4),  # SPN 91, 0.4%/bit
        _u8(s["engine_load_pct"]),     # SPN 92, 1%/bit
        0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
    ])


def encode_et1(s: dict) -> bytes:
    """PGN 65262 (0xFEEE) ET1 — Engine Temperature 1. 1 s broadcast, priority 6."""
    return bytes([
        _u8(s["coolant_temp_c"] + 40),      # SPN 110, 1°C/bit, offset -40
        _u8(s["fuel_temp_c"] + 40),         # SPN 174
        _u16_le((s["oil_temp_c"] + 273) / 0.03125)[0],
        _u16_le((s["oil_temp_c"] + 273) / 0.03125)[1],  # SPN 175, 0.03125°C/bit, offset -273
        0xFF, 0xFF, 0xFF, 0xFF,
    ])


def encode_efl_p1(s: dict) -> bytes:
    """PGN 65263 (0xFEEF) EFL/P1 — Engine Fluid Level/Pressure 1. 500 ms."""
    return bytes([
        _u8(s["fuel_delivery_pressure_kpa"] / 4),   # SPN 94, 4 kPa/bit
        0xFF,
        _u8(s["engine_oil_level_pct"] / 0.4),       # SPN 98, 0.4%/bit
        _u8(s["engine_oil_pressure_kpa"] / 4),      # SPN 100, 4 kPa/bit
        _u16_le((s["crankcase_pressure_kpa"] + 250) / 0.0078125)[0],
        _u16_le((s["crankcase_pressure_kpa"] + 250) / 0.0078125)[1],  # SPN 101
        _u8(s["coolant_pressure_kpa"] / 2),         # SPN 109, 2 kPa/bit
        _u8(s["coolant_level_pct"] / 0.4),          # SPN 111
    ])


def encode_lfe1(s: dict) -> bytes:
    """PGN 65266 (0xFEF2) LFE1 — Fuel Economy. 100 ms."""
    fuel_rate = _u16_le(s["fuel_rate_lph"] / 0.05)  # SPN 183, 0.05 L/h /bit
    fuel_econ = _u16_le(s["fuel_economy_kmpl"] / 0.001953125)  # SPN 184
    inst_econ = _u16_le(s["inst_fuel_economy_kmpl"] / 0.001953125)  # SPN 185
    return bytes([
        fuel_rate[0], fuel_rate[1],
        fuel_econ[0], fuel_econ[1],
        inst_econ[0], inst_econ[1],
        _u8(s["engine_throttle_valve_pos_pct"] / 0.4),
        0xFF,
    ])


def encode_ccvs1(s: dict) -> bytes:
    """PGN 65265 (0xFEF1) CCVS1 — Cruise Control/Vehicle Speed. 100 ms."""
    speed = _u16_le(s["vehicle_speed_kph"] / (1/256))  # SPN 84, 1/256 km/h /bit
    return bytes([
        0xF0,                                    # byte 0: switches packed
        speed[0], speed[1],                      # bytes 1-2: wheel-based vehicle speed
        0xFF,                                    # byte 3: cruise control set speed
        0xFF, 0xFF, 0xFF,                        # bytes 4-6: switches / brake / clutch
        0xFF,                                    # byte 7: PTO state
    ])


def encode_ic1(s: dict) -> bytes:
    """PGN 65270 (0xFEF6) IC1 — Inlet/Exhaust Conditions 1. 500 ms."""
    return bytes([
        _u8(s["air_filter_diff_pressure_kpa"] / 0.05),   # SPN 107
        _u8(s["exhaust_gas_temp_c"] + 273),              # SPN 173 simplification
        _u8(s["boost_pressure_kpa"] / 2),                # SPN 102, 2 kPa/bit
        _u8(s["intake_manifold_temp_c"] + 40),           # SPN 105
        _u8(s["air_inlet_pressure_kpa"] / 2),            # SPN 106
        _u8(s["air_filter_diff_pressure_kpa"] / 0.05),   # duplicate for encoding
        _u16_le(s["exhaust_gas_temp_c"] / 0.03125 + 273 / 0.03125)[0],
        _u16_le(s["exhaust_gas_temp_c"] / 0.03125 + 273 / 0.03125)[1],
    ])


def encode_etc1(s: dict) -> bytes:
    """PGN 61445 (0xF005) ETC1 — Electronic Transmission Controller 1. 100 ms, priority 3."""
    gear = s.get("current_gear", 0)
    return bytes([
        0xF0,                     # byte 0: transmission driveline engaged / lockup switches
        _u8(gear + 125),          # SPN 523, current gear, offset -125 (0 = Neutral)
        _u8(gear + 125),          # SPN 524, selected gear (AMT: no separate lever position)
        0xFF, 0xFF,               # bytes 3-4: output shaft speed (not modeled)
        0xFF, 0xFF, 0xFF,
    ])


def encode_hours(s: dict) -> bytes:
    """PGN 65253 (0xFEE5) HOURS — Engine hours & revolutions. On request or 1 s."""
    hrs = _u32_le(s["engine_total_hours"] / 0.05)  # SPN 247, 0.05 h/bit
    return bytes([hrs[0], hrs[1], hrs[2], hrs[3], 0xFF, 0xFF, 0xFF, 0xFF])


def encode_dd(s: dict) -> bytes:
    """PGN 65276 (0xFEFC) DD — Dash Display. 1 s."""
    return bytes([
        _u8(s["washer_fluid_pct"] / 0.4),
        _u8(s["fuel_level_pct"] / 0.4),          # SPN 96, 0.4%/bit
        _u8(s["fuel_filter_diff_pressure_kpa"] / 2),
        _u8(s["engine_oil_filter_diff_pressure_kpa"] / 0.5),
        _u16_le((s["cab_interior_temp_c"] + 273) / 0.03125)[0],
        _u16_le((s["cab_interior_temp_c"] + 273) / 0.03125)[1],
        _u8(s["fuel_level_2_pct"] / 0.4),
        0xFF,
    ])


def encode_vdhr(s: dict) -> bytes:
    """PGN 65217 (0xFEC1) VDHR — Vehicle Distance (high res). 1 s."""
    total = _u32_le(s["total_vehicle_distance_km"] / 0.005)  # SPN 917
    trip = _u32_le(s["trip_distance_km"] / 0.005)             # SPN 918
    return bytes([total[0], total[1], total[2], total[3],
                  trip[0], trip[1], trip[2], trip[3]])


def encode_dm1(s: dict) -> bytes:
    """PGN 65226 (0xFECA) DM1 — Active Diagnostic Trouble Codes. 1 s if faults present.

    Byte layout: lamp status (2 bytes) + one DTC (SPN 19 bits + FMI 5 bits + CM 1 + OC 7).
    If multiple DTCs, real J1939 uses TP.CM; we emit the first for the compact 8-byte frame.
    """
    dtcs = s.get("active_dtcs", [])
    if not dtcs:
        # All lamps off, no DTC
        return bytes([0x00, 0xFF, 0x00, 0x00, 0x00, 0x00, 0xFF, 0xFF])
    d = dtcs[0]
    spn = d["spn"] & 0x7FFFF     # 19 bits
    fmi = d["fmi"] & 0x1F        # 5 bits
    cm = 0                        # conversion method
    oc = d.get("occurrence", 1) & 0x7F
    # SPN packing per J1939-73: byte4 = SPN LSB, byte5 = SPN middle, byte6 top 3 bits SPN + 5 bits FMI
    b3 = spn & 0xFF
    b4 = (spn >> 8) & 0xFF
    b5 = ((spn >> 16) & 0x07) << 5 | (fmi & 0x1F)
    b6 = ((cm & 0x1) << 7) | (oc & 0x7F)
    lamp_status = d.get("lamp", 0x40)  # amber warning by default
    return bytes([lamp_status, 0xFF, b3, b4, b5, b6, 0xFF, 0xFF])


# ---------- PGN registry ----------

@dataclass
class PgnSpec:
    pgn: int
    name: str
    priority: int
    period_ms: int              # nominal broadcast period
    encoder: Callable[[dict], bytes]


PGN_REGISTRY: list[PgnSpec] = [
    PgnSpec(61444, "EEC1",   3,  100, encode_eec1),
    PgnSpec(61443, "EEC2",   3,   50, encode_eec2),
    PgnSpec(65262, "ET1",    6, 1000, encode_et1),
    PgnSpec(65263, "EFL/P1", 6,  500, encode_efl_p1),
    PgnSpec(65266, "LFE1",   3,  100, encode_lfe1),
    PgnSpec(61445, "ETC1",   3,  100, encode_etc1),
    PgnSpec(65265, "CCVS1",  6,  100, encode_ccvs1),
    PgnSpec(65270, "IC1",    6,  500, encode_ic1),
    PgnSpec(65253, "HOURS",  6, 1000, encode_hours),
    PgnSpec(65276, "DD",     6, 1000, encode_dd),
    PgnSpec(65217, "VDHR",   6, 1000, encode_vdhr),
    PgnSpec(65226, "DM1",    6, 1000, encode_dm1),
]


def build_frames(snapshot: dict, source_address: int = 0x00) -> list[J1939Frame]:
    """Encode every registered PGN from a full ECU snapshot."""
    frames: list[J1939Frame] = []
    for spec in PGN_REGISTRY:
        data = spec.encoder(snapshot)
        # Pad/truncate to 8 bytes
        if len(data) < 8:
            data = data + bytes([0xFF] * (8 - len(data)))
        else:
            data = data[:8]
        can_id = build_can_id(spec.priority, spec.pgn, source_address)
        frames.append(J1939Frame(
            can_id=can_id, pgn=spec.pgn,
            source_address=source_address, priority=spec.priority,
            data=data,
        ))
    return frames


# ---------- DTC catalog ----------

# (SPN, FMI, description) — the subset our fault library can raise.
DTC_CATALOG = {
    "coolant_overheat":   {"spn": 110, "fmi": 0,  "desc": "Engine coolant temperature — above normal (most severe)"},
    "oil_pressure_low":   {"spn": 100, "fmi": 1,  "desc": "Engine oil pressure — below normal (most severe)"},
    "injector_misfire":   {"spn": 1323,"fmi": 5,  "desc": "Cylinder 1 misfire detected"},
    "turbo_boost_loss":   {"spn": 102, "fmi": 1,  "desc": "Boost pressure — below normal"},
    "dpf_regen_required": {"spn": 3251,"fmi": 15, "desc": "Diesel particulate filter differential pressure — high"},
    "alternator_fault":   {"spn": 168, "fmi": 1,  "desc": "Electrical potential (voltage) — below normal"},
}
