"""
Alert Monitor Agent — runs after every ingest.

Reads recent risk scores from ClickHouse, reasons about multi-variable
combinations and rising trends, and fires alerts when warranted.

Alert delivery is stubbed (logged); swap _deliver_alert() for Composio later.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import anthropic
from dotenv import load_dotenv

from api.db import get_top_risks, get_trend

load_dotenv()

log = logging.getLogger(__name__)

ALERT_THRESHOLD = 70       # score that triggers investigation
TREND_THRESHOLD = 15       # rising delta that flags urgency

DISASTER_TYPES = ["wildfire", "flood", "extreme_heat", "winter_storm", "high_wind"]

SYSTEM_PROMPT = """You are HazardWatch's alert monitor. After each hourly ingest you
receive access to tools that read live risk scores from a ClickHouse database.

Your job:
1. Check the top-scoring locations for each disaster type.
2. For any location scoring above {threshold}, check its trend over the last 6 hours.
3. Look for dangerous combinations at the same location (e.g. wildfire + high_wind,
   flood + extreme_rain, winter_storm + high_wind = blizzard conditions).
4. Decide which situations warrant an alert.
5. Call send_alert for each situation that warrants one — include location, disaster
   types involved, current score, trend direction, and a plain-English summary
   a homeowner or fire department dispatcher would understand.

Be conservative: only alert when score ≥ {threshold} OR a score is rising rapidly
(trend delta ≥ {trend}). Do not alert on stable moderate risk.
""".format(threshold=ALERT_THRESHOLD, trend=TREND_THRESHOLD)

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "get_top_risks",
        "description": (
            "Return the highest-scoring grid points for a disaster type "
            "in the last hour. Use this to find which locations need attention."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "disaster_type": {
                    "type": "string",
                    "enum": DISASTER_TYPES,
                    "description": "Disaster type to query.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max number of locations to return (default 5).",
                    "default": 5,
                },
            },
            "required": ["disaster_type"],
        },
    },
    {
        "name": "get_trend",
        "description": (
            "Return the score delta over the last N hours for a specific location "
            "and disaster type. Positive delta = rising risk."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lat":           {"type": "number", "description": "Latitude."},
                "lon":           {"type": "number", "description": "Longitude."},
                "disaster_type": {"type": "string", "enum": DISASTER_TYPES},
                "hours":         {"type": "integer", "description": "Look-back window in hours (default 6).", "default": 6},
            },
            "required": ["lat", "lon", "disaster_type"],
        },
    },
    {
        "name": "send_alert",
        "description": "Fire an alert for a high-risk situation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lat":           {"type": "number"},
                "lon":           {"type": "number"},
                "location_name": {"type": "string", "description": "Human-readable location name."},
                "disaster_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "One or more disaster types involved.",
                },
                "max_score":     {"type": "number", "description": "Highest current risk score (0–100)."},
                "trend_delta":   {"type": "number", "description": "Score change over the look-back window."},
                "summary":       {"type": "string", "description": "Plain-English alert message."},
                "severity":      {
                    "type": "string",
                    "enum": ["watch", "warning", "emergency"],
                    "description": "watch=elevated, warning=high, emergency=extreme/imminent.",
                },
            },
            "required": ["lat", "lon", "disaster_types", "max_score", "summary", "severity"],
        },
    },
]


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def _dispatch_tool(name: str, inputs: dict) -> Any:
    if name == "get_top_risks":
        return get_top_risks(
            disaster_type=inputs["disaster_type"],
            limit=inputs.get("limit", 5),
        )
    if name == "get_trend":
        return get_trend(
            lat=inputs["lat"],
            lon=inputs["lon"],
            disaster_type=inputs["disaster_type"],
            hours=inputs.get("hours", 6),
        )
    if name == "send_alert":
        return _deliver_alert(inputs)
    raise ValueError(f"Unknown tool: {name}")


def _deliver_alert(alert: dict) -> dict:
    """Stub — log the alert. Replace body with Composio call when ready."""
    severity = alert.get("severity", "watch").upper()
    types = ", ".join(alert.get("disaster_types", []))
    log.warning(
        "[ALERT %s] %s | types=%s score=%.0f delta=%+.0f | %s",
        severity,
        alert.get("location_name", f"{alert['lat']},{alert['lon']}"),
        types,
        alert.get("max_score", 0),
        alert.get("trend_delta", 0),
        alert.get("summary", ""),
    )
    return {"status": "sent", "alert": alert}


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def run() -> list[dict]:
    """Run the monitor agent. Returns list of alerts that were fired."""
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                "Ingest complete. Check all disaster types for high or rising risk "
                "and send alerts where warranted."
            ),
        }
    ]

    alerts_fired: list[dict] = []

    while True:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            log.warning("Unexpected stop_reason: %s", response.stop_reason)
            break

        # Execute all tool calls and collect results
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result = _dispatch_tool(block.name, block.input)
                if block.name == "send_alert":
                    alerts_fired.append(block.input)
            except Exception as exc:
                result = {"error": str(exc)}
                log.error("Tool %s failed: %s", block.name, exc)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        messages.append({"role": "user", "content": tool_results})

    log.info("Monitor complete — %d alert(s) fired.", len(alerts_fired))
    return alerts_fired


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fired = run()
    print(json.dumps(fired, indent=2, default=str))
