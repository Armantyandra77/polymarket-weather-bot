from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

from .store import Store


FRONTEND_INDEX = Path(__file__).resolve().parent.parent / 'frontend' / 'index.html'


def load_dashboard_html() -> str:
    try:
        return FRONTEND_INDEX.read_text(encoding='utf-8')
    except FileNotFoundError:
        return """<!doctype html><html><body><h1>Polymarket Weather Bot</h1><p>Missing frontend/index.html</p></body></html>"""


HTML_TEMPLATE = load_dashboard_html()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 'y')


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(normalized)
    except Exception:
        return None


class DashboardState:
    def __init__(self, store: Store):
        self.store = store

    def _alerts_health(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        alerts = snapshot.get('alerts') or {}
        if not isinstance(alerts, dict):
            alerts = {}
        if not alerts:
            alerts = {
                'enabled': _truthy(os.getenv('BOT_TELEGRAM_BOT_TOKEN')) and _truthy(os.getenv('BOT_TELEGRAM_CHAT_ID')),
                'chat_id_set': _truthy(os.getenv('BOT_TELEGRAM_CHAT_ID')),
                'token_set': _truthy(os.getenv('BOT_TELEGRAM_BOT_TOKEN')),
            }
        return alerts

    def _forecast_summary(self, forecasts: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_source: Dict[str, Dict[str, Any]] = {}
        blend_count = 0
        blend_conf_total = 0.0
        blend_disagreement_total = 0.0
        source_rows = 0
        for snap in forecasts:
            forecast = snap.get('forecast') or {}
            sources = forecast.get('sources') or []
            blend = forecast.get('blend') or {}
            if sources:
                source_rows += 1
                blend_count += 1
                blend_conf_total += float(blend.get('confidence', 0.0) or 0.0)
                blend_disagreement_total += float(blend.get('disagreement_c', 0.0) or 0.0)
            for source in sources:
                name = str(source.get('source') or 'unknown')
                bucket = by_source.setdefault(name, {'count': 0, 'confidence_total': 0.0, 'weight_total': 0.0, 'available_dates_total': 0})
                bucket['count'] += 1
                bucket['confidence_total'] += float(source.get('confidence', 0.0) or 0.0)
                bucket['weight_total'] += float(source.get('weight', 0.0) or 0.0)
                bucket['available_dates_total'] += int(source.get('available_dates', 0) or 0)
        top_sources = []
        for name, bucket in by_source.items():
            count = max(1, bucket['count'])
            top_sources.append({
                'source': name,
                'count': bucket['count'],
                'avg_confidence': round(bucket['confidence_total'] / count, 3),
                'avg_weight': round(bucket['weight_total'] / count, 3),
                'avg_available_dates': round(bucket['available_dates_total'] / count, 1),
            })
        top_sources.sort(key=lambda item: (item['count'], item['avg_confidence'], item['avg_weight']), reverse=True)
        return {
            'forecast_rows': len(forecasts),
            'forecast_rows_with_sources': source_rows,
            'blend_count': blend_count,
            'avg_blend_confidence': round(blend_conf_total / blend_count, 3) if blend_count else 0.0,
            'avg_blend_disagreement_c': round(blend_disagreement_total / blend_count, 2) if blend_count else 0.0,
            'by_source': by_source,
            'top_sources': top_sources[:4],
        }

    def current_state(self) -> Dict[str, Any]:
        snapshot = self.store.get_last_snapshot() or {}
        controls = self.store.get_controls()
        last_snapshot_at = snapshot.get('timestamp') or snapshot.get('created_at')
        parsed = _parse_iso(last_snapshot_at)
        freshness_seconds = None
        if parsed is not None:
            freshness_seconds = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())

        positions = snapshot.get('positions') or self.store.get_positions()
        trades = snapshot.get('recent_trades') or self.store.get_trades(20)
        signals = snapshot.get('recent_signals') or self.store.get_signals(20)
        recent_errors = self.store.get_errors(12)
        alerts = self._alerts_health(snapshot)
        bot_health = snapshot.get('bot_health') or {}
        live_account = snapshot.get('live_account') or {}
        live_mode_active = bool(live_account.get('enabled')) and str(live_account.get('status', '')).lower() in {'connected', 'read_only', 'partial'}

        live_positions: List[Dict[str, Any]] = []
        live_trades: List[Dict[str, Any]] = []
        if live_mode_active:
            for p in live_account.get('positions') or []:
                try:
                    size = float(p.get('size') or 0.0)
                    avg_price = float(p.get('avg_price') or 0.0)
                    current_price = float(p.get('cur_price') or 0.0)
                    current_value = float(p.get('current_value') or (size * current_price))
                    live_positions.append({
                        'market_id': p.get('slug') or p.get('condition_id') or p.get('title') or 'live-position',
                        'question': p.get('title') or p.get('slug') or 'Live position',
                        'side': p.get('outcome') or 'LIVE',
                        'quantity': size,
                        'avg_entry_price': avg_price,
                        'current_price': current_price,
                        'market_prob': current_price,
                        'model_prob': current_price,
                        'opened_at': p.get('updated_at') or p.get('opened_at') or p.get('updatedAt') or last_snapshot_at,
                        'updated_at': p.get('updated_at') or p.get('updatedAt') or last_snapshot_at,
                        'status': 'open',
                        'order_id': p.get('order_id'),
                        'source': 'live',
                        'budget': None,
                        'meta': {'current_value': current_value, 'slug': p.get('slug'), 'condition_id': p.get('condition_id')},
                        'unrealized_pnl': float(p.get('cash_pnl') or 0.0),
                    })
                except Exception:
                    continue
            live_trades = [t for t in trades if str(t.get('source', '')).lower() == 'live' or str(t.get('mode', '')).lower() == 'live']
            positions = live_positions
            trades = live_trades

        total_cost = snapshot.get('total_cost', 0.0)
        live_value = snapshot.get('live_value', 0.0)
        unrealized_pnl = snapshot.get('unrealized_pnl', 0.0)
        return_pct = snapshot.get('return_pct', 0.0)
        if live_mode_active:
            total_cost = round(sum(float(p.get('avg_entry_price', 0.0)) * float(p.get('quantity', 0.0)) for p in live_positions), 4)
            live_value = round(sum(float((p.get('meta') or {}).get('current_value', 0.0)) for p in live_positions), 4)
            unrealized_pnl = round(sum(float(p.get('unrealized_pnl', 0.0)) for p in live_positions), 4)
            return_pct = round((unrealized_pnl / total_cost * 100.0) if total_cost else 0.0, 2)

        market_scans = self.store.get_market_scans(12)
        forecasts = self.store.get_forecast_snapshots(12)
        signal_outcomes = self.store.get_signal_outcomes(12)
        order_snapshots = self.store.get_account_order_snapshots(12)
        order_events = self.store.get_account_order_events(12)
        telegram_commands = self.store.get_telegram_command_history(12)
        forecast_summary = self._forecast_summary(forecasts)
        calibration_summary = self.store.get_forecast_calibration_summary()
        open_orders = live_account.get('open_orders') or live_account.get('order_history') or []
        if not isinstance(open_orders, list):
            open_orders = []
        order_activity_summary = {
            'order_events_count': len(order_events),
            'open_orders_count': len(open_orders),
            'live_trades_count': len(live_trades),
        }
        latest_account_order_sections = [
            {
                'key': 'order_events',
                'title': 'Order events',
                'note': 'Lifecycle updates from live account sync and fills.',
                'count': len(order_events),
            },
            {
                'key': 'open_orders',
                'title': 'Open orders',
                'note': 'Resting orders currently visible in the live account.',
                'count': len(open_orders),
            },
            {
                'key': 'live_trades',
                'title': 'Live trades',
                'note': 'Executed trades and fills streamed from the live executor.',
                'count': len(live_trades),
            },
        ]

        return {
            'mode': snapshot.get('mode', os.getenv('BOT_MODE', 'paper')),
            'signals_count': snapshot.get('signals_count', 0),
            'open_positions': len(positions) if live_mode_active else snapshot.get('open_positions', len(positions)),
            'total_cost': total_cost,
            'live_value': live_value,
            'unrealized_pnl': unrealized_pnl,
            'return_pct': return_pct,
            'positions': positions,
            'live_positions': live_positions,
            'recent_signals': signals,
            'recent_trades': trades,
            'live_trades': live_trades,
            'recent_errors': recent_errors,
            'markets_scanned': len(self.store.get_markets(1000)),
            'last_error': self.store.get_last_error(),
            'last_snapshot_at': last_snapshot_at,
            'freshness_seconds': freshness_seconds,
            'controls': controls,
            'alerts': alerts,
            'bot_health': bot_health,
            'live_account': live_account,
            'market_scans_count': len(self.store.get_market_scans(1000)),
            'forecast_snapshots_count': len(self.store.get_forecast_snapshots(1000)),
            'signal_outcomes_count': len(self.store.get_signal_outcomes(1000)),
            'forecast_outcomes_count': len(self.store.get_forecast_outcomes(1000)),
            'latest_market_scans': market_scans,
            'latest_forecast_snapshots': forecasts,
            'latest_signal_outcomes': signal_outcomes,
            'latest_forecast_outcomes': self.store.get_forecast_outcomes(12),
            'latest_account_order_snapshots': order_snapshots,
            'latest_account_order_events': order_events,
            'forecast_summary': forecast_summary,
            'forecast_sources_summary': forecast_summary,
            'calibration_summary': calibration_summary,
            'order_activity_summary': order_activity_summary,
            'latest_account_order_sections': latest_account_order_sections,
            'telegram_commands_count': len(telegram_commands),
            'latest_telegram_commands': telegram_commands,
            'telegram_command_usage': self.store.get_telegram_command_counts(5),
            'health': {
                'paused': _truthy(controls.get('paused', False)),
                'force_scan': _truthy(controls.get('force_scan', False)),
                'alerts_enabled': bool(alerts.get('enabled')),
                'fresh': freshness_seconds is not None and freshness_seconds < 1800,
            },
        }

    def snapshots(self, limit: int = 120) -> List[Dict[str, Any]]:
        return self.store.get_snapshots(limit)

    def journal(self, limit: int = 60) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []

        for trade in self.store.get_trades(limit):
            entries.append({
                'kind': 'trade',
                'created_at': trade.get('created_at'),
                'title': f"{trade.get('side', 'TRADE')} • {trade.get('quantity', 0)} @ {trade.get('price', 0)}",
                'detail': trade.get('reason', ''),
                'market_id': trade.get('market_id'),
                'payload': trade,
            })

        for signal in self.store.get_signals(limit):
            entries.append({
                'kind': 'signal',
                'created_at': signal.get('generated_at'),
                'title': f"{signal.get('action', 'SIGNAL')} • edge {signal.get('edge', 0):+.2f}" if isinstance(signal.get('edge'), (int, float)) else f"{signal.get('action', 'SIGNAL')}",
                'detail': signal.get('rationale', ''),
                'market_id': signal.get('market_id'),
                'payload': signal,
            })

        for command in self.store.get_telegram_command_history(limit):
            entries.append({
                'kind': 'command',
                'created_at': command.get('created_at'),
                'title': f"/{command.get('command', '')} {command.get('args', '')}".strip(),
                'detail': command.get('reply_text', ''),
                'market_id': None,
                'payload': command,
            })

        for err in self.store.get_errors(limit):
            entries.append({
                'kind': 'error',
                'created_at': err.get('created_at'),
                'title': 'Error',
                'detail': err.get('error', err.get('message', 'Unexpected error')),
                'market_id': err.get('market_id'),
                'payload': err,
            })

        entries.sort(key=lambda item: item.get('created_at') or '', reverse=True)
        return entries[:limit]


class Handler(BaseHTTPRequestHandler):
    state: DashboardState = None  # type: ignore
    serve_ui: bool = True

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Auth-Token')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: Any):
        self._send(code, 'application/json; charset=utf-8', json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8'))

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, 'text/plain; charset=utf-8', b'')

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path in ('/', '/index.html'):
            if not self.serve_ui:
                self._send(404, 'text/plain; charset=utf-8', b'not found')
                return
            self._send(200, 'text/html; charset=utf-8', HTML_TEMPLATE.encode('utf-8'))
            return

        if path == '/api/state':
            self._json(200, self.state.current_state())
            return

        if path == '/api/snapshots':
            limit = int(query.get('limit', ['120'])[0])
            self._json(200, {'snapshots': self.state.snapshots(limit)})
            return

        if path == '/api/journal':
            limit = int(query.get('limit', ['60'])[0])
            self._json(200, {'journal': self.state.journal(limit)})
            return

        if path in ('/health', '/api/health'):
            self._json(200, {'ok': True})
            return

        self._send(404, 'text/plain; charset=utf-8', b'not found')

    def do_POST(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != '/api/control':
            self._send(404, 'text/plain; charset=utf-8', b'not found')
            return

        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
        except Exception:
            length = 0
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            payload = {}

        updates: Dict[str, Any] = {}
        for key in ('paused', 'force_scan', 'prepare_collateral'):
            if key in payload:
                updates[key] = _truthy(payload[key])
                self.state.store.set_control(key, updates[key])

        if 'pause' in payload and 'paused' not in updates:
            updates['paused'] = _truthy(payload['pause'])
            self.state.store.set_control('paused', updates['paused'])

        if 'action' in payload:
            action = str(payload['action']).strip().lower()
            if action == 'pause':
                updates['paused'] = True
                self.state.store.set_control('paused', True)
            elif action == 'resume':
                updates['paused'] = False
                self.state.store.set_control('paused', False)
            elif action in ('scan', 'rescan', 'force_scan'):
                updates['force_scan'] = True
                self.state.store.set_control('force_scan', True)
            elif action in ('prepare_collateral', 'prep_collateral', 'collateral'):
                updates['prepare_collateral'] = True
                self.state.store.set_control('prepare_collateral', True)

        response = {
            'ok': True,
            'controls': self.state.store.get_controls(),
            'state': self.state.current_state(),
            'updated': updates,
        }
        self._json(200, response)

    def log_message(self, fmt, *args):
        return


def serve_dashboard(store: Store, port: int = 8080, serve_ui: bool = True):
    Handler.state = DashboardState(store)
    Handler.serve_ui = serve_ui
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
