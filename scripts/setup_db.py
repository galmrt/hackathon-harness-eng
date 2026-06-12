"""Create the ClickHouse database and risk_scores table."""
import os
import sys
import clickhouse_connect
from dotenv import load_dotenv

load_dotenv()


def main():
    host = os.environ["CLICKHOUSE_HOST"]
    port = int(os.environ.get("CLICKHOUSE_PORT", 8443))
    db = os.environ.get("CLICKHOUSE_DB", "hazardwatch")
    user = os.environ.get("CLICKHOUSE_USER", "default")
    password = os.environ.get("CLICKHOUSE_PASSWORD", "")

    # Connect without db to create it
    client = clickhouse_connect.get_client(host=host, port=port, username=user, password=password, verify=False)
    client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
    print(f"Database '{db}' ready.")

    client = clickhouse_connect.get_client(host=host, port=port, database=db, username=user, password=password, verify=False)
    client.command("""
        CREATE TABLE IF NOT EXISTS risk_scores (
            lat           Float64,
            lon           Float64,
            timestamp     DateTime,
            disaster_type LowCardinality(String),
            score         Float32,
            raw_variables String
        )
        ENGINE = MergeTree()
        ORDER BY (disaster_type, timestamp, lat, lon)
        PARTITION BY toYYYYMMDD(timestamp)
        TTL timestamp + INTERVAL 30 DAY
    """)
    print("Table 'risk_scores' ready.")
    print("Schema setup complete.")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        print(f"Missing environment variable: {e}", file=sys.stderr)
        sys.exit(1)
