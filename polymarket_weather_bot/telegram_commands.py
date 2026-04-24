from __future__ import annotations

import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple

from .dashboard import DashboardState
from .notifier import TelegramNotifier
from .store import Store

_COMMAND_RE = re.compile(r'^/([A-Za-z0-9_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$', re.S)


class TelegramCommandService:
    def __init__(
        self,
        store: Store,
        notifier: TelegramNotifier,
        allowed_chat_id: str | None = None,
        poll_timeout: int = 25,
        enabled: bool = True,
    ):
        self.store = store
        self.notifier = notifier
        self.allowed_chat_id = (allowed_chat_id or '').strip() or None
        self.poll_timeout = max(1, int(poll_timeout))
        self.enabled = bool(enabled and self.notifier.token)
        self._offset: Optional[int] = None
        self._state = DashboardState(store)

    @classmethod
    def from_env(cls, store: Store, notifier: Optional[TelegramNotifier] = None) -> 'TelegramCommandService':
        notifier = notifier or TelegramNotifier.from_env()
        enabled = str(os.getenv('BOT_TELEGRAM_COMMANDS', '1')).strip().lower() not in {'0', 'false', 'no', 'off'}
        allowed_chat_id = os.getenv('BOT_TELEGRAM_COMMAND_CHAT_ID') or notifier.chat_id or None
        poll_timeout = int(os.getenv('BOT_TELEGRAM_COMMAND_POLL_TIMEOUT', '25'))
        return cls(store, notifier, allowed_chat_id=allowed_chat_id, poll_timeout=poll_timeout, enabled=enabled)

    def _truncate(self, text: str, limit: int = 3900) -> str:
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 12)] + '\n…truncated…'

    def _record_command(
        self,
        *,
        update: Dict[str, Any],
        message: Dict[str, Any],
        command: str,
        args: str,
        reply_text: str,
        status: str,
    ) -> None:
        chat = message.get('chat') or {}
        user = message.get('from') or {}
        payload = {
            'chat_id': str(chat.get('id') or ''),
            'user_id': str(user.get('id') or ''),
            'username': str(user.get('username') or user.get('first_name') or ''),
            'command': command,
            'args': args,
            'message_text': str(message.get('text') or ''),
            'reply_text': reply_text,
            'status': status,
            'raw_update': update,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        self.store.save_telegram_command(payload)

    def _is_authorized_chat(self, chat_id: str) -> bool:
        if not self.allowed_chat_id:
            return True
        return str(chat_id).strip() == self.allowed_chat_id

    def _parse_message(self, text: str) -> Tuple[str, str] | None:
        match = _COMMAND_RE.match((text or '').strip())
        if not match:
            return None
        command = match.group(1).lower().strip()
        args = (match.group(2) or '').strip()
        return command, args

    def _format_time_ago(self, iso_value: Optional[str]) -> str:
        if not iso_value:
            return 'unknown'
        try:
            parsed = datetime.fromisoformat(str(iso_value).replace('Z', '+00:00'))
        except Exception:
            return str(iso_value)
        delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
        seconds = max(0, int(delta.total_seconds()))
        if seconds < 60:
            return f'{seconds}s ago'
        if seconds < 3600:
            return f'{seconds // 60}m ago'
        if seconds < 86400:
            return f'{seconds // 3600}h ago'
        return f'{seconds // 86400}d ago'

    def _common_city(self, signals: Iterable[Dict[str, Any]]) -> Optional[str]:
        counts = Counter()
        latest: Dict[str, str] = {}
        for signal in signals:
            city = str(signal.get('city') or '').strip()
            if not city:
                continue
            counts[city.lower()] += 1
            latest[city.lower()] = city
        if not counts:
            return None
        city_key, _ = counts.most_common(1)[0]
        return latest.get(city_key, city_key.title())

    def _resolve_city(self, args: str, state: Dict[str, Any]) -> Optional[str]:
        if args.strip():
            return args.strip()
        signals = state.get('recent_signals') or self.store.get_signals(20)
        city = self._common_city(signals)
        if city:
            return city
        forecasts = state.get('latest_forecast_snapshots') or self.store.get_forecast_snapshots(20)
        for snap in forecasts:
            forecast = snap.get('forecast') or {}
            city = forecast.get('city') or snap.get('city')
            if city:
                return str(city)
        return None

    def _build_top_reply(self, state: Dict[str, Any], limit: int = 5) -> str:
        limit = max(1, min(int(limit), 10))
        markets = self.store.get_markets(limit)
        positions = state.get('positions') or []
        signals = state.get('recent_signals') or []
        lines = [
            f'Top markets ({limit})',
            f"mode: {state.get('mode', 'paper')} | paused: {state.get('health', {}).get('paused', False)} | open positions: {len(positions)}",
        ]
        if not markets:
            lines.append('No markets have been scanned yet.')
        else:
            for idx, market in enumerate(markets[:limit], start=1):
                lines.append(
                    f"{idx}. {market.get('question', 'Unknown market')}"
                    f" | vol {float(market.get('volume') or 0):,.0f}"
                    f" | yes {float(market.get('yes_price') or 0):.2%}"
                    f" | spread {float(market.get('yes_price') or 0) - (1.0 - float(market.get('no_price') or 0)):+.2%}"
                )
        if signals:
            top_signal = signals[0]
            lines.append(
                f"Latest signal: {top_signal.get('action', 'SIGNAL')} {top_signal.get('city', '')} {top_signal.get('edge', 0):+.2%}"
                if isinstance(top_signal.get('edge'), (int, float)) else
                f"Latest signal: {top_signal.get('action', 'SIGNAL')} {top_signal.get('city', '')}"
            )
        return '\n'.join(lines)

    def _build_city_reply(self, state: Dict[str, Any], city: Optional[str]) -> str:
        city = (city or '').strip()
        if not city:
            return 'City summary: no city provided and none could be inferred yet. Try `/city Seoul`.'
        lc = city.lower()
        signals = [s for s in (self.store.get_signals(50)) if str(s.get('city') or '').lower() == lc]
        markets = [m for m in self.store.get_markets(100) if lc in str(m.get('question') or '').lower() or lc in str(m.get('slug') or '').lower()]
        forecasts = []
        for snap in self.store.get_forecast_snapshots(20):
            forecast = snap.get('forecast') or {}
            if str(forecast.get('city') or snap.get('city') or '').lower() == lc:
                forecasts.append(snap)
        lines = [f'City summary: {city}']
        lines.append(f"signals: {len(signals)} | markets: {len(markets)} | forecasts: {len(forecasts)}")
        if signals:
            lines.append('Recent signals:')
            for sig in signals[:3]:
                edge = sig.get('edge')
                edge_text = f" {float(edge):+.2%}" if isinstance(edge, (int, float)) else ''
                lines.append(f"- {sig.get('action', 'SIGNAL')}{edge_text} • {sig.get('question', 'Unknown market')}")
        if forecasts:
            latest = forecasts[0].get('forecast') or {}
            stats = latest.get('stats') or {}
            if stats or latest:
                lines.append(
                    'Latest forecast: '
                    f"{latest.get('date', forecasts[0].get('date', 'unknown'))} "
                    f"mean {float(stats.get('mean') or latest.get('mean') or 0):.1f}°C"
                )
            else:
                lines.append('Latest forecast: unavailable')
        if not signals and not markets:
            lines.append('No recent bot activity found for this city yet.')
        return '\n'.join(lines)

    def _build_diag_reply(self, state: Dict[str, Any]) -> str:
        controls = state.get('controls') or {}
        alerts = state.get('alerts') or {}
        health = state.get('health') or {}
        command_counts = self.store.get_telegram_command_counts(5)
        recent_commands = self.store.get_telegram_command_history(5)
        lines = [
            'Bot diagnostics',
            f"mode: {state.get('mode', 'paper')}",
            f"paused: {bool(health.get('paused', False))} | force_scan: {bool(controls.get('force_scan', False))}",
            f"alerts: {bool(alerts.get('enabled', False))} | token: {bool(alerts.get('token_set', False))} | chat_id: {bool(alerts.get('chat_id_set', False))}",
            f"open positions: {state.get('open_positions', 0)} | markets scanned: {state.get('market_scans_count', 0)}",
            f"signals: {state.get('signals_count', 0)} | forecasts: {state.get('forecast_snapshots_count', 0)} | outcomes: {state.get('signal_outcomes_count', 0)}",
            f"freshness: {self._format_time_ago(state.get('last_snapshot_at'))}",
        ]
        if state.get('latest_telegram_commands'):
            lines.append(f"recent command activity: {len(state['latest_telegram_commands'])} stored")
        if command_counts:
            lines.append('Command usage:')
            for item in command_counts:
                lines.append(f"- /{item.get('command', '')}: {item.get('count', 0)}")
        if recent_commands:
            lines.append('Recent commands:')
            for item in recent_commands[:3]:
                lines.append(
                    f"- /{item.get('command', '')} {item.get('args', '')}".rstrip()
                    + f" [{self._format_time_ago(item.get('created_at'))}]"
                )
        return '\n'.join(lines)

    def build_reply(self, command: str, args: str, state: Optional[Dict[str, Any]] = None) -> str:
        state = state or self._state.current_state()
        command = command.lower().strip()
        if command == 'top':
            limit = 5
            if args.strip().isdigit():
                limit = int(args.strip())
            return self._build_top_reply(state, limit=limit)
        if command == 'city':
            resolved = self._resolve_city(args, state)
            return self._build_city_reply(state, resolved)
        if command == 'diag':
            return self._build_diag_reply(state)
        return (
            'Available commands:\n'
            '/top [n] - top scanned markets and latest signal\n'
            '/city [name] - summarize activity for a city\n'
            '/diag - bot diagnostics and command history'
        )

    def handle_update(self, update: Dict[str, Any]) -> bool:
        message = update.get('message') or update.get('edited_message')
        if not isinstance(message, dict):
            return False
        text = str(message.get('text') or '').strip()
        parsed = self._parse_message(text)
        if not parsed:
            return False
        command, args = parsed
        chat = message.get('chat') or {}
        chat_id = str(chat.get('id') or '')
        if not chat_id:
            return False
        if not self._is_authorized_chat(chat_id):
            self._record_command(update=update, message=message, command=command, args=args, reply_text='unauthorized', status='unauthorized')
            return True

        state = self._state.current_state()
        reply_text = self.build_reply(command, args, state=state)
        reply_text = self._truncate(reply_text)
        try:
            self.notifier.send_message(reply_text, chat_id=chat_id)
            status = 'handled'
        except Exception as exc:
            reply_text = f'Command error: {exc}'
            status = 'error'
            try:
                self.notifier.send_message(reply_text, chat_id=chat_id)
            except Exception:
                pass
        self._record_command(update=update, message=message, command=command, args=args, reply_text=reply_text, status=status)
        return True

    def poll_once(self) -> int:
        updates = self.notifier.get_updates(offset=self._offset, timeout=self.poll_timeout)
        next_offset = self._offset
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                update_id = int(update.get('update_id'))
            except Exception:
                continue
            next_offset = max(next_offset or update_id + 1, update_id + 1)
            self.handle_update(update)
        self._offset = next_offset
        return len(updates)

    def run_forever(self, stop_event: Optional[threading.Event] = None):
        if not self.enabled:
            return
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                self.poll_once()
            except Exception as exc:
                payload = {'error': str(exc), 'stage': 'telegram_command_poll'}
                try:
                    self.store.save_error(payload)
                except Exception:
                    pass
                try:
                    self.notifier.notify_error(str(exc), payload)
                except Exception:
                    pass
                time.sleep(5)
