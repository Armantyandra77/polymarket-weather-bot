# Polymarket Weather Bot

A noise-filtered weather-market scanner for Polymarket with:

- market discovery via Polymarket public APIs
- weather forecasting via Open-Meteo
- deterministic edge calculation
- paper trading by default
- a split frontend/backend architecture
- a premium Vercel-ready dashboard frontend
- live PnL API over plain HTTP

## What it does

1. Discovers active weather-related Polymarket markets.
2. Tries to parse the market question into a city + temperature bucket.
3. Fetches a weather forecast for that city/date.
4. Estimates model probability and compares it to market price.
5. Emits signals only when the edge exceeds thresholds.
6. Simulates positions/trades in paper mode and stores everything in SQLite.
7. Serves a live dashboard so you can monitor PnL only.

## Run

```bash
cd /home/ubuntu/polymarket-weather-bot
python run_bot.py
```

Then open:

- http://127.0.0.1:8080/

## Frontend / Vercel deploy

The UI is now split into `frontend/index.html` so it can be deployed as a static Vercel app.

### Local preview

Open the static file directly or serve the `frontend/` directory with any static server.

### Vercel

1. Create a new Vercel project from this repo.
2. Set the **Root Directory** to `frontend`.
3. Leave the framework as **Other** / static.
4. Deploy.
5. Point the dashboard to your VPS API with:

```text
https://your-vercel-domain.vercel.app/?api=https://YOUR-VPS-DOMAIN
```

The backend on the VPS stays responsible for the bot loop and `/api/state`.

## Environment variables

- `BOT_DB_PATH` — SQLite file path (default: `./bot.db`)
- `BOT_PORT` — dashboard port (default: `8080`)
- `BOT_POLL_SECONDS` — scan interval (default: `300`)
- `BOT_CONTROL_POLL_SECONDS` — idle/pause control loop interval (default: `20`)
- `BOT_MIN_VOLUME` — minimum market volume (default: `5000`)
- `BOT_MAX_SPREAD` — maximum spread to consider (default: `0.08`)
- `BOT_EDGE_THRESHOLD` — minimum edge to enter (default: `0.10`)
- `BOT_MAX_POSITIONS` — max open positions (default: `3`)
- `BOT_MODE` — `paper` (default) or `live` stub
- `BOT_SERVE_UI` — `1` (default) serves local HTML from the backend, `0` makes the backend API-only
- `BOT_TELEGRAM_BOT_TOKEN` — optional Telegram bot token for alerts
- `BOT_TELEGRAM_CHAT_ID` — optional Telegram chat id for alerts

## API endpoints

- `GET /health` — simple health check
- `GET /api/state` — current dashboard state
- `GET /api/snapshots?limit=120` — snapshot history for the PnL chart
- `GET /api/journal?limit=60` — combined signal / trade / error journal
- `POST /api/control` — pause, resume, or force a rescan

## Notes

- Live Polymarket execution is intentionally left as a guarded stub.
- The system is designed to keep noise low and only surface tradable situations.
- If the weather market question cannot be parsed confidently, the market is skipped.
