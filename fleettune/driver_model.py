"""
Driver model: throttle/steering behavior and fatigue score.

Fatigue score is a 0-100 composite driven by:
  - Continuous driving hours (Hours of Service)
  - Time of day (circadian dips: 02:00-06:00 and 14:00-16:00)
  - Steering entropy (proxy: variance of small steering corrections)
  - Speed variance (erratic driving raises score)
  - Lane departure counter (short-term spike)

Faults can add a "fatigue_push" that biases the score upward for demos.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import math
import random
from collections import deque
from datetime import datetime


@dataclass
class Driver:
    name: str
    profile: str = "normal"    # "eco", "normal", "aggressive"
    continuous_drive_s: float = 0.0
    steering_history: deque = field(default_factory=lambda: deque(maxlen=60))
    speed_history: deque = field(default_factory=lambda: deque(maxlen=60))
    lane_departures_recent: int = 0
    lane_departure_decay_s: float = 0.0
    fatigue_score: float = 5.0
    _rng: random.Random = field(default_factory=lambda: random.Random())

    def target_speed_factor(self) -> float:
        if self.profile == "eco":        return 0.85
        if self.profile == "aggressive": return 1.15
        return 1.0

    def throttle_smoothness(self) -> float:
        """1.0 = perfectly smooth, 0 = jerky. Aggressive drivers are jerkier."""
        base = {"eco": 0.9, "normal": 0.75, "aggressive": 0.55}[self.profile]
        return max(0.1, base - 0.006 * self.fatigue_score)

    def tick(self, dt: float, ecu: dict, now: datetime):
        # Continuous drive: reset when parked/off for >5 min (simplified: reset when speed<1 for 60s)
        if ecu.get("vehicle_speed_kph", 0) > 2:
            self.continuous_drive_s += dt
            ecu["_idle_stopped_s"] = 0
        else:
            ecu["_idle_stopped_s"] = ecu.get("_idle_stopped_s", 0) + dt
            if ecu["_idle_stopped_s"] > 60:
                self.continuous_drive_s = max(0, self.continuous_drive_s - dt * 3)

        # Steering "micro-correction" sample — inversely correlated with fatigue
        entropy_base = 0.4 + (0.4 if self.profile == "aggressive" else 0.2)
        fatigue_damp = max(0.15, 1 - self.fatigue_score / 120)
        steering_sample = self._rng.gauss(0, entropy_base * fatigue_damp)
        self.steering_history.append(steering_sample)

        # Speed variance sample
        self.speed_history.append(ecu.get("vehicle_speed_kph", 0))

        # Lane departure trigger from fault
        if ecu.pop("lane_departure_pending", False):
            self.lane_departures_recent += 1
            self.lane_departure_decay_s = 0
        else:
            self.lane_departure_decay_s += dt
            if self.lane_departure_decay_s > 300 and self.lane_departures_recent > 0:
                self.lane_departures_recent -= 1
                self.lane_departure_decay_s = 0

        # Score composition
        hours = self.continuous_drive_s / 3600
        hos_component = min(60, hours * 8)   # 8 pts/hour continuous
        hr = now.hour + now.minute / 60
        circ = 0.0
        if 2 <= hr <= 6:    circ = 20 * (1 - abs(hr - 4) / 2)
        elif 14 <= hr <= 16: circ = 8 * (1 - abs(hr - 15))

        # Steering entropy: low variance under sustained motion is a fatigue signal
        if len(self.steering_history) >= 30 and ecu.get("vehicle_speed_kph", 0) > 30:
            import statistics
            var = statistics.pvariance(self.steering_history)
            # Low variance = 0..15 pts
            entropy_component = max(0, 15 - var * 30)
        else:
            entropy_component = 0

        # Speed variance: extreme variance under highway speed = distracted/drowsy
        if len(self.speed_history) >= 30:
            import statistics
            svar = statistics.pvariance(self.speed_history)
            speed_var_component = min(10, svar / 20)
        else:
            speed_var_component = 0

        departures_component = min(20, self.lane_departures_recent * 8)

        raw = (
            hos_component
            + circ
            + entropy_component
            + speed_var_component
            + departures_component
            + ecu.get("fatigue_push", 0)
        )
        # Smooth toward raw — a lot faster while a drowsy-driver fault is actively pushing,
        # so injecting the fault doesn't take tens of seconds to become visible.
        target = max(0, min(100, raw))
        tau = 1.5 if ecu.get("fatigue_push", 0) > 0 else 5
        self.fatigue_score += (target - self.fatigue_score) * min(1, dt / tau)

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "profile": self.profile,
            "continuous_drive_h": round(self.continuous_drive_s / 3600, 2),
            "fatigue_score": round(self.fatigue_score, 1),
            "lane_departures_recent": self.lane_departures_recent,
        }


DRIVER_NAMES = [
    "R. Menon", "S. Iyer", "K. Patel", "A. Sharma", "M. Reddy",
    "V. Singh", "N. Das", "P. Nair", "H. Gupta", "T. Kaur",
    "J. Fernandes", "D. Rao", "L. Bose", "F. Khan", "G. Verma",
]
