from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .account import PolymarketAccountSync
from .executor import PolymarketLiveExecutor
from .models import Market, Position, Signal, Trade
from .notifier import TelegramNotifier
from .strategy import WeatherStrategy
from .store import Store


class PaperExecutor:
    def __init__(self, store: Store, bankroll: float = 100.0):
        self.store = store
        self.bankroll = bankroll

    def open_position(self, signal: Signal, quantity: float, market: Optional[Market] = None) -> Position:
        now = datetime.now(timezone.utc).isoformat()
        side = "YES" if signal.action == "BUY_YES" else "NO"
        avg_entry = signal.market_prob
        current = signal.market_prob
        pos = Position(
            market_id=signal.market_id,
            question=signal.question,
            side=side,
            quantity=quantity,
            avg_entry_price=avg_entry,
            current_price=current,
            market_prob=signal.market_prob,
            model_prob=signal.model_prob,
            opened_at=now,
            updated_at=now,
            source="paper",
            budget=quantity,
            meta={"city": signal.city, "date": signal.date, "signal_confidence": signal.confidence, "signal_edge": signal.edge},
        )
        self.store.save_position(pos)
        self.store.save_trade(
            Trade(
                market_id=signal.market_id,
                side=side,
                quantity=quantity,
                price=avg_entry,
                reason=signal.rationale,
                created_at=now,
                mode="paper",
                status="filled",
                source="paper",
            )
        )
        return pos

    def mark_to_market(self, market_id: str, current_price: float):
        positions = self.store.get_positions()
        for p in positions:
            if p["market_id"] != market_id:
                continue
            updated = Position(
                market_id=p["market_id"],
                question=p["question"],
                side=p["side"],
                quantity=float(p["quantity"]),
                avg_entry_price=float(p["avg_entry_price"]),
                current_price=current_price,
                market_prob=float(p["market_prob"]),
                model_prob=float(p["model_prob"]),
                opened_at=p["opened_at"],
                updated_at=datetime.now(timezone.utc).isoformat(),
                status=p.get("status", "open"),
                order_id=p.get("order_id"),
                source=p.get("source", "paper"),
                budget=p.get("budget"),
                meta=p.get("meta"),
            )
            self.store.save_position(updated)
            return updated
        return None


class BotEngine:
    def __init__(
        self,
        store: Store,
        strategy: WeatherStrategy,
        mode: str = "paper",
        bankroll: float = 100.0,
        notifier: Optional[TelegramNotifier] = None,
        account_sync: Optional[PolymarketAccountSync] = None,
    ):
        self.store = store
        self.strategy = strategy
        self.mode = mode.strip().lower()
        self.bankroll = bankroll
        self.notifier = notifier or TelegramNotifier.from_env()
        self.account_sync = account_sync
        if self.mode == "live" and account_sync is not None:
            self.executor = PolymarketLiveExecutor.from_env(store, account_sync.config)
        else:
            self.executor = PaperExecutor(store, bankroll=bankroll)

    def scan_and_trade(self, markets: List[Market]) -> Dict[str, Any]:
        self.store.upsert_markets(markets)
        signals = []
        positions_snapshot = self.store.get_positions()
        if self.mode == 'live':
            open_positions = len([p for p in positions_snapshot if str(p.get('source', 'paper')).lower() == 'live' and str(p.get('status', 'open')).lower() == 'open'])
            open_positions_list = [p for p in positions_snapshot if str(p.get('source', 'paper')).lower() == 'live' and str(p.get('status', 'open')).lower() == 'open']
        else:
            open_positions = len([p for p in positions_snapshot if str(p.get('status', 'open')).lower() == 'open'])
            open_positions_list = [p for p in positions_snapshot if str(p.get('status', 'open')).lower() == 'open']
        for market in markets:
            try:
                res = self.strategy.analyze_market(market)
                analysis_record = {
                    'market_id': market.id,
                    'question': market.question,
                    'market': asdict(market),
                    'analysis': {k: v for k, v in res.items() if k not in ('signal', 'forecast')},
                    'created_at': datetime.now(timezone.utc).isoformat(),
                }
                if res.get('forecast'):
                    forecast = dict(res['forecast'])
                    analysis_record['forecast'] = forecast
                    self.store.save_forecast_snapshot({
                        'market_id': market.id,
                        'city': forecast.get('city'),
                        'date': forecast.get('date'),
                        'forecast': forecast,
                        'signal_meta': {'edge_threshold': self.strategy.edge_threshold, 'mode': self.mode},
                        'created_at': datetime.now(timezone.utc).isoformat(),
                    })
                self.store.save_market_scan(analysis_record)
                if res.get('skip'):
                    continue
                signal: Signal = res['signal']
                self.store.save_signal(signal)
                self.store.save_signal_outcome({
                    'market_id': signal.market_id,
                    'question': signal.question,
                    'signal': asdict(signal),
                    'forecast': res.get('forecast'),
                    'market': asdict(market),
                    'created_at': signal.generated_at,
                })
                signals.append(asdict(signal))
                try:
                    self.notifier.notify_signal(signal)
                except Exception:
                    pass
                if self.strategy.should_enter(signal, open_positions):
                    if not self.strategy.passes_risk_limits(signal, open_positions_list, bankroll=self.bankroll):
                        continue
                    qty = self.strategy.recommended_size(signal, bankroll=self.bankroll, positions=open_positions_list)
                    if qty > 0:
                        pos = self.executor.open_position(signal, qty, market=market)
                        if pos is None:
                            continue
                        open_positions += 1
                        open_positions_list.append(asdict(pos))
                        self.store.save_snapshot({"event": "opened_position", "position": asdict(pos)})
                        try:
                            self.notifier.notify_position(pos)
                        except Exception:
                            pass
                # mark-to-market with the current market price as a baseline
                self.executor.mark_to_market(signal.market_id, signal.market_prob)
            except Exception as e:
                payload = {"market_id": market.id, "question": market.question, "error": str(e)}
                self.store.save_error(payload)
                try:
                    self.notifier.notify_error(str(e), payload)
                except Exception:
                    pass
        live_account = None
        if self.account_sync is not None:
            try:
                live_account = self.account_sync.sync()
            except Exception as exc:
                live_account = {
                    "enabled": False,
                    "status": "error",
                    "errors": [str(exc)],
                    "warnings": [],
                    "positions": [],
                    "positions_count": 0,
                    "portfolio_value": 0.0,
                    "balance": {},
                    "open_orders": [],
                    "open_orders_count": 0,
                }
        snapshot = self._build_snapshot(signals, live_account=live_account)
        snapshot['market_scans_count'] = len(self.store.get_market_scans(1000))
        snapshot['forecast_snapshots_count'] = len(self.store.get_forecast_snapshots(1000))
        snapshot['signal_outcomes_count'] = len(self.store.get_signal_outcomes(1000))
        snapshot['forecast_outcomes_count'] = len(self.store.get_forecast_outcomes(1000))
        snapshot['latest_market_scans'] = self.store.get_market_scans(12)
        snapshot['latest_forecast_snapshots'] = self.store.get_forecast_snapshots(12)
        snapshot['latest_signal_outcomes'] = self.store.get_signal_outcomes(12)
        snapshot['latest_forecast_outcomes'] = self.store.get_forecast_outcomes(12)
        snapshot['calibration_summary'] = self.store.get_forecast_calibration_summary()
        self.store.save_snapshot(snapshot)
        return snapshot

    def _build_snapshot(self, signals: List[Dict[str, Any]], live_account: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        positions = self.store.get_positions()
        trades = self.store.get_trades(50)
        total_cost = 0.0
        live_value = 0.0
        unrealized = 0.0
        for p in positions:
            qty = float(p["quantity"])
            avg = float(p["avg_entry_price"])
            cur = float(p["current_price"])
            total_cost += avg * qty
            live_value += cur * qty
            if p["side"].upper() == "YES":
                unrealized += (cur - avg) * qty
            else:
                unrealized += ((1.0 - cur) - (1.0 - avg)) * qty
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "signals_count": len(signals),
            "open_positions": len(positions),
            "total_cost": round(total_cost, 4),
            "live_value": round(live_value, 4),
            "unrealized_pnl": round(unrealized, 4),
            "return_pct": round((unrealized / total_cost * 100.0) if total_cost else 0.0, 2),
            "positions": positions,
            "recent_trades": trades,
            "recent_signals": signals[:20],
            "live_account": live_account or {},
        }
