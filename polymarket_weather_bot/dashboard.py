from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from .store import Store


FRONTEND_INDEX = Path(__file__).resolve().parent.parent / 'frontend' / 'index.html'


def load_dashboard_html() -> str:
    try:
        return FRONTEND_INDEX.read_text(encoding='utf-8')
    except FileNotFoundError:
        return """<!doctype html><html><body><h1>Polymarket Weather Bot</h1><p>Missing frontend/index.html</p></body></html>"""


HTML_TEMPLATE = load_dashboard_html()


class DashboardState:
    def __init__(self, store: Store):
        self.store = store

    def current_state(self) -> Dict[str, Any]:
        snapshot = self.store.get_last_snapshot() or {}
        return {
            "mode": snapshot.get("mode", "paper"),
            "signals_count": snapshot.get("signals_count", 0),
            "open_positions": snapshot.get("open_positions", len(self.store.get_positions())),
            "total_cost": snapshot.get("total_cost", 0.0),
            "live_value": snapshot.get("live_value", 0.0),
            "unrealized_pnl": snapshot.get("unrealized_pnl", 0.0),
            "return_pct": snapshot.get("return_pct", 0.0),
            "positions": snapshot.get("positions", self.store.get_positions()),
            "recent_signals": snapshot.get("recent_signals", self.store.get_signals(20)),
            "recent_trades": snapshot.get("recent_trades", self.store.get_trades(20)),
            "markets_scanned": len(self.store.get_markets(1000)),
            "last_error": self.store.get_last_error(),
        }


class Handler(BaseHTTPRequestHandler):
    state: DashboardState = None  # type: ignore

    def _send(self, code: int, content_type: str, body: bytes):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, 'text/plain; charset=utf-8', b'')

    def do_GET(self):  # noqa: N802
        if self.path in ('/', '/index.html'):
            self._send(200, 'text/html; charset=utf-8', HTML_TEMPLATE.encode('utf-8'))
            return
        if self.path == '/api/state':
            data = self.state.current_state()
            self._send(200, 'application/json; charset=utf-8', json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
            return
        if self.path == '/health':
            self._send(200, 'application/json; charset=utf-8', b'{"ok":true}')
            return
        self._send(404, 'text/plain; charset=utf-8', b'not found')

    def log_message(self, fmt, *args):
        return


def serve_dashboard(store: Store, port: int = 8080, serve_ui: bool = True):
    Handler.state = DashboardState(store)
    server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
