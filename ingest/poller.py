"""
Hourly ingest: Open-Meteo forecast → scorer → ClickHouse.

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

from api.db import get_client as get_ch_client
from ingest.points import POINTS
from ingest.scorer import score_all

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_VARS = (
    "precipitation,relative_humidity_2m,soil_moisture_0_to_1cm,"
    "snow_depth,wind_gusts_10m,temperature_2m,wind_speed_10m"
)


def _val(data: list, i: int, fallback: float) -> float:
    v = data[i]
    return float(v) if v is not None else fallback


def fetch_open_meteo(points) -> dict[str, dict]:
    """Return {point_key: {hour_iso: {var: value}}} for the next 48 h."""
    result: dict[str, dict] = {}
    with httpx.Client(timeout=30) as http:
        for pt in points:
            resp = http.get(
                OPEN_METEO_URL,
                params={
                    "latitude":      pt.lat,
                    "longitude":     pt.lon,
                    "hourly":        OPEN_METEO_VARS,
                    "forecast_days": 2,
                    "timezone":      "UTC",
                },
            )
            resp.raise_for_status()
            data = resp.json()["hourly"]
            times = data["time"]
            by_hour: dict[str, dict] = {}
            for i, t in enumerate(times):
                by_hour[t] = {
                    "temperature":       _val(data["temperature_2m"], i, 20.0),
                    "wind_speed":        _val(data["wind_speed_10m"], i, 0.0),
                    "wind_gusts":        _val(data["wind_gusts_10m"], i, 0.0),
                    "precipitation":     _val(data["precipitation"], i, 0.0),
                    "relative_humidity": _val(data["relative_humidity_2m"], i, 50.0),
                    "soil_moisture":     _val(data["soil_moisture_0_to_1cm"], i, 0.2),
                    "snow_depth":        _val(data["snow_depth"], i, 0.0),
                }
            result[pt.key] = by_hour
    return result


def write_scores(rows: list[dict]) -> None:
    client = get_ch_client()
    client.insert(
        "risk_scores",
        [
            [r["lat"], r["lon"], r["timestamp"], r["disaster_type"], r["score"], r["raw_variables"]]
            for r in rows
        ],
        column_names=["lat", "lon", "timestamp", "disaster_type", "score", "raw_variables"],
    )
    log.info("Inserted %d rows into ClickHouse.", len(rows))


def fetch_and_store_point(lat: float, lon: float) -> int:
    """Fetch Open-Meteo forecast for a single lat/lon, score it, write to ClickHouse."""
    from ingest.points import Point  # avoid circular at module level
    pt = Point(lat=lat, lon=lon, label=f"{lat},{lon}")
    om_data = fetch_open_meteo([pt])
    rows: list[dict] = []
    for hour_key, vars_dict in om_data.get(pt.key, {}).items():
        scores = score_all(vars_dict)
        try:
            ts = datetime.strptime(hour_key, "%Y-%m-%dT%H:00").replace(tzinfo=timezone.utc)
        except ValueError:
            ts = datetime.strptime(hour_key, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
        for disaster_type, score in scores.items():
            rows.append({
                "lat": lat, "lon": lon, "timestamp": ts,
                "disaster_type": disaster_type, "score": score,
                "raw_variables": json.dumps(vars_dict),
            })
    write_scores(rows)
    return len(rows)


def run() -> None:
    log.info("=== HazardWatch ingest start ===")
    om_data = fetch_open_meteo(POINTS)

    rows: list[dict] = []
    for pt in POINTS:
        for hour_key, vars_dict in om_data.get(pt.key, {}).items():
            scores = score_all(vars_dict)
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
                    "raw_variables": json.dumps(vars_dict),
                })

    write_scores(rows)
    log.info("=== HazardWatch ingest complete: %d score rows ===", len(rows))

    try:
        from agents.monitor import run as run_monitor  # noqa: PLC0415
        run_monitor()
    except Exception as exc:
        log.warning("Alert monitor failed: %s", exc)


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        log.error("Ingest failed: %s", exc, exc_info=True)
        sys.exit(1)
