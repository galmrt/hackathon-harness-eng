"""FastAPI app — risk map data + /ask Q&A endpoint."""

from __future__ import annotations

import logging
import os
from typing import Literal

from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.db import get_data_range, get_map_snapshot, get_recent_alerts, get_top_risks, query_point

load_dotenv()

log = logging.getLogger(__name__)

app = FastAPI(title="HazardWatch API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DisasterType = Literal["wildfire", "flood", "extreme_heat", "winter_storm", "high_wind"]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/data-range")
def data_range():
    """Returns the min/max timestamps and row count of stored risk scores."""
    return get_data_range()


@app.get("/risks/map/{disaster_type}")
def map_snapshot(disaster_type: DisasterType, at: str | None = None):
    """Latest score per grid point. at=ISO datetime (UTC) for historical view, omit for live."""
    at_time = datetime.fromisoformat(at.replace("Z", "+00:00")) if at else None
    return get_map_snapshot(disaster_type, at_time=at_time)


@app.get("/risks/top/{disaster_type}")
def top_risks(disaster_type: DisasterType, limit: int = 10):
    """Highest-scoring locations for a disaster type in the last hour."""
    return get_top_risks(disaster_type, limit=limit)


@app.get("/risks/point")
def point_timeseries(lat: float, lon: float, disaster_type: DisasterType, hours: int = 24):
    """Time-series scores for a specific location."""
    return query_point(lat, lon, disaster_type, hours)


@app.get("/alerts")
def alerts(hours: int = 24, limit: int = 20):
    """Recent alerts fired by the monitor agent."""
    return get_recent_alerts(hours=hours, limit=limit)


class BackfillRequest(BaseModel):
    date: str          # YYYY-MM-DD — single day
    lat: float | None = None
    lon: float | None = None


@app.post("/backfill")
def backfill(body: BackfillRequest, background_tasks: BackgroundTasks):
    """Load one day of archive data from Open-Meteo into ClickHouse."""
    from ingest.poller import backfill_points  # noqa: PLC0415
    from ingest.points import POINTS, Point    # noqa: PLC0415

    if body.lat is not None and body.lon is not None:
        points = [Point(lat=body.lat, lon=body.lon, label=f"{body.lat},{body.lon}")]
    else:
        points = list(POINTS)

    background_tasks.add_task(backfill_points, points, body.date, body.date)
    return {"status": "started", "points": len(points), "date": body.date}


class RegionRequest(BaseModel):
    lat1: float
    lon1: float
    lat2: float
    lon2: float


@app.post("/fetch-region")
def fetch_region(body: RegionRequest):
    """Fetch and store risk scores for a grid of points within a bounding box."""
    from ingest.poller import fetch_and_store_point  # noqa: PLC0415

    lat_min = min(body.lat1, body.lat2)
    lat_max = max(body.lat1, body.lat2)
    lon_min = min(body.lon1, body.lon2)
    lon_max = max(body.lon1, body.lon2)

    lat_range = lat_max - lat_min
    lon_range = lon_max - lon_min

    # Target a 5×5 grid max (25 points); step at least 0.1°
    steps = 4
    lat_step = max(lat_range / steps, 0.1)
    lon_step = max(lon_range / steps, 0.1)

    points: list[tuple[float, float]] = []
    lat = lat_min
    while lat <= lat_max + 1e-9:
        lon = lon_min
        while lon <= lon_max + 1e-9:
            points.append((round(lat, 3), round(lon, 3)))
            lon += lon_step
        lat += lat_step

    points = points[:25]

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows_per_point = list(pool.map(lambda pt: fetch_and_store_point(lat=pt[0], lon=pt[1]), points))

    return {"points_fetched": len(points), "rows_written": sum(rows_per_point)}


class AskRequest(BaseModel):
    question: str


@app.post("/ask")
def ask(body: AskRequest):
    """Q&A agent — answers natural language questions about disaster risk."""
    try:
        from agents.analyst import ask as analyst_ask  # noqa: PLC0415
        return analyst_ask(body.question)
    except Exception as exc:
        log.error("Analyst agent failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


# Serve frontend — must be mounted last so API routes take precedence
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir) and os.listdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
