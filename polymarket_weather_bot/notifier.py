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

    def _send(self, text: str) -> None:
        if not self.enabled:
            return
        payload = urllib.parse.urlencode({
            'chat_id': self.chat_id,
            'text': text,
            'disable_web_page_preview': 'true',
        }).encode('utf-8')
        url = f'https://api.telegram.org/bot{self.token}/sendMessage'
        req = urllib.request.Request(url, data=payload, method='POST')
        req.add_header('Content-Type', 'application/x-www-form-urlencoded')
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()

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
