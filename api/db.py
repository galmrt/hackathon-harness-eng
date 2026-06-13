import json
import os
from datetime import datetime
import clickhouse_connect
from dotenv import load_dotenv

load_dotenv()


def get_client():
    return clickhouse_connect.get_client(
        host=os.environ["CLICKHOUSE_HOST"],
        port=int(os.environ.get("CLICKHOUSE_PORT", 8443)),
        database=os.environ.get("CLICKHOUSE_DB", "hazardwatch"),
        username=os.environ.get("CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
        verify=False,
    )


def query_point(lat: float, lon: float, disaster_type: str, hours: int = 24) -> list[dict]:
    """Time-series risk scores for a single location."""
    client = get_client()
    result = client.query(
        """
        SELECT timestamp, score, raw_variables
        FROM risk_scores
        WHERE lat = {lat:Float64}
          AND lon = {lon:Float64}
          AND disaster_type = {disaster_type:String}
          AND timestamp >= now() - INTERVAL {hours:Int32} HOUR
        ORDER BY timestamp ASC
        """,
        parameters={"lat": lat, "lon": lon, "disaster_type": disaster_type, "hours": hours},
    )
    return [{"timestamp": str(r[0]), "score": r[1], "raw_variables": r[2]} for r in result.result_rows]


def query_point_by_date(lat: float, lon: float, disaster_type: str, start_date: str, end_date: str) -> list[dict]:
    """Time-series risk scores for a location within an absolute date range (YYYY-MM-DD)."""
    client = get_client()
    result = client.query(
        """
        SELECT timestamp, score, raw_variables
        FROM risk_scores
        WHERE lat = {lat:Float64}
          AND lon = {lon:Float64}
          AND disaster_type = {disaster_type:String}
          AND timestamp >= parseDateTimeBestEffort({start_date:String})
          AND timestamp <  parseDateTimeBestEffort({end_date:String}) + INTERVAL 1 DAY
        ORDER BY timestamp ASC
        """,
        parameters={"lat": lat, "lon": lon, "disaster_type": disaster_type, "start_date": start_date, "end_date": end_date},
    )
    return [{"timestamp": str(r[0]), "score": r[1], "raw_variables": r[2]} for r in result.result_rows]


def get_top_risks(disaster_type: str, limit: int = 10) -> list[dict]:
    """Highest-scoring points for a disaster type in the last hour."""
    client = get_client()
    result = client.query(
        """
        SELECT lat, lon, score, raw_variables
        FROM risk_scores
        WHERE disaster_type = {disaster_type:String}
          AND timestamp >= now() - INTERVAL 1 HOUR
        ORDER BY score DESC
        LIMIT {limit:Int32}
        """,
        parameters={"disaster_type": disaster_type, "limit": limit},
    )
    return [{"lat": r[0], "lon": r[1], "score": r[2], "raw_variables": r[3]} for r in result.result_rows]


def get_map_snapshot(disaster_type: str, at_time: datetime | None = None) -> list[dict]:
    """Latest score per grid point at or before at_time (None = live/now)."""
    client = get_client()
    if at_time is None:
        result = client.query(
            """
            SELECT lat, lon, argMax(score, timestamp) AS score
            FROM risk_scores
            WHERE disaster_type = {disaster_type:String}
              AND timestamp >= now() - INTERVAL 3 MINUTE
            GROUP BY lat, lon
            """,
            parameters={"disaster_type": disaster_type},
        )
    else:
        result = client.query(
            """
            SELECT lat, lon, argMax(score, timestamp) AS score
            FROM risk_scores
            WHERE disaster_type = {disaster_type:String}
              AND timestamp >= {at_time:DateTime} - INTERVAL 4 HOUR
              AND timestamp <= {at_time:DateTime}
            GROUP BY lat, lon
            """,
            parameters={"disaster_type": disaster_type, "at_time": at_time},
        )
    return [{"lat": r[0], "lon": r[1], "score": r[2]} for r in result.result_rows]


def check_cache(lat: float, lon: float, disaster_type: str, max_age_hours: int = 2) -> dict:
    """Return whether fresh data exists for a location and how recent it is."""
    client = get_client()
    result = client.query(
        """
        SELECT count(), max(timestamp)
        FROM risk_scores
        WHERE lat = {lat:Float64}
          AND lon = {lon:Float64}
          AND disaster_type = {disaster_type:String}
          AND timestamp >= now() - INTERVAL {max_age_hours:Int32} HOUR
        """,
        parameters={"lat": lat, "lon": lon, "disaster_type": disaster_type, "max_age_hours": max_age_hours},
    )
    row = result.result_rows[0]
    count, latest_ts = row[0], row[1]
    return {
        "has_data": count > 0,
        "row_count": int(count),
        "latest_timestamp": str(latest_ts) if latest_ts else None,
    }


def get_data_range() -> dict:
    """Return the min/max timestamps of stored risk scores."""
    client = get_client()
    result = client.query("SELECT min(timestamp), max(timestamp), count() FROM risk_scores")
    row = result.result_rows[0]
    return {
        "min_timestamp": str(row[0]) if row[0] else None,
        "max_timestamp": str(row[1]) if row[1] else None,
        "total_rows": int(row[2]),
    }


def write_alert(alert: dict) -> None:
    client = get_client()
    client.insert(
        "alerts",
        [[
            alert.get("lat", 0.0),
            alert.get("lon", 0.0),
            alert.get("location_name", ""),
            json.dumps(alert.get("disaster_types", [])),
            float(alert.get("max_score", 0.0)),
            float(alert.get("trend_delta", 0.0)),
            alert.get("summary", ""),
            alert.get("severity", "watch"),
        ]],
        column_names=["lat", "lon", "location_name", "disaster_types", "max_score", "trend_delta", "summary", "severity"],
    )


def get_recent_alerts(hours: int = 24, limit: int = 20) -> list[dict]:
    client = get_client()
    result = client.query(
        """
        SELECT fired_at, lat, lon, location_name, disaster_types,
               max_score, trend_delta, summary, severity
        FROM alerts
        WHERE fired_at >= now() - INTERVAL {hours:Int32} HOUR
        ORDER BY fired_at DESC
        LIMIT {limit:Int32}
        """,
        parameters={"hours": hours, "limit": limit},
    )
    return [
        {
            "fired_at": str(r[0]),
            "lat": r[1],
            "lon": r[2],
            "location_name": r[3],
            "disaster_types": json.loads(r[4]),
            "max_score": r[5],
            "trend_delta": r[6],
            "summary": r[7],
            "severity": r[8],
        }
        for r in result.result_rows
    ]


def get_trend(lat: float, lon: float, disaster_type: str, hours: int = 6) -> dict:
    """Score delta over the last N hours (positive = rising risk)."""
    client = get_client()
    result = client.query(
        """
        SELECT timestamp, score
        FROM risk_scores
        WHERE lat = {lat:Float64}
          AND lon = {lon:Float64}
          AND disaster_type = {disaster_type:String}
          AND timestamp >= now() - INTERVAL {hours:Int32} HOUR
        ORDER BY timestamp ASC
        """,
        parameters={"lat": lat, "lon": lon, "disaster_type": disaster_type, "hours": hours},
    )
    rows = result.result_rows
    if not rows:
        return {"delta": 0.0, "first_score": None, "last_score": None}
    first, last = rows[0][1], rows[-1][1]
    return {"delta": round(last - first, 1), "first_score": first, "last_score": last}
