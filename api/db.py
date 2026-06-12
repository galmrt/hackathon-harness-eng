import os
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


def get_map_snapshot(disaster_type: str) -> list[dict]:
    """Latest score per grid point for map rendering."""
    client = get_client()
    result = client.query(
        """
        SELECT lat, lon, score
        FROM risk_scores
        WHERE disaster_type = {disaster_type:String}
          AND timestamp = (
              SELECT max(timestamp) FROM risk_scores WHERE disaster_type = {disaster_type:String}
          )
        """,
        parameters={"disaster_type": disaster_type},
    )
    return [{"lat": r[0], "lon": r[1], "score": r[2]} for r in result.result_rows]


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
