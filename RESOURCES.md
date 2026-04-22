# Resources used by the weather bot

## Polymarket public APIs
- Gamma discovery: `https://gamma-api.polymarket.com/public-search?q=weather`
- Gamma events: `https://gamma-api.polymarket.com/events`
- Gamma markets: `https://gamma-api.polymarket.com/markets`

## Weather APIs
- Open-Meteo geocoding: `https://geocoding-api.open-meteo.com/v1/search`
- Open-Meteo forecast: `https://api.open-meteo.com/v1/forecast`

## Local runtime
- Python 3.11+
- SQLite
- Built-in `http.server` dashboard

## Trading mode
- Default mode: paper
- Live execution: guarded stub only, to avoid unsafe accidental trading without wallet credentials
