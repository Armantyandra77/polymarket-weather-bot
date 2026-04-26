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
- `BOT_ACCOUNT_SYNC_SECONDS` — live Polymarket account refresh interval for open orders / portfolio sync (default: `15`)
- `BOT_MIN_VOLUME` — minimum market volume (default: `5000`)
- `BOT_MAX_SPREAD` — maximum spread to consider (default: `0.08`)
- `BOT_EDGE_THRESHOLD` — minimum edge to enter (default: `0.10`)
- `BOT_MAX_POSITIONS` — max open positions (default: `3`)
- `BOT_ALLOWED_CITIES` — optional comma-separated city allowlist; if set, only those cities can trade
- `BOT_BLOCKED_CITIES` — optional comma-separated city denylist
- `BOT_ALLOWED_TERMS` — optional comma-separated keyword allowlist matched against the market question
- `BOT_BLOCKED_TERMS` — optional comma-separated keyword denylist matched against the market question
- `BOT_MODE` — `paper` (default) or `live` for live CLOB execution
- `BOT_LIVE_ORDER_STYLE` — `market` (default) or `limit`
- `BOT_LIVE_MIN_ORDER_USD` — skip live entries below this size (default: `1`)
- `BOT_LIVE_MAX_ORDER_USD` — cap each live entry size in USD (default: `25`)
- `BOT_LIVE_POST_ONLY` — when `BOT_LIVE_ORDER_STYLE=limit`, use post-only maker orders (`0`/`1`)
- `BOT_LIVE_LIMIT_BUFFER_BPS` — optional price buffer for limit orders in basis points (default: `0`)
- `BOT_SERVE_UI` — `1` (default) serves local HTML from the backend, `0` makes the backend API-only
- `BOT_TELEGRAM_BOT_TOKEN` — optional Telegram bot token for alerts
- `BOT_TELEGRAM_CHAT_ID` — optional Telegram chat id for alerts
- `BOT_POLYMARKET_WALLET_ADDRESS` — wallet/proxy wallet address for Polymarket profile / CLOB auth
- `BOT_POLYMARKET_PROXY_ADDRESS` — optional authenticated proxy address derived from a logged-in Polymarket session
- `BOT_POLYMARKET_AUTHENTICATION_TYPE` — optional auth type (`magic` or `eoa`) when using a session-derived proxy address
- `BOT_POLYMARKET_SESSION_HINT` — optional session hint string or JSON, e.g. `0xabc...123:magic` or `{"proxyAddress":"0xabc...123","authenticationType":"magic"}`
- `BOT_POLYMARKET_SESSION_HINT_PATH` — optional file path whose contents are used as `BOT_POLYMARKET_SESSION_HINT`
- `BOT_POLYMARKET_FUNDER_ADDRESS` — fallback wallet address if `BOT_POLYMARKET_WALLET_ADDRESS` is not set
- `BOT_POLYMARKET_DEPOSIT_ADDRESS` — optional Solana deposit address used for wallet balance lookup when deposits are on Solana
- `BOT_POLYMARKET_PRIVATE_KEY` — signer private key used for L1 EIP-712 signing and for deriving CLOB API credentials
- `BOT_POLYMARKET_API_KEY` — cached CLOB API key derived from `BOT_POLYMARKET_PRIVATE_KEY` (optional if you let the bot derive it)
- `BOT_POLYMARKET_API_SECRET` — cached CLOB API secret derived from `BOT_POLYMARKET_PRIVATE_KEY` (optional if you let the bot derive it)
- `BOT_POLYMARKET_API_PASSPHRASE` — cached CLOB API passphrase derived from `BOT_POLYMARKET_PRIVATE_KEY` (optional if you let the bot derive it)
- `BOT_POLYMARKET_CLOB_HOST` — CLOB host override (default: `https://clob.polymarket.com`)
- `BOT_POLYMARKET_RPC_URL` — Polygon RPC used to read EVM wallet USDC balance (default: `https://polygon-bor.publicnode.com`)
- `BOT_POLYMARKET_SOLANA_RPC_URL` — Solana RPC used when the account address is a Solana deposit address (default: `https://api.mainnet-beta.solana.com`)
- `BOT_POLYMARKET_SOLANA_USDC_MINT` — USDC mint used for Solana balance lookup (default: `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`)
- `BOT_POLYMARKET_CHAIN_ID` — chain id used by the CLOB client (default: `137`)
- `BOT_POLYMARKET_SIGNATURE_TYPE` — CLOB wallet type (`0` = EOA, `1` = Magic Link/proxy wallet, `2` = browser/embedded proxy wallet)

## Bootstrap auth helper

If `BOT_POLYMARKET_API_KEY` / `BOT_POLYMARKET_API_SECRET` / `BOT_POLYMARKET_API_PASSPHRASE` are missing, derive them from `BOT_POLYMARKET_PRIVATE_KEY` and write them back to the system env file:

```bash
python scripts/bootstrap_polymarket_auth.py --env-file /etc/default/polymarket-weather-bot
```

To derive and restart the service in one shot:

```bash
python scripts/bootstrap_polymarket_auth.py --env-file /etc/default/polymarket-weather-bot --restart
```

Use `--force` if you want to re-derive and overwrite the cached API creds.

## API endpoints

- `GET /health` — simple health check
- `GET /api/state` — current dashboard state
- `GET /api/snapshots?limit=120` — snapshot history for the PnL chart
- `GET /api/journal?limit=60` — combined signal / trade / error journal
- `POST /api/control` — pause, resume, force a rescan, or trigger collateral preparation (prep USDC → pUSD collateral flow)

## Notes

- Live Polymarket execution is intentionally left as a guarded stub.
- Live account sync can show portfolio, balance, collateral, allowance, and open positions when the Polymarket wallet/credentials env vars are set.
- Wallet balance is reported separately from CLOB collateral; the dashboard now surfaces both so you can see when funds still need to be wrapped/transferred into pUSD collateral.
- If Polymarket login is done via Gmail/Google, export the logged-in session's proxy address as `BOT_POLYMARKET_SESSION_HINT` or `BOT_POLYMARKET_PROXY_ADDRESS`; the bot does not need the Gmail password.
- The system is designed to keep noise low and only surface tradable situations.
- If the weather market question cannot be parsed confidently, the market is skipped.
- To make the bot trade automatically with your own rules, set the strategy env vars above or tell me the exact rule set and I can encode it into the strategy layer.
