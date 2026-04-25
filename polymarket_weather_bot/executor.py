from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OpenOrderParams, OrderArgs, OrderType

from .account import PolymarketAccountConfig
from .models import Market, Position, Signal, Trade
from .store import Store


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _pick(payload: Any, *keys: str) -> Any:
    if isinstance(payload, dict):
        lowered = {str(k).lower(): k for k in payload.keys()}
        for key in keys:
            actual = lowered.get(str(key).lower())
            if actual is not None:
                return payload.get(actual)
    return None


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    if status in {"filled", "matched", "executed", "success", "done", "closed"}:
        return "filled"
    if status in {"open", "working", "pending", "submitted", "live", "partial", "partially_filled"}:
        return "open"
    if status in {"cancelled", "canceled", "rejected", "expired", "failed"}:
        return status
    return "unknown"


class PolymarketLiveExecutor:
    """Small live execution adapter for Polymarket CLOB.

    Default mode is conservative market-order execution so the bot can start
    with small, deterministic entries and immediately reconcile via the live
    account sync.
    """

    def __init__(
        self,
        store: Store,
        config: PolymarketAccountConfig,
        client_factory=None,
    ):
        self.store = store
        self.config = config
        self._client_factory = client_factory
        self._client = None
        self.order_style = os.getenv("BOT_LIVE_ORDER_STYLE", "market").strip().lower()
        self.post_only = _truthy(os.getenv("BOT_LIVE_POST_ONLY", "0"))
        self.max_order_usd = float(os.getenv("BOT_LIVE_MAX_ORDER_USD", "25"))
        self.min_order_usd = float(os.getenv("BOT_LIVE_MIN_ORDER_USD", "1"))
        self.tick_buffer_bps = float(os.getenv("BOT_LIVE_LIMIT_BUFFER_BPS", "0"))

    @classmethod
    def from_env(cls, store: Store, config: PolymarketAccountConfig) -> "PolymarketLiveExecutor":
        return cls(store=store, config=config)

    def _build_client(self):
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory(self.config)
            return self._client
        if not self.config.private_key:
            return None

        creds = None
        if self.config.api_key and self.config.api_secret and self.config.api_passphrase:
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.api_passphrase,
            )

        client = ClobClient(
            self.config.clob_host,
            key=self.config.private_key,
            chain_id=self.config.chain_id,
            creds=creds,
            signature_type=self.config.signature_type,
            funder=self.config.funder_address,
        )
        if creds is None:
            client.set_api_creds(client.create_or_derive_api_creds())
        self._client = client
        return self._client

    def _resolve_side(self, signal: Signal) -> str:
        if signal.action == "BUY_YES":
            return "BUY"
        if signal.action == "BUY_NO":
            return "BUY"
        raise ValueError(f"unsupported signal action: {signal.action}")

    def _resolve_token(self, market: Market, signal: Signal) -> str:
        if signal.action == "BUY_YES" and market.clob_yes_token:
            return market.clob_yes_token
        if signal.action == "BUY_NO" and market.clob_no_token:
            return market.clob_no_token
        raise ValueError(f"missing CLOB token for {signal.action} on market {market.id}")

    def _resolve_limit_price(self, client: ClobClient, token_id: str, amount_usd: float) -> float:
        try:
            book = client.get_order_book(token_id)
        except Exception:
            book = None

        tick = 0.01
        best_ask = None
        if book is not None:
            try:
                tick = _safe_float(getattr(book, "tick_size", None), tick) or tick
            except Exception:
                pass
            try:
                asks = getattr(book, "asks", None) or []
                if asks:
                    best_ask = _safe_float(getattr(asks[0], "price", None), None)
            except Exception:
                best_ask = None

        if best_ask is None or best_ask <= 0:
            try:
                estimated = client.calculate_market_price(token_id, "BUY", amount_usd, OrderType.FOK)
                best_ask = _safe_float(estimated, 0.0)
            except Exception:
                best_ask = 0.0

        price = max(tick, min(best_ask or 0.0, 1.0 - tick))
        if self.tick_buffer_bps > 0:
            price = max(tick, min(price * (1.0 + self.tick_buffer_bps / 10000.0), 1.0 - tick))
        return round(price, 4)

    def _extract_order_id(self, payload: Any) -> Optional[str]:
        if isinstance(payload, dict):
            for key in ("orderID", "orderId", "order_id", "id"):
                value = _pick(payload, key)
                if value:
                    return str(value)
        return None

    def _extract_status(self, payload: Any) -> str:
        if isinstance(payload, dict):
            status = _pick(payload, "status", "state", "orderStatus")
            if status is not None:
                return _normalize_status(status)
        return "unknown"

    def _extract_fill_price(self, payload: Any, fallback: float) -> float:
        if isinstance(payload, dict):
            for key in ("avgPrice", "averagePrice", "avg_fill_price", "fillPrice", "price"):
                value = _pick(payload, key)
                if value is not None:
                    parsed = _safe_float(value, fallback)
                    if parsed > 0:
                        return parsed
        return fallback

    def _extract_filled_quantity(self, payload: Any, fallback_amount: float, fallback_price: float) -> float:
        if isinstance(payload, dict):
            for key in ("sizeMatched", "filledSize", "executedSize", "filled_amount", "matched_size", "filledQuantity"):
                value = _pick(payload, key)
                if value is not None:
                    parsed = _safe_float(value, 0.0)
                    if parsed > 0:
                        return parsed
            amount = _pick(payload, "filledAmount", "matchedAmount", "amountFilled")
            if amount is not None:
                amount_f = _safe_float(amount, 0.0)
                if amount_f > 0 and fallback_price > 0:
                    return amount_f / fallback_price
        if fallback_price > 0:
            return fallback_amount / fallback_price
        return fallback_amount

    def sync_open_orders(self) -> list[Dict[str, Any]]:
        client = self._build_client()
        if client is None:
            return []
        try:
            orders = client.get_orders(OpenOrderParams())
        except Exception:
            return []
        if not isinstance(orders, list):
            return []
        normalized = []
        for order in orders:
            if not isinstance(order, dict):
                normalized.append({"raw": order})
                continue
            normalized.append({
                "id": _pick(order, "orderID", "orderId", "id"),
                "market_id": _pick(order, "market", "market_id"),
                "token_id": _pick(order, "asset_id", "assetId", "token_id"),
                "side": _pick(order, "side"),
                "price": _safe_float(_pick(order, "price"), 0.0),
                "size": _safe_float(_pick(order, "size"), 0.0),
                "status": _normalize_status(_pick(order, "status", "state")),
                "raw": order,
            })
        return normalized

    def cancel_all(self) -> Any:
        client = self._build_client()
        if client is None:
            return None
        return client.cancel_all()

    def mark_to_market(self, market_id: str, current_price: float):
        positions = self.store.get_positions()
        for p in positions:
            if p["market_id"] != market_id:
                continue
            if str(p.get("source", "paper")).lower() != "live":
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
                source=p.get("source", "live"),
                budget=p.get("budget"),
                meta=p.get("meta"),
            )
            self.store.save_position(updated)
            return updated
        return None

    def open_position(self, signal: Signal, amount_usd: float, market: Market) -> Optional[Position]:
        client = self._build_client()
        if client is None:
            raise RuntimeError("live executor unavailable: missing CLOB client")

        amount_usd = min(max(amount_usd, 0.0), self.max_order_usd)
        if amount_usd < self.min_order_usd:
            return None

        side = self._resolve_side(signal)
        token_id = self._resolve_token(market, signal)

        if self.order_style == "limit":
            limit_price = self._resolve_limit_price(client, token_id, amount_usd)
            size = round(amount_usd / max(limit_price, 0.01), 4)
            order = client.create_order(
                OrderArgs(
                    token_id=token_id,
                    price=limit_price,
                    size=size,
                    side=side,
                )
            )
            response = client.post_order(order, orderType=OrderType.GTC, post_only=self.post_only)
            order_id = self._extract_order_id(response)
            status = self._extract_status(response)
            price = self._extract_fill_price(response, limit_price)
            qty = self._extract_filled_quantity(response, amount_usd, price)
            position_status = "open" if status in {"filled", "open", "unknown"} else status
            trade_status = status if status != "unknown" else "submitted"
        else:
            estimated_price = self._resolve_limit_price(client, token_id, amount_usd)
            order = client.create_market_order(
                MarketOrderArgs(
                    token_id=token_id,
                    amount=amount_usd,
                    side=side,
                    price=estimated_price,
                    order_type=OrderType.FOK,
                )
            )
            response = client.post_order(order, orderType=OrderType.FOK)
            order_id = self._extract_order_id(response)
            status = self._extract_status(response)
            price = self._extract_fill_price(response, estimated_price)
            qty = self._extract_filled_quantity(response, amount_usd, price)
            position_status = "open" if status in {"filled", "open", "unknown"} else status
            trade_status = status if status != "unknown" else "submitted"

        now = datetime.now(timezone.utc).isoformat()
        position = Position(
            market_id=signal.market_id,
            question=signal.question,
            side="YES" if signal.action == "BUY_YES" else "NO",
            quantity=round(qty, 4),
            avg_entry_price=round(price, 4),
            current_price=round(price, 4),
            market_prob=signal.market_prob,
            model_prob=signal.model_prob,
            opened_at=now,
            updated_at=now,
            status=position_status,
            order_id=order_id,
            source="live",
            budget=round(amount_usd, 4),
            meta={"response": response, "token_id": token_id, "order_style": self.order_style, "city": signal.city, "date": signal.date, "signal_confidence": signal.confidence, "signal_edge": signal.edge},
        )
        self.store.save_position(position)
        self.store.save_trade(
            Trade(
                market_id=signal.market_id,
                side=position.side,
                quantity=round(qty, 4),
                price=round(price, 4),
                reason=signal.rationale,
                created_at=now,
                mode="live",
                order_id=order_id,
                status=trade_status,
                source="live",
                meta={"response": response, "token_id": token_id, "amount_usd": round(amount_usd, 4)},
            )
        )
        return position
