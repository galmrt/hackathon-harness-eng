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

OPEN_METEO_URL         = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_VARS = (
    "precipitation,relative_humidity_2m,soil_moisture_0_to_1cm,"
    "snow_depth,wind_gusts_10m,temperature_2m,wind_speed_10m"
)


def _val(data: list, i: int, fallback: float) -> float:
    v = data[i]
    return float(v) if v is not None else fallback


def fetch_open_meteo(points, past_days: int = 0) -> dict[str, dict]:
    """Return {point_key: {hour_iso: {var: value}}} for forecast + optional recent history."""
    result: dict[str, dict] = {}
    with httpx.Client(timeout=60) as http:
        for pt in points:
            params = {
                "latitude":      pt.lat,
                "longitude":     pt.lon,
                "hourly":        OPEN_METEO_VARS,
                "forecast_days": 2,
                "timezone":      "UTC",
            }
            if past_days > 0:
                params["past_days"] = min(past_days, 92)
            resp = http.get(OPEN_METEO_URL, params=params)
            resp.raise_for_status()
            result[pt.key] = _parse_hourly(resp.json()["hourly"])
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


def _parse_hourly(data: dict) -> dict[str, dict]:
    """Parse Open-Meteo hourly response into {hour_iso: {var: value}}."""
    by_hour: dict[str, dict] = {}
    for i, t in enumerate(data["time"]):
        by_hour[t] = {
            "temperature":       _val(data["temperature_2m"], i, 20.0),
            "wind_speed":        _val(data["wind_speed_10m"], i, 0.0),
            "wind_gusts":        _val(data["wind_gusts_10m"], i, 0.0),
            "precipitation":     _val(data["precipitation"], i, 0.0),
            "relative_humidity": _val(data["relative_humidity_2m"], i, 50.0),
            "soil_moisture":     _val(data["soil_moisture_0_to_1cm"], i, 0.2),
            "snow_depth":        _val(data["snow_depth"], i, 0.0),
        }
    return by_hour


def fetch_open_meteo_archive(points, start_date: str, end_date: str) -> dict[str, dict]:
    """Fetch historical hourly data from Open-Meteo archive (back to 1940)."""
    result: dict[str, dict] = {}
    with httpx.Client(timeout=120) as http:
        for pt in points:
            resp = http.get(
                OPEN_METEO_ARCHIVE_URL,
                params={
                    "latitude":   pt.lat,
                    "longitude":  pt.lon,
                    "start_date": start_date,
                    "end_date":   end_date,
                    "hourly":     OPEN_METEO_VARS,
                    "timezone":   "UTC",
                },
            )
            resp.raise_for_status()
            result[pt.key] = _parse_hourly(resp.json()["hourly"])
    return result


def _rows_from_om_data(om_data: dict, points) -> list[dict]:
    rows: list[dict] = []
    for pt in points:
        for hour_key, vars_dict in om_data.get(pt.key, {}).items():
            scores = score_all(vars_dict)
            try:
                ts = datetime.strptime(hour_key, "%Y-%m-%dT%H:00").replace(tzinfo=timezone.utc)
            except ValueError:
                ts = datetime.strptime(hour_key, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            for disaster_type, score in scores.items():
                rows.append({
                    "lat": pt.lat, "lon": pt.lon, "timestamp": ts,
                    "disaster_type": disaster_type, "score": score,
                    "raw_variables": json.dumps(vars_dict),
                })
    return rows


def backfill_points(points, start_date: str, end_date: str) -> int:
    """Fetch archive data for given points and write to ClickHouse."""
    log.info("Backfilling %d points from %s to %s", len(points), start_date, end_date)
    om_data = fetch_open_meteo_archive(points, start_date, end_date)
    rows = _rows_from_om_data(om_data, points)
    if rows:
        write_scores(rows)
    log.info("Backfill complete: %d rows written", len(rows))
    return len(rows)


def fetch_archive_point(lat: float, lon: float, start_date: str, end_date: str) -> int:
    """Fetch Open-Meteo archive data for a single lat/lon and date range, score it, write to ClickHouse."""
    from ingest.points import Point  # noqa: PLC0415
    pt = Point(lat=lat, lon=lon, label=f"{lat},{lon}")
    om_data = fetch_open_meteo_archive([pt], start_date, end_date)
    rows = _rows_from_om_data(om_data, [pt])
    if rows:
        write_scores(rows)
    return len(rows)


def fetch_and_store_point(lat: float, lon: float) -> int:
    """Fetch Open-Meteo forecast for a single lat/lon, score it, write to ClickHouse."""
    from ingest.points import Point  # noqa: PLC0415
    pt = Point(lat=lat, lon=lon, label=f"{lat},{lon}")
    om_data = fetch_open_meteo([pt])
    rows = _rows_from_om_data(om_data, [pt])
    write_scores(rows)
    return len(rows)


def run() -> None:
    log.info("=== HazardWatch ingest start ===")
    om_data = fetch_open_meteo(POINTS)
    rows = _rows_from_om_data(om_data, POINTS)
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
