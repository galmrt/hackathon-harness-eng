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

from groq import Groq
from dotenv import load_dotenv

from api.db import get_top_risks, get_trend, query_point

load_dotenv()

log = logging.getLogger(__name__)

DISASTER_TYPES = ["wildfire", "flood", "extreme_heat", "winter_storm", "high_wind"]

SYSTEM_PROMPT = """You are HazardWatch's data analyst. You answer questions about
current and recent weather-driven disaster risk across the United States.

You have access to tools that query a live ClickHouse database of hourly risk scores
(0-100) for five disaster types: wildfire, flood, extreme_heat, winter_storm, high_wind.
Scores are computed from Open-Meteo weather forecasts for ~20 major US cities.

Guidelines:
- Always query before answering — never guess at numbers.
- Use multiple tool calls when a question spans several locations or disaster types.
- Give a concise plain-English answer first, then include the raw data that supports it.
- Express scores as low (0-33), moderate (34-66), or high (67-100) in addition to the number.
- When trend data shows a rising score, flag it explicitly as increasing risk.
- If the database has no data yet (empty results), say so clearly rather than fabricating.
"""

TOOLS: list[dict] = [
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


def _dispatch_tool(name: str, inputs: dict) -> Any:
    if name == "query_point":
        return query_point(
            lat=inputs["lat"],
            lon=inputs["lon"],
            disaster_type=inputs["disaster_type"],
            hours=inputs.get("hours", 24),
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


def ask(question: str) -> dict:
    """
    Answer a natural language question about disaster risk.

    Returns:
        {"answer": str, "data": list[dict]}
    """
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    collected_data: list[dict] = []

    while True:
        response = client.chat.completions.create(
            model="llama3-groq-70b-8192-tool-use-preview",
            max_tokens=2048,
            tools=TOOLS,
            tool_choice="auto",
            messages=messages,
        )

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

    return {"answer": msg.content or "", "data": collected_data}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    question = " ".join(sys.argv[1:]) or "Where is wildfire risk highest right now?"
    result = ask(question)
    print("\n=== Answer ===")
    print(result["answer"])
    print("\n=== Data ===")
    print(json.dumps(result["data"], indent=2, default=str))
