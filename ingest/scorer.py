"""
Risk scoring functions — one per disaster type, all return float 0–100.

Variable units (Jua SDK defaults):
  wind_speed / wind_gusts : m/s
  temperature             : °C
  relative_humidity       : % (0–100)
  precipitation           : mm/hr
  soil_moisture           : m³/m³ (0–1)
  snow_depth              : m
"""

from __future__ import annotations


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _ramp(value: float, lo: float, hi: float) -> float:
    """Linear ramp: returns 0 when value≤lo, 100 when value≥hi."""
    if hi == lo:
        return 100.0 if value >= hi else 0.0
    return _clamp((value - lo) / (hi - lo) * 100.0)


def _ramp_inv(value: float, lo: float, hi: float) -> float:
    """Inverse ramp: returns 100 when value≤lo, 0 when value≥hi."""
    return _ramp(hi - (value - lo), 0.0, hi - lo)


# ---------------------------------------------------------------------------
# Wildfire
# Variables: wind_speed, relative_humidity, temperature, precipitation
# ---------------------------------------------------------------------------

def score_wildfire(v: dict) -> float:
    wind   = float(v.get("wind_speed", 0))
    rh     = float(v.get("relative_humidity", 50))
    temp   = float(v.get("temperature", 20))
    precip = float(v.get("precipitation", 0))

    # If actively raining (>5 mm/hr) wildfire risk collapses
    rain_suppression = _clamp(1.0 - precip / 5.0, 0.0, 1.0)

    wind_score = _ramp(wind, 3, 15)        # 3–15 m/s
    rh_score   = _ramp_inv(rh, 10, 50)     # 50%→0 risk, 10%→100 risk
    temp_score = _ramp(temp, 25, 42)       # 25–42 °C

    raw = wind_score * 0.30 + rh_score * 0.40 + temp_score * 0.30
    return round(_clamp(raw * rain_suppression), 1)


# ---------------------------------------------------------------------------
# Flood
# Variables: precipitation, soil_moisture
# ---------------------------------------------------------------------------

def score_flood(v: dict) -> float:
    precip       = float(v.get("precipitation", 0))
    soil_moisture = float(v.get("soil_moisture", 0.2))

    precip_score = _ramp(precip, 5, 50)           # 5–50 mm/hr
    # Saturated soil (>0.4 m³/m³) can't absorb more water
    soil_score   = _ramp(soil_moisture, 0.25, 0.50)

    return round(_clamp(precip_score * 0.65 + soil_score * 0.35), 1)


# ---------------------------------------------------------------------------
# Extreme heat
# Variables: temperature, relative_humidity  (Steadman heat index → risk)
# ---------------------------------------------------------------------------

def score_extreme_heat(v: dict) -> float:
    temp = float(v.get("temperature", 20))
    rh   = float(v.get("relative_humidity", 40))

    # Simplified Steadman apparent temperature (°C)
    hi = -8.784695 + 1.61139411 * temp + 2.338549 * (rh / 100) \
         - 0.14611605 * temp * (rh / 100) \
         - 0.012308094 * temp ** 2 \
         - 0.016424828 * (rh / 100) ** 2 \
         + 0.002211732 * temp ** 2 * (rh / 100) \
         + 0.00072546 * temp * (rh / 100) ** 2 \
         - 0.000003582 * temp ** 2 * (rh / 100) ** 2

    # Use raw temp for low-humidity desert conditions where HI underestimates
    effective = max(temp, hi)

    return round(_ramp(effective, 30, 48), 1)   # 30–48 °C heat index


# ---------------------------------------------------------------------------
# Winter storm
# Variables: temperature, precipitation, snow_depth
# ---------------------------------------------------------------------------

def score_winter_storm(v: dict) -> float:
    temp       = float(v.get("temperature", 10))
    precip     = float(v.get("precipitation", 0))
    snow_depth = float(v.get("snow_depth", 0))

    # Only scores when temperature is near or below freezing
    if temp > 5:
        return 0.0

    cold_score  = _ramp(-temp, 0, 25)             # 0→-25 °C
    precip_score = _ramp(precip, 1, 15)           # freezing precip 1–15 mm/hr
    snow_score  = _ramp(snow_depth, 0.1, 1.0)     # 10 cm–1 m snow depth

    return round(_clamp(cold_score * 0.40 + precip_score * 0.40 + snow_score * 0.20), 1)


# ---------------------------------------------------------------------------
# High wind
# Variables: wind_speed, wind_gusts
# ---------------------------------------------------------------------------

def score_high_wind(v: dict) -> float:
    wind   = float(v.get("wind_speed", 0))
    gusts  = float(v.get("wind_gusts", wind))     # fall back to sustained if no gusts

    wind_score  = _ramp(wind, 10, 28)    # 10–28 m/s sustained (~Beaufort 6–10)
    gust_score  = _ramp(gusts, 15, 33)   # 15–33 m/s gusts (damaging threshold)

    return round(_clamp(wind_score * 0.45 + gust_score * 0.55), 1)


# ---------------------------------------------------------------------------
# Convenience: score all disaster types at once
# ---------------------------------------------------------------------------

SCORERS = {
    "wildfire":     score_wildfire,
    "flood":        score_flood,
    "extreme_heat": score_extreme_heat,
    "winter_storm": score_winter_storm,
    "high_wind":    score_high_wind,
}


def score_all(v: dict) -> dict[str, float]:
    """Return {disaster_type: score} for all five types given a variable dict."""
    return {name: fn(v) for name, fn in SCORERS.items()}
