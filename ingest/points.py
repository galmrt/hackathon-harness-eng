"""~20 US grid points covering major disaster-risk regions."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class Point:
    lat: float
    lon: float
    label: str

    @property
    def key(self) -> str:
        return f"{self.lat},{self.lon}"


POINTS: list[Point] = [
    # West Coast / wildfire + heat
    Point(lat=34.05,  lon=-118.25, label="Los Angeles"),
    Point(lat=37.77,  lon=-122.42, label="San Francisco"),
    Point(lat=45.52,  lon=-122.68, label="Portland"),
    Point(lat=47.61,  lon=-122.33, label="Seattle"),
    Point(lat=36.17,  lon=-115.14, label="Las Vegas"),
    Point(lat=33.45,  lon=-112.07, label="Phoenix"),
    # Mountain West / wildfire + winter storm
    Point(lat=39.09,  lon=-120.03, label="Lake Tahoe"),
    Point(lat=39.74,  lon=-104.98, label="Denver"),
    Point(lat=40.76,  lon=-111.89, label="Salt Lake City"),
    Point(lat=35.68,  lon=-105.94, label="Santa Fe"),
    # Gulf Coast / flood + heat
    Point(lat=29.76,  lon=-95.37,  label="Houston"),
    Point(lat=29.95,  lon=-90.07,  label="New Orleans"),
    Point(lat=30.33,  lon=-81.66,  label="Jacksonville"),
    Point(lat=25.77,  lon=-80.19,  label="Miami"),
    # Midwest / tornado alley + flood
    Point(lat=41.85,  lon=-87.65,  label="Chicago"),
    Point(lat=39.10,  lon=-94.58,  label="Kansas City"),
    Point(lat=35.47,  lon=-97.52,  label="Oklahoma City"),
    # Northeast / winter storm
    Point(lat=40.71,  lon=-74.01,  label="New York"),
    Point(lat=42.36,  lon=-71.06,  label="Boston"),
    # Southeast
    Point(lat=33.75,  lon=-84.39,  label="Atlanta"),
    Point(lat=35.23,  lon=-80.84,  label="Charlotte"),
]
