"""~20 US grid points covering major disaster-risk regions."""

from jua.types.geo import LatLon

POINTS: list[LatLon] = [
    # West Coast / wildfire + heat
    LatLon(lat=34.05, lon=-118.25, label="Los Angeles"),
    LatLon(lat=37.77, lon=-122.42, label="San Francisco"),
    LatLon(lat=45.52, lon=-122.68, label="Portland"),
    LatLon(lat=47.61, lon=-122.33, label="Seattle"),
    LatLon(lat=36.17, lon=-115.14, label="Las Vegas"),
    LatLon(lat=33.45, lon=-112.07, label="Phoenix"),
    # Mountain West / wildfire + winter storm
    LatLon(lat=39.74, lon=-104.98, label="Denver"),
    LatLon(lat=40.76, lon=-111.89, label="Salt Lake City"),
    LatLon(lat=35.68, lon=-105.94, label="Santa Fe"),
    # Gulf Coast / flood + heat
    LatLon(lat=29.76, lon=-95.37, label="Houston"),
    LatLon(lat=29.95, lon=-90.07, label="New Orleans"),
    LatLon(lat=30.33, lon=-81.66, label="Jacksonville"),
    LatLon(lat=25.77, lon=-80.19, label="Miami"),
    # Midwest / tornado alley + flood
    LatLon(lat=41.85, lon=-87.65, label="Chicago"),
    LatLon(lat=39.10, lon=-94.58, label="Kansas City"),
    LatLon(lat=35.47, lon=-97.52, label="Oklahoma City"),
    # Northeast / winter storm
    LatLon(lat=40.71, lon=-74.01, label="New York"),
    LatLon(lat=42.36, lon=-71.06, label="Boston"),
    # Southeast
    LatLon(lat=33.75, lon=-84.39, label="Atlanta"),
    LatLon(lat=35.23, lon=-80.84, label="Charlotte"),
]
