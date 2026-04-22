from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List

from .bot import BotEngine
from .dashboard import serve_dashboard
from .models import Market
from .polymarket import discover_weather_markets
from .strategy import WeatherStrategy
from .store import Store


def run_once(store: Store, engine: BotEngine, min_volume: float) -> Dict[str, Any]:
    markets = discover_weather_markets(min_volume=min_volume)
    return engine.scan_and_trade(markets)


def run_forever():
    db_path = os.getenv('BOT_DB_PATH', './bot.db')
    port = int(os.getenv('BOT_PORT', '8080'))
    poll_seconds = int(os.getenv('BOT_POLL_SECONDS', '300'))
    min_volume = float(os.getenv('BOT_MIN_VOLUME', '5000'))
    max_spread = float(os.getenv('BOT_MAX_SPREAD', '0.08'))
    edge_threshold = float(os.getenv('BOT_EDGE_THRESHOLD', '0.10'))
    max_positions = int(os.getenv('BOT_MAX_POSITIONS', '3'))
    mode = os.getenv('BOT_MODE', 'paper')
    serve_ui = os.getenv('BOT_SERVE_UI', '1') not in ('0', 'false', 'False', 'no', 'NO')

    store = Store(db_path)
    strategy = WeatherStrategy(
        min_volume=min_volume,
        max_spread=max_spread,
        edge_threshold=edge_threshold,
        max_positions=max_positions,
    )
    engine = BotEngine(store, strategy, mode=mode)
    serve_dashboard(store, port=port, serve_ui=serve_ui)

    def worker():
        while True:
            try:
                snapshot = run_once(store, engine, min_volume=min_volume)
                store.save_snapshot(snapshot)
            except Exception as e:
                store.save_error({"error": str(e), "stage": "worker_loop"})
            time.sleep(poll_seconds)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return store, engine


if __name__ == '__main__':
    store, engine = run_forever()
    print('Running on http://127.0.0.1:%s' % os.getenv('BOT_PORT', '8080'))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
