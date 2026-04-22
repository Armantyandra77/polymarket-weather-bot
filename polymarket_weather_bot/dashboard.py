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

    def current_state(self) -> Dict[str, Any]:
        snapshot = self.store.get_last_snapshot() or {}
        controls = self.store.get_controls()
        last_snapshot_at = snapshot.get('timestamp') or snapshot.get('created_at')
        parsed = _parse_iso(last_snapshot_at)
        freshness_seconds = None
        if parsed is not None:
            freshness_seconds = max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())

        positions = snapshot.get('positions', self.store.get_positions())
        trades = snapshot.get('recent_trades', self.store.get_trades(20))
        signals = snapshot.get('recent_signals', self.store.get_signals(20))
        recent_errors = self.store.get_errors(12)
        alerts = self._alerts_health(snapshot)
        bot_health = snapshot.get('bot_health') or {}
        live_account = snapshot.get('live_account') or {}

        return {
            'mode': snapshot.get('mode', os.getenv('BOT_MODE', 'paper')),
            'signals_count': snapshot.get('signals_count', 0),
            'open_positions': snapshot.get('open_positions', len(positions)),
            'total_cost': snapshot.get('total_cost', 0.0),
            'live_value': snapshot.get('live_value', 0.0),
            'unrealized_pnl': snapshot.get('unrealized_pnl', 0.0),
            'return_pct': snapshot.get('return_pct', 0.0),
            'positions': positions,
            'recent_signals': signals,
            'recent_trades': trades,
            'recent_errors': recent_errors,
            'markets_scanned': len(self.store.get_markets(1000)),
            'last_error': self.store.get_last_error(),
            'last_snapshot_at': last_snapshot_at,
            'freshness_seconds': freshness_seconds,
            'controls': controls,
            'alerts': alerts,
            'bot_health': bot_health,
            'live_account': live_account,
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
        for key in ('paused', 'force_scan'):
            if key in payload:
                value = payload[key]
                if key == 'force_scan':
                    updates[key] = _truthy(value)
                else:
                    updates[key] = _truthy(value)
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
