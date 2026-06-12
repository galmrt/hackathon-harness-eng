"""
Hourly ingest: Jua forecast + Open-Meteo supplement → scorer → ClickHouse.

Run:  python -m ingest.poller
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from jua import JuaClient
from jua.settings import JuaSettings
from jua.settings.authentication import AuthenticationSettings
from jua.types.geo import LatLon
from jua.weather.models import Models
from jua.weather.variables import Variables

from api.db import get_client as get_ch_client
from ingest.points import POINTS
from ingest.scorer import score_all

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Jua variables we request
JUA_VARIABLES = [
    Variables.AIR_TEMPERATURE_AT_HEIGHT_LEVEL_2M,   # Kelvin
    Variables.WIND_SPEED_AT_HEIGHT_LEVEL_10M,        # m/s
    Variables.WIND_SPEED_AT_HEIGHT_LEVEL_100M,       # m/s (used as gusts proxy)
]

# Open-Meteo free API — no key required
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_VARS = (
    "precipitation,relative_humidity_2m,"
    "soil_moisture_0_to_1cm,snow_depth,wind_gusts_10m"
)


def _jua_client() -> JuaClient:
    settings = JuaSettings(
        auth=AuthenticationSettings(
            api_key_id=os.environ.get("JUA_API_KEY_ID", ""),
            api_key_secret=os.environ.get("JUA_API_KEY", ""),
        )
    )
    return JuaClient(settings=settings)


def fetch_open_meteo(points: list[LatLon]) -> dict[str, dict]:
    """
    Returns {point_key: {hour_iso: {var: value}}} for the next 24 h.
    Uses Open-Meteo to fill variables Jua doesn't provide.
    """
    result: dict[str, dict] = {}
    with httpx.Client(timeout=30) as http:
        for pt in points:
            resp = http.get(
                OPEN_METEO_URL,
                params={
                    "latitude": pt.lat,
                    "longitude": pt.lon,
                    "hourly": OPEN_METEO_VARS,
                    "forecast_days": 2,
                    "timezone": "UTC",
                },
            )
            resp.raise_for_status()
            data = resp.json()["hourly"]
            times = data["time"]
            by_hour: dict[str, dict] = {}
            for i, t in enumerate(times):
                by_hour[t] = {
                    "precipitation":      data["precipitation"][i] or 0.0,
                    "relative_humidity":  data["relative_humidity_2m"][i] or 50.0,
                    "soil_moisture":      data["soil_moisture_0_to_1cm"][i] or 0.2,
                    "snow_depth":         data["snow_depth"][i] or 0.0,
                    "wind_gusts":         data["wind_gusts_10m"][i] or 0.0,
                }
            result[pt.key] = by_hour
    return result


def fetch_jua(points: list[LatLon]) -> dict[str, dict]:
    """
    Returns {point_key: {hour_iso: {var: value}}} for the next 24 h.
    Temperature converted from K to °C.
    """
    client = _jua_client()
    model = client.weather.get_model(Models.EPT2)

    log.info("Fetching Jua forecast for %d points …", len(points))
    ds = model.forecast.get_forecast(
        points=points,
        variables=JUA_VARIABLES,
        max_lead_time=24,
        print_progress=False,
    )
    xr_ds = ds.to_xarray()

    result: dict[str, dict] = {}
    for pt in points:
        by_hour: dict[str, dict] = {}
        try:
            pt_data = xr_ds.sel(points=pt)
            for i in range(len(pt_data.prediction_timedelta)):
                row = pt_data.isel(prediction_timedelta=i)
                # valid_time = init_time + lead
                valid_time: datetime = row.valid_time.values.astype("datetime64[s]").astype(datetime)
                hour_key = valid_time.strftime("%Y-%m-%dT%H:00")
                temp_k = float(row[str(Variables.AIR_TEMPERATURE_AT_HEIGHT_LEVEL_2M)].values)
                wind10 = float(row[str(Variables.WIND_SPEED_AT_HEIGHT_LEVEL_10M)].values)
                wind100 = float(row[str(Variables.WIND_SPEED_AT_HEIGHT_LEVEL_100M)].values)
                by_hour[hour_key] = {
                    "temperature": temp_k - 273.15,
                    "wind_speed":  wind10,
                    "wind_gusts":  wind100,   # 100m wind ≈ gusts proxy until gusts available
                }
        except Exception as exc:
            log.warning("Jua extraction failed for %s: %s", pt.key, exc)
        result[pt.key] = by_hour
    return result


def write_scores(rows: list[dict]) -> None:
    client = get_ch_client()
    client.insert(
        "risk_scores",
        [
            [
                r["lat"],
                r["lon"],
                r["timestamp"],
                r["disaster_type"],
                r["score"],
                r["raw_variables"],
            ]
            for r in rows
        ],
        column_names=["lat", "lon", "timestamp", "disaster_type", "score", "raw_variables"],
    )
    log.info("Inserted %d rows into ClickHouse.", len(rows))


def run() -> None:
    log.info("=== HazardWatch ingest start ===")

    log.info("Fetching Open-Meteo supplement …")
    om_data = fetch_open_meteo(POINTS)

    jua_data = fetch_jua(POINTS)

    rows: list[dict] = []
    for pt in POINTS:
        jua_hours = jua_data.get(pt.key, {})
        om_hours  = om_data.get(pt.key, {})

        # Use union of available hours; fall back gracefully when one source is missing
        all_hours = sorted(set(jua_hours) | set(om_hours))
        for hour_key in all_hours:
            vars_merged = {
                "temperature":      20.0,
                "wind_speed":        0.0,
                "wind_gusts":        0.0,
                "precipitation":     0.0,
                "relative_humidity": 50.0,
                "soil_moisture":     0.2,
                "snow_depth":        0.0,
            }
            vars_merged.update(jua_hours.get(hour_key, {}))
            vars_merged.update(om_hours.get(hour_key, {}))

            scores = score_all(vars_merged)
            try:
                ts = datetime.strptime(hour_key, "%Y-%m-%dT%H:00").replace(tzinfo=timezone.utc)
            except ValueError:
                ts = datetime.strptime(hour_key, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)

            for disaster_type, score in scores.items():
                rows.append({
                    "lat":           pt.lat,
                    "lon":           pt.lon,
                    "timestamp":     ts,
                    "disaster_type": disaster_type,
                    "score":         score,
                    "raw_variables": json.dumps(vars_merged),
                })

    write_scores(rows)
    log.info("=== HazardWatch ingest complete: %d score rows ===", len(rows))

    # Trigger alert monitor after each ingest (Step 5)
    try:
        from agents.monitor import run as run_monitor  # noqa: PLC0415
        run_monitor()
    except Exception as exc:
        log.warning("Alert monitor skipped (not yet implemented): %s", exc)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        log.error("Ingest failed: %s", exc, exc_info=True)
        sys.exit(1)
