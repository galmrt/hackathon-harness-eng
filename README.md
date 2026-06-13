# HazardWatch

US weather-driven disaster risk dashboard for homeowners and fire departments. Pulls hourly forecasts from Open-Meteo, computes risk scores for wildfire, flood, extreme heat, winter storm, and high wind, stores them in ClickHouse, and renders them on an interactive AI-powered map.

## Features

- **Live risk map** — color-coded circles across US grid points, auto-refreshed every minute
- **Historical playback** — navigate to any past hour; load archive data back to 1940 with a single click
- **Region fetch** — draw a rectangle on the map to pull live risk data for any custom area (up to 25 points)
- **Alert panel** — AI-generated alerts for rising or high-severity risk, with severity badges and click-to-fly
- **Chat analyst** — ask natural language questions about current or historical risk; map pans to the queried location

## Stack

| Layer | Technology |
|---|---|
| Backend | Python, FastAPI |
| Forecast data | Open-Meteo API (free, no auth) |
| Database | ClickHouse (time-series risk scores + alerts) |
| AI agents | Google Gemini `gemini-2.5-flash` via LiteLLM |
| Observability | Langfuse (traces, tool calls, LLM generations) |
| Frontend | Leaflet.js, leaflet-draw |
| Deployment | Render |

## Architecture

```
Open-Meteo API
    │
    ▼
ingest/poller.py          pulls hourly forecasts for US grid points
    │  scorer.py          computes 0-100 risk scores per disaster type
    ▼
ClickHouse                risk_scores(lat, lon, timestamp, disaster_type, score)
    │                     alerts(fired_at, location, severity, summary, ...)
    ├── agents/monitor.py   alert monitor — runs after each ingest, writes alerts
    └── api/main.py         FastAPI — serves map, leaderboard, time-series, chat

agents/analyst.py         Q&A agent — tool-calling loop, reads live + archive data
frontend/index.html       Leaflet map + draw tool + time picker + alerts + chat
```

**Disaster types**: `wildfire`, `flood`, `extreme_heat`, `winter_storm`, `high_wind`

**Risk variables** (from Open-Meteo): `temperature_2m`, `wind_speed_10m`, `wind_gusts_10m`, `precipitation`, `relative_humidity_2m`, `soil_moisture_0_to_1cm`, `snow_depth`

## Getting started

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
GEMINI_API_KEY=

CLICKHOUSE_HOST=<service>.us-east-2.aws.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_DB=hazardwatch
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=

LANGFUSE_SECRET_KEY=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
```

### 3. Set up the database schema

```bash
python3 scripts/setup_db.py
```

### 4. Run the ingest (populates initial data)

```bash
python3 -m ingest.poller
```

### 5. Start the backend

```bash
uvicorn api.main:app --reload
```

Open `http://localhost:8000` in your browser.

## API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/data-range` | Min/max timestamps + row count in DB |
| GET | `/risks/map/{type}` | Map snapshot. `?at=ISO` for historical, omit for live |
| GET | `/risks/top/{type}` | Top-scoring locations for a disaster type |
| GET | `/risks/point` | Hourly time-series for a lat/lon |
| GET | `/alerts` | Recent alerts. `?hours=24&limit=20` |
| POST | `/fetch-region` | Ingest live forecast for a bounding box grid |
| POST | `/backfill` | Load one archive day. Body: `{"date": "YYYY-MM-DD"}` |
| POST | `/ask` | Chat analyst. Body: `{"question": "..."}` |

## Agents

### Alert Monitor (`agents/monitor.py`)
Runs automatically after every ingest cycle. Reads top risk scores and 6-hour trends, reasons about multi-variable combinations (e.g. low humidity + high wind + high temperature → elevated wildfire risk), and persists alerts to ClickHouse. Alerts expire after 7 days.

### Q&A Analyst (`agents/analyst.py`)
Triggered by `POST /ask`. Autonomous tool-calling loop with 8 tools:

| Tool | Purpose |
|---|---|
| `geocode` | Resolve any place name to lat/lon |
| `check_cache` | Check if fresh data exists in DB |
| `fetch_and_store` | Pull live forecast + store scores |
| `fetch_archive` | Pull historical data from archive API + store |
| `query_point` | Hourly time-series (recent data) |
| `query_archive` | Hourly time-series by absolute date range (historical) |
| `get_top_risks` | Highest-scoring locations right now |
| `get_trend` | Score delta over the last N hours |

Response includes a `focus: {lat, lon, name}` field — the frontend uses this to pan and zoom the map to the queried location.

## Deployment

The project is configured for [Render](https://render.com) via `render.yaml`. To deploy:

1. Connect the repository in the Render dashboard
2. Set all environment variables from the `.env` section above
3. Trigger the first deploy

## Sponsors

| Sponsor | Role |
|---|---|
| [ClickHouse](https://clickhouse.com) | Time-series risk score and alerts database |
| [Google Gemini](https://deepmind.google/technologies/gemini/) | LLM inference for both agents |
| [Langfuse](https://langfuse.com) | Agent observability — traces, tool calls, LLM generations |
| [Render](https://render.com) | Cloud deployment |
| [Composio](https://composio.dev) | Alert delivery (stubbed) |
