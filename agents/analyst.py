"""
Q&A Analyst Agent — called by POST /ask.

Takes a natural language question, queries ClickHouse via tool use,
and returns a plain-English answer + structured data.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dotenv import load_dotenv
load_dotenv()

import litellm
import httpx
from langfuse.decorators import observe, langfuse_context

from api.db import check_cache, get_top_risks, get_trend, query_point, query_point_by_date
from ingest.poller import fetch_and_store_point, fetch_archive_point

litellm.success_callback = ["langfuse"]
litellm.failure_callback = ["langfuse"]

log = logging.getLogger(__name__)

MODEL = "gemini/gemini-2.5-flash"
DISASTER_TYPES = ["wildfire", "flood", "extreme_heat", "winter_storm", "high_wind"]

SYSTEM_PROMPT = """You are HazardWatch's data analyst. You answer questions about
current and recent weather-driven disaster risk across the United States.

You have access to tools that query a live ClickHouse database of hourly risk scores
(0-100) for five disaster types: wildfire, flood, extreme_heat, winter_storm, high_wind.
Scores are computed from Open-Meteo weather forecasts for any location.

Data-fetching workflow (follow this order for every location):
1. Call geocode to resolve the place name to lat/lon.
2. For CURRENT/FORECAST questions:
   a. Call check_cache to see if fresh data exists.
   b. If not, call fetch_and_store to pull a live forecast.
   c. Then query with query_point, get_top_risks, or get_trend.
3. For HISTORICAL questions (specific past dates or date ranges):
   a. Call fetch_archive with lat, lon, start_date, and end_date (YYYY-MM-DD).
   b. Then call query_archive (NOT query_point) with the same lat, lon, disaster_type, start_date, and end_date to retrieve the stored scores.

Guidelines:
- Always geocode before querying — never guess coordinates.
- Use multiple tool calls when a question spans several locations or disaster types.
- Give a concise plain-English answer. Never dump raw numbers, JSON, or data tables — summarize findings in natural language only.
- Express scores as low (0-33), moderate (34-66), or high (67-100) in addition to the number.
- When trend data shows a rising score, flag it explicitly as increasing risk.
- If geocoding fails or fetch_and_store fails, say so clearly rather than fabricating data.
"""

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "geocode",
            "description": (
                "Resolve a place name (city, region, landmark) to latitude and longitude. "
                "Always call this first before any other location-based tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "Place name to look up, e.g. 'Lake Tahoe' or 'Phoenix'."},
                },
                "required": ["place"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_cache",
            "description": (
                "Check whether fresh risk score data already exists in the database for a location. "
                "Call this first before fetch_and_store to avoid unnecessary API calls."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":           {"type": "number", "description": "Latitude."},
                    "lon":           {"type": "number", "description": "Longitude."},
                    "disaster_type": {"type": "string", "enum": DISASTER_TYPES},
                    "max_age_hours": {"type": "integer", "description": "Acceptable data age in hours (default 2)."},
                },
                "required": ["lat", "lon", "disaster_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_and_store",
            "description": (
                "Fetch a live weather forecast from Open-Meteo for a lat/lon, compute all five risk scores, "
                "and write them to the database. Only call this after check_cache confirms data is missing or stale. "
                "Covers all disaster types in one call — no need to call per type."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat": {"type": "number", "description": "Latitude."},
                    "lon": {"type": "number", "description": "Longitude."},
                },
                "required": ["lat", "lon"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_archive",
            "description": (
                "Fetch historical weather data from the Open-Meteo archive (available back to 1940) "
                "for a lat/lon and date range, compute all five risk scores, and write them to the database. "
                "Use this for historical questions like 'what was wildfire risk in California last August?' "
                "or 'how bad was flooding in Houston in 2017?'. "
                "After calling this, use query_point to read the stored scores."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":        {"type": "number", "description": "Latitude."},
                    "lon":        {"type": "number", "description": "Longitude."},
                    "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format (inclusive)."},
                    "end_date":   {"type": "string", "description": "End date in YYYY-MM-DD format (inclusive). Keep ranges ≤7 days to avoid timeouts."},
                },
                "required": ["lat", "lon", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_point",
            "description": (
                "Get hourly risk score time series for a specific lat/lon and disaster type. "
                "Use this for questions about a specific city or location over time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":           {"type": "number", "description": "Latitude."},
                    "lon":           {"type": "number", "description": "Longitude."},
                    "disaster_type": {"type": "string", "enum": DISASTER_TYPES},
                    "hours":         {"type": "integer", "description": "How many past hours to include (default 24)."},
                },
                "required": ["lat", "lon", "disaster_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_archive",
            "description": (
                "Read stored historical risk scores for a lat/lon and disaster type within an absolute date range. "
                "Use this AFTER fetch_archive has loaded the data — query_point cannot reach historical dates. "
                "Returns hourly scores between start_date and end_date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":           {"type": "number", "description": "Latitude."},
                    "lon":           {"type": "number", "description": "Longitude."},
                    "disaster_type": {"type": "string", "enum": DISASTER_TYPES},
                    "start_date":    {"type": "string", "description": "Start date YYYY-MM-DD (inclusive)."},
                    "end_date":      {"type": "string", "description": "End date YYYY-MM-DD (inclusive)."},
                },
                "required": ["lat", "lon", "disaster_type", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_risks",
            "description": (
                "Return the highest-scoring locations for a disaster type in the last hour. "
                "Use this for questions like 'where is wildfire risk highest right now?'"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "disaster_type": {"type": "string", "enum": DISASTER_TYPES},
                    "limit":         {"type": "integer", "description": "Number of results (default 10)."},
                },
                "required": ["disaster_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trend",
            "description": (
                "Get the score change over the last N hours for a location and disaster type. "
                "Positive delta means rising risk. Use this for 'is X getting worse?' questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":           {"type": "number"},
                    "lon":           {"type": "number"},
                    "disaster_type": {"type": "string", "enum": DISASTER_TYPES},
                    "hours":         {"type": "integer"},
                },
                "required": ["lat", "lon", "disaster_type"],
            },
        },
    },
]


def _geocode(place: str) -> dict:
    resp = httpx.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": place, "count": 1, "language": "en", "format": "json"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json().get("results")
    if not results:
        return {"error": f"No location found for '{place}'"}
    r = results[0]
    return {
        "lat": r["latitude"],
        "lon": r["longitude"],
        "name": r.get("name"),
        "country": r.get("country"),
        "admin1": r.get("admin1"),
    }


@observe(name="tool-call")
def _dispatch_tool(name: str, inputs: dict) -> Any:
    langfuse_context.update_current_observation(input={"tool": name, "args": inputs})
    if name == "geocode":
        return _geocode(inputs["place"])
    if name == "check_cache":
        return check_cache(
            lat=inputs["lat"],
            lon=inputs["lon"],
            disaster_type=inputs["disaster_type"],
            max_age_hours=inputs.get("max_age_hours", 2),
        )
    if name == "fetch_and_store":
        rows_written = fetch_and_store_point(lat=inputs["lat"], lon=inputs["lon"])
        return {"rows_written": rows_written, "status": "ok"}
    if name == "fetch_archive":
        rows_written = fetch_archive_point(
            lat=inputs["lat"],
            lon=inputs["lon"],
            start_date=inputs["start_date"],
            end_date=inputs["end_date"],
        )
        return {"rows_written": rows_written, "status": "ok", "note": "Use query_point to read the stored scores."}
    if name == "query_point":
        return query_point(
            lat=inputs["lat"],
            lon=inputs["lon"],
            disaster_type=inputs["disaster_type"],
            hours=inputs.get("hours", 24),
        )
    if name == "query_archive":
        return query_point_by_date(
            lat=inputs["lat"],
            lon=inputs["lon"],
            disaster_type=inputs["disaster_type"],
            start_date=inputs["start_date"],
            end_date=inputs["end_date"],
        )
    if name == "get_top_risks":
        return get_top_risks(
            disaster_type=inputs["disaster_type"],
            limit=inputs.get("limit", 10),
        )
    if name == "get_trend":
        return get_trend(
            lat=inputs["lat"],
            lon=inputs["lon"],
            disaster_type=inputs["disaster_type"],
            hours=inputs.get("hours", 6),
        )
    raise ValueError(f"Unknown tool: {name}")


def _llm_call(messages: list[dict]) -> Any:
    return litellm.completion(
        model=MODEL,
        api_key=os.environ["GEMINI_API_KEY"],
        max_tokens=2048,
        tools=TOOLS,
        tool_choice="auto",
        messages=messages,
        metadata={
            "trace_id": langfuse_context.get_current_trace_id(),
            "parent_observation_id": langfuse_context.get_current_observation_id(),
            "generation_name": "gemini-llm",
        },
    )


@observe(name="ask")
def ask(question: str) -> dict:
    """
    Answer a natural language question about disaster risk.

    Returns:
        {"answer": str, "data": list[dict]}
    """
    langfuse_context.update_current_observation(input=question)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    collected_data: list[dict] = []

    while True:
        response = _llm_call(messages)

        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        assistant_turn: dict = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            assistant_turn["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        messages.append(assistant_turn)

        if finish_reason == "stop":
            break

        if finish_reason != "tool_calls":
            log.warning("Unexpected finish_reason: %s", finish_reason)
            break

        for tc in msg.tool_calls:
            try:
                inputs = json.loads(tc.function.arguments)
                result = _dispatch_tool(tc.function.name, inputs)
                collected_data.append({"tool": tc.function.name, "input": inputs, "result": result})
            except Exception as exc:
                result = {"error": str(exc)}
                log.error("Tool %s failed: %s", tc.function.name, exc)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    answer = msg.content or ""
    langfuse_context.update_current_observation(output=answer)
    langfuse_context.flush()

    focus = None
    for item in collected_data:
        if item["tool"] == "geocode" and isinstance(item.get("result"), dict):
            r = item["result"]
            if "lat" in r and "lon" in r:
                focus = {"lat": r["lat"], "lon": r["lon"], "name": r.get("name", "")}
                break

    return {"answer": answer, "data": collected_data, "focus": focus}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    question = " ".join(sys.argv[1:]) or "Where is wildfire risk highest right now?"
    result = ask(question)
    print("\n=== Answer ===")
    print(result["answer"])
    print("\n=== Data ===")
    print(json.dumps(result["data"], indent=2, default=str))
