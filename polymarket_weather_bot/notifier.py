from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Any, Optional

from .models import Position, Signal


class TelegramNotifier:
    def __init__(self, token: str = '', chat_id: str = ''):
        self.token = token.strip()
        self.chat_id = chat_id.strip()

    @classmethod
    def from_env(cls) -> 'TelegramNotifier':
        token = os.getenv('BOT_TELEGRAM_BOT_TOKEN', '').strip()
        chat_id = os.getenv('BOT_TELEGRAM_CHAT_ID', '').strip()
        return cls(token=token, chat_id=chat_id)

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def health(self) -> dict[str, Any]:
        return {
            'enabled': self.enabled,
            'chat_id_set': bool(self.chat_id),
            'token_set': bool(self.token),
        }

    def _request(self, method: str, params: Optional[dict[str, Any]] = None, timeout: int = 15) -> Any:
        if not self.token:
            return None
        url = f'https://api.telegram.org/bot{self.token}/{method}'
        data = None
        headers: dict[str, str] = {}
        if params is not None:
            data = urllib.parse.urlencode(params, doseq=True).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, data=data, method='POST' if data is not None else 'GET')
        for key, value in headers.items():
            req.add_header(key, value)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode('utf-8')
            return json.loads(raw) if raw else {}

    def send_message(self, text: str, chat_id: Optional[str] = None) -> None:
        target_chat_id = (chat_id or self.chat_id).strip()
        if not self.token or not target_chat_id:
            return
        self._request('sendMessage', {
            'chat_id': target_chat_id,
            'text': text,
            'disable_web_page_preview': 'true',
        }, timeout=15)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> list[dict[str, Any]]:
        if not self.token:
            return []
        params: dict[str, Any] = {'timeout': max(0, int(timeout))}
        if offset is not None:
            params['offset'] = int(offset)
        response = self._request('getUpdates', params, timeout=max(30, timeout + 5)) or {}
        updates = response.get('result') if isinstance(response, dict) else None
        return updates if isinstance(updates, list) else []

    def _send(self, text: str) -> None:
        self.send_message(text)

    def notify_signal(self, signal: Signal) -> None:
        if not self.enabled:
            return
        emoji = '🟢' if signal.action == 'BUY_YES' else '🔴'
        text = (
            f'{emoji} *Polymarket Weather Signal*\n'
            f'Market: {signal.question}\n'
            f'Action: {signal.action}\n'
            f'Edge: {signal.edge:+.2%}\n'
            f'Market prob: {signal.market_prob:.2%}\n'
            f'Model prob: {signal.model_prob:.2%}\n'
            f'Confidence: {signal.confidence:.0%}\n'
            f'Reason: {signal.rationale}'
        )
        self._send(text)

    def notify_position(self, position: Position) -> None:
        if not self.enabled:
            return
        text = (
            f'📥 *Position opened*\n'
            f'Market: {position.question}\n'
            f'Side: {position.side}\n'
            f'Qty: {position.quantity:.2f}\n'
            f'Entry: {position.avg_entry_price:.2%}\n'
            f'Model: {position.model_prob:.2%}\n'
            f'Market: {position.market_prob:.2%}'
        )
        self._send(text)

    def notify_error(self, message: str, context: Optional[dict[str, Any]] = None) -> None:
        if not self.enabled:
            return
        ctx = ''
        if context:
            try:
                ctx = '\n' + json.dumps(context, ensure_ascii=False)[:600]
            except Exception:
                ctx = f'\n{context}'
        self._send(f'⚠️ *Bot error*\n{message}{ctx}')
