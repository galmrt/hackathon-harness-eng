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

import litellm
from dotenv import load_dotenv
from langfuse import observe, get_client as get_langfuse

from api.db import get_top_risks, get_trend

load_dotenv()

log = logging.getLogger(__name__)

MODEL = "gemini/gemini-1.5-flash"
ALERT_THRESHOLD = 70
TREND_THRESHOLD = 15

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

Be conservative: only alert when score >= {threshold} OR a score is rising rapidly
(trend delta >= {trend}). Do not alert on stable moderate risk.
""".format(threshold=ALERT_THRESHOLD, trend=TREND_THRESHOLD)

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "get_top_risks",
            "description": (
                "Return the highest-scoring grid points for a disaster type "
                "in the last hour. Use this to find which locations need attention."
            ),
            "parameters": {
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
                    },
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
                "Return the score delta over the last N hours for a specific location "
                "and disaster type. Positive delta = rising risk."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":           {"type": "number", "description": "Latitude."},
                    "lon":           {"type": "number", "description": "Longitude."},
                    "disaster_type": {"type": "string", "enum": DISASTER_TYPES},
                    "hours":         {"type": "integer", "description": "Look-back window in hours (default 6)."},
                },
                "required": ["lat", "lon", "disaster_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_alert",
            "description": "Fire an alert for a high-risk situation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "lat":            {"type": "number"},
                    "lon":            {"type": "number"},
                    "location_name":  {"type": "string", "description": "Human-readable location name."},
                    "disaster_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "One or more disaster types involved.",
                    },
                    "max_score":    {"type": "number", "description": "Highest current risk score (0-100)."},
                    "trend_delta":  {"type": "number", "description": "Score change over the look-back window."},
                    "summary":      {"type": "string", "description": "Plain-English alert message."},
                    "severity":     {
                        "type": "string",
                        "enum": ["watch", "warning", "emergency"],
                        "description": "watch=elevated, warning=high, emergency=extreme/imminent.",
                    },
                },
                "required": ["lat", "lon", "disaster_types", "max_score", "summary", "severity"],
            },
        },
    },
]


@observe(name="tool-call")
def _dispatch_tool(name: str, inputs: dict) -> Any:
    get_langfuse().update_current_observation(input={"tool": name, "args": inputs})
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


@observe(as_type="generation", name="gemini-llm")
def _llm_call(messages: list[dict]) -> Any:
    response = litellm.completion(
        model=MODEL,
        api_key=os.environ["GEMINI_API_KEY"],
        max_tokens=4096,
        tools=TOOLS,
        tool_choice="auto",
        messages=messages,
    )
    if response.usage:
        get_langfuse().update_current_observation(
            model=MODEL,
            usage_details={
                "input_tokens": response.usage.prompt_tokens,
                "output_tokens": response.usage.completion_tokens,
            },
        )
    return response


@observe(name="monitor-run")
def run() -> list[dict]:
    """Run the monitor agent. Returns list of alerts that were fired."""
    trigger = "Ingest complete. Check all disaster types for high or rising risk and send alerts where warranted."
    get_langfuse().update_current_observation(input=trigger)

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": trigger},
    ]
    alerts_fired: list[dict] = []

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
                if tc.function.name == "send_alert":
                    alerts_fired.append(inputs)
            except Exception as exc:
                result = {"error": str(exc)}
                log.error("Tool %s failed: %s", tc.function.name, exc)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    log.info("Monitor complete — %d alert(s) fired.", len(alerts_fired))
    get_langfuse().update_current_observation(output={"alerts_fired": len(alerts_fired), "alerts": alerts_fired})
    get_langfuse().flush()
    return alerts_fired


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    fired = run()
    print(json.dumps(fired, indent=2, default=str))
