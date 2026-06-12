"""FastAPI app — risk map data + /ask Q&A endpoint."""

from __future__ import annotations

import logging
import os
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.db import get_map_snapshot, get_top_risks, query_point

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


@app.get("/risks/map/{disaster_type}")
def map_snapshot(disaster_type: DisasterType):
    """Latest score per grid point — for map rendering."""
    return get_map_snapshot(disaster_type)


@app.get("/risks/top/{disaster_type}")
def top_risks(disaster_type: DisasterType, limit: int = 10):
    """Highest-scoring locations for a disaster type in the last hour."""
    return get_top_risks(disaster_type, limit=limit)


@app.get("/risks/point")
def point_timeseries(lat: float, lon: float, disaster_type: DisasterType, hours: int = 24):
    """Time-series scores for a specific location."""
    return query_point(lat, lon, disaster_type, hours)


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
