from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict

from .account import PolymarketAccountSync
from .bot import BotEngine
from .dashboard import serve_dashboard
from .notifier import TelegramNotifier
from .polymarket import discover_weather_markets
from .strategy import WeatherStrategy
from .store import Store
from .telegram_commands import TelegramCommandService


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'y')


def _clear_stale_signer_block(store: Store, live_account: Dict[str, Any]) -> bool:
    controls = store.get_controls()
    stage = str(controls.get('live_execution_block_stage') or '').strip().lower()
    reason = str(controls.get('live_execution_block_reason') or '')
    if stage != 'signer_mismatch':
        return False
    if not _truthy(live_account.get('trading_ready')) and str(live_account.get('status') or '').lower() not in {'connected', 'ready'}:
        return False
    if 'signer address' not in reason.lower() and 'signer_mismatch' not in reason.lower():
        return False
    store.set_control('live_execution_blocked', False)
    store.set_control('live_execution_block_reason', '')
    store.set_control('live_execution_block_stage', '')
    if _truthy(controls.get('paused', False)):
        store.set_control('paused', False)
    return True


def run_once(store: Store, engine: BotEngine, min_volume: float) -> Dict[str, Any]:
    markets = discover_weather_markets(min_volume=min_volume, store=store)
    return engine.scan_and_trade(markets)


def run_forever():
    db_path = os.getenv('BOT_DB_PATH', './bot.db')
    port = int(os.getenv('BOT_PORT', '8080'))
    poll_seconds = int(os.getenv('BOT_POLL_SECONDS', '300'))
    control_poll_seconds = int(os.getenv('BOT_CONTROL_POLL_SECONDS', '20'))
    account_sync_seconds = int(os.getenv('BOT_ACCOUNT_SYNC_SECONDS', '15'))
    min_volume = float(os.getenv('BOT_MIN_VOLUME', '8000'))
    max_spread = float(os.getenv('BOT_MAX_SPREAD', '0.08'))
    edge_threshold = float(os.getenv('BOT_EDGE_THRESHOLD', '0.10'))
    max_positions = int(os.getenv('BOT_MAX_POSITIONS', '3'))
    mode = os.getenv('BOT_MODE', 'paper')
    serve_ui = os.getenv('BOT_SERVE_UI', '1') not in ('0', 'false', 'False', 'no', 'NO')

    store = Store(db_path)
    notifier = TelegramNotifier.from_env()
    strategy = WeatherStrategy(
        min_volume=min_volume,
        max_spread=max_spread,
        edge_threshold=edge_threshold,
        max_positions=max_positions,
    )
    account_sync = PolymarketAccountSync.from_env()
    engine = BotEngine(store, strategy, mode=mode, notifier=notifier, account_sync=account_sync)
    serve_dashboard(store, port=port, serve_ui=serve_ui)

    command_service = TelegramCommandService.from_env(store, notifier=notifier)
    if command_service.enabled:
        threading.Thread(target=command_service.run_forever, daemon=True).start()

    def worker():
        while True:
            try:
                if account_sync is None or not account_sync.enabled():
                    time.sleep(account_sync_seconds)
                    continue
                controls = store.get_controls()
                paused = _truthy(controls.get('paused', False))
                force_scan = _truthy(controls.get('force_scan', False))
                if paused and not force_scan:
                    time.sleep(control_poll_seconds)
                    continue

                snapshot = run_once(store, engine, min_volume=min_volume)
                snapshot['controls'] = controls
                snapshot['alerts'] = notifier.health()
                snapshot['bot_health'] = {
                    'paused': paused,
                    'force_scan': force_scan,
                    'backend': 'running',
                    'scan_mode': mode,
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                }
                store.save_snapshot(snapshot)

                if force_scan:
                    store.set_control('force_scan', False)
                time.sleep(control_poll_seconds if paused else poll_seconds)
            except Exception as e:
                payload = {'error': str(e), 'stage': 'worker_loop'}
                store.save_error(payload)
                try:
                    notifier.notify_error(str(e), payload)
                except Exception:
                    pass
                time.sleep(control_poll_seconds)

    def account_worker():
        while True:
            try:
                if account_sync is None or not account_sync.enabled():
                    time.sleep(account_sync_seconds)
                    continue
                controls = store.get_controls()
                prepare_collateral = _truthy(controls.get('prepare_collateral'))
                collateral_prep = None
                if prepare_collateral:
                    collateral_prep = account_sync.prepare_collateral()
                    store.set_control('prepare_collateral', False)
                live_account = account_sync.sync()
                _clear_stale_signer_block(store, live_account)
                if collateral_prep is not None:
                    live_account['collateral_flow'] = collateral_prep
                    live_account['balance'] = dict(live_account.get('balance') or {}, flow=collateral_prep)
                snapshot = store.get_last_snapshot() or {}
                prev_live_account = snapshot.get('live_account') if isinstance(snapshot, dict) else {}
                prev_orders = prev_live_account.get('order_history') or prev_live_account.get('open_orders') or []
                current_orders = live_account.get('order_history') or live_account.get('open_orders') or []
                prev_by_id = {str(o.get('id') or o.get('order_id') or o.get('token_id') or idx): o for idx, o in enumerate(prev_orders) if isinstance(o, dict)}
                curr_by_id = {str(o.get('id') or o.get('order_id') or o.get('token_id') or idx): o for idx, o in enumerate(current_orders) if isinstance(o, dict)}
                order_events = []
                now_iso = datetime.now(timezone.utc).isoformat()
                for order_id, order in curr_by_id.items():
                    if order_id not in prev_by_id:
                        order_events.append({
                            'order_id': order_id,
                            'event_type': 'created',
                            'payload': order,
                            'created_at': order.get('updated_at') or order.get('created_at') or now_iso,
                        })
                    else:
                        prev_order = prev_by_id[order_id]
                        if json.dumps(prev_order, sort_keys=True, default=str) != json.dumps(order, sort_keys=True, default=str):
                            order_events.append({
                                'order_id': order_id,
                                'event_type': 'updated',
                                'payload': {'before': prev_order, 'after': order},
                                'created_at': order.get('updated_at') or now_iso,
                            })
                for order_id, order in prev_by_id.items():
                    if order_id not in curr_by_id:
                        order_events.append({
                            'order_id': order_id,
                            'event_type': 'closed',
                            'payload': order,
                            'created_at': now_iso,
                        })
                if current_orders:
                    live_account['order_history'] = current_orders
                    live_account['order_history_count'] = len(current_orders)
                store.save_account_order_snapshot({'created_at': now_iso, 'orders': current_orders, 'source': live_account.get('order_source', 'clob-open-orders')}, source=str(live_account.get('order_source', 'clob-open-orders')))
                if order_events:
                    store.save_account_order_events(order_events)
                merged = dict(snapshot)
                merged['mode'] = mode
                merged['timestamp'] = datetime.now(timezone.utc).isoformat()
                merged['live_account'] = live_account
                merged.setdefault('controls', store.get_controls())
                merged.setdefault('alerts', notifier.health())
                store.save_snapshot(merged)
                time.sleep(account_sync_seconds)
            except Exception as e:
                payload = {'error': str(e), 'stage': 'account_sync_loop'}
                store.save_error(payload)
                try:
                    notifier.notify_error(str(e), payload)
                except Exception:
                    pass
                time.sleep(account_sync_seconds)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    account_t = threading.Thread(target=account_worker, daemon=True)
    account_t.start()
    return store, engine


if __name__ == '__main__':
    store, engine = run_forever()
    print('Running on http://127.0.0.1:%s' % os.getenv('BOT_PORT', '8080'))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
