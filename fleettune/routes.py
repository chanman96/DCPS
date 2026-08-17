"""
Route generator.

Produces a list of (lat, lng) waypoints per vehicle. We don't hit OSM at runtime — instead
we synthesize plausible city-scale loops around a handful of anchor points, so the sim runs
offline. Vehicles wrap around their loop indefinitely.
"""
from __future__ import annotations
import math
import random

# Anchor cities (lat, lng) — spread globally so a fleet demo looks like fleet ops
ANCHORS = [
    ("Mumbai",       19.076, 72.877),
    ("Delhi",        28.613, 77.209),
    ("Bengaluru",    12.972, 77.594),
    ("Chennai",      13.083, 80.270),
    ("Kolkata",      22.573, 88.364),
    ("Hyderabad",    17.385, 78.487),
    ("Pune",         18.520, 73.856),
    ("Ahmedabad",    23.023, 72.571),
    ("Jaipur",       26.912, 75.787),
    ("Lucknow",      26.847, 80.947),
    ("Nagpur",       21.146, 79.088),
    ("Surat",        21.170, 72.831),
    ("Kanpur",       26.449, 80.331),
    ("Indore",       22.720, 75.858),
    ("Bhopal",       23.259, 77.413),
]


def _polyline_loop(center_lat: float, center_lng: float,
                   radius_km: float, points: int, rng: random.Random) -> list[tuple[float, float]]:
    """Generate a slightly noisy loop of GPS points around a center."""
    coords = []
    # Roughly: 1 degree lat ~ 111 km, 1 degree lng ~ 111 km * cos(lat)
    lat_scale = 1 / 111.0
    lng_scale = 1 / (111.0 * math.cos(math.radians(center_lat)))
    for i in range(points):
        theta = (i / points) * 2 * math.pi
        # Vary radius so the loop looks organic
        r = radius_km * (0.7 + 0.6 * rng.random())
        lat = center_lat + r * math.sin(theta) * lat_scale
        lng = center_lng + r * math.cos(theta) * lng_scale
        coords.append((lat, lng))
    # Close the loop
    coords.append(coords[0])
    return coords


def generate_route(seed: int) -> dict:
    rng = random.Random(seed)
    name, lat, lng = rng.choice(ANCHORS)
    radius = rng.uniform(6, 22)   # km
    points = rng.randint(28, 60)
    waypoints = _polyline_loop(lat, lng, radius, points, rng)
    return {
        "region": name,
        "center": (lat, lng),
        "waypoints": waypoints,
    }


# ---------- Speed profile & interpolation ----------

def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    R = 6371.0
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlng/2)**2
    return 2 * R * math.asin(math.sqrt(h))


def interpolate(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
