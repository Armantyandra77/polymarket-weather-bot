from __future__ import annotations

import binascii
import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = os.getenv("BOT_POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
POLYGON_RPC_URL = os.getenv("BOT_POLYMARKET_RPC_URL", "https://polygon-bor.publicnode.com")
SOLANA_RPC_URL = os.getenv("BOT_POLYMARKET_SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_SOLANA = os.getenv("BOT_POLYMARKET_SOLANA_USDC_MINT", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
DEFAULT_CHAIN_ID = int(os.getenv("BOT_POLYMARKET_CHAIN_ID", "137"))
DEFAULT_SIGNATURE_TYPE = int(os.getenv("BOT_POLYMARKET_SIGNATURE_TYPE", "0"))
BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _read_text_env_or_file(value_env: str, file_env: str) -> str | None:
    value = os.getenv(value_env)
    if value:
        return value.strip()
    path = os.getenv(file_env)
    if not path:
        return None
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
        return text.strip() or None
    except Exception:
        return None


def _parse_session_hint(raw: str | None) -> Dict[str, str | None]:
    if not raw:
        return {"proxy_address": None, "authentication_type": None}

    text = raw.strip()
    if not text:
        return {"proxy_address": None, "authentication_type": None}

    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except Exception:
            payload = {}
        proxy_address = payload.get("proxyAddress") or payload.get("proxy_address") or payload.get("address")
        authentication_type = payload.get("authenticationType") or payload.get("authentication_type") or payload.get("type")
        return {
            "proxy_address": proxy_address or None,
            "authentication_type": authentication_type or None,
        }

    if ":" in text:
        maybe_address, maybe_type = text.split(":", 1)
        if maybe_type:
            return {
                "proxy_address": maybe_address.strip() or None,
                "authentication_type": maybe_type.strip() or None,
            }

    return {"proxy_address": text, "authentication_type": None}


def _get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 25) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _hex_address(addr: str) -> str:
    raw = addr.lower().replace('0x', '')
    return raw.rjust(64, '0')


def _is_evm_address(addr: str | None) -> bool:
    return bool(addr and addr.startswith('0x') and len(addr) == 42)


def _is_solana_address(addr: str | None) -> bool:
    return bool(addr and not addr.startswith('0x') and BASE58_RE.fullmatch(addr))


def _rpc_post(url: str, method: str, params: list[Any], timeout: int = 25) -> Any:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode('utf-8')
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _rpc_post_with_fallback(method: str, params: list[Any], timeout: int = 25) -> Any:
    endpoints = [
        os.getenv("BOT_POLYMARKET_RPC_URL", "https://polygon-bor.publicnode.com"),
        "https://polygon-bor.publicnode.com",
        "https://rpc.ankr.com/polygon",
    ]
    last_exc: Exception | None = None
    for endpoint in endpoints:
        try:
            return _rpc_post(endpoint, method, params, timeout=timeout)
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise RuntimeError("RPC request failed")


def _rpc_post_solana_with_fallback(method: str, params: list[Any], timeout: int = 25) -> Any:
    endpoints = [
        os.getenv("BOT_POLYMARKET_SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
        "https://api.mainnet-beta.solana.com",
        "https://solana-api.projectserum.com",
    ]
    last_exc: Exception | None = None
    for endpoint in endpoints:
        try:
            return _rpc_post(endpoint, method, params, timeout=timeout)
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise RuntimeError("Solana RPC request failed")


def _get_onchain_usdc_balance(wallet_address: str) -> float:
    if _is_solana_address(wallet_address):
        response = _rpc_post_solana_with_fallback(
            "getTokenAccountsByOwner",
            [
                wallet_address,
                {"mint": USDC_SOLANA},
                {"encoding": "jsonParsed"},
            ],
        )
        result = response.get("result") if isinstance(response, dict) else None
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, list):
            return 0.0
        total = 0.0
        for item in value:
            try:
                info = item.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                token_amount = info.get("tokenAmount", {})
                total += float(token_amount.get("uiAmount") or 0.0)
            except Exception:
                continue
        return total

    data = '0x70a08231000000000000000000000000' + _hex_address(wallet_address)
    result = _rpc_post(POLYGON_RPC_URL, 'eth_call', [{"to": USDC_POLYGON, "data": data}, 'latest'])
    value = result.get('result') if isinstance(result, dict) else None
    if not value:
        return 0.0
    return int(value, 16) / 1_000_000


@dataclass(frozen=True)
class PolymarketAccountConfig:
    wallet_address: str | None = None
    proxy_address: str | None = None
    deposit_address: str | None = None
    authentication_type: str | None = None
    private_key: str | None = None
    funder_address: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    chain_id: int = DEFAULT_CHAIN_ID
    signature_type: int = DEFAULT_SIGNATURE_TYPE
    clob_host: str = CLOB_API

    @classmethod
    def from_env(cls) -> "PolymarketAccountConfig":
        wallet_address = os.getenv("BOT_POLYMARKET_WALLET_ADDRESS") or os.getenv("BOT_POLYMARKET_PUBLIC_ADDRESS")
        deposit_address = os.getenv("BOT_POLYMARKET_DEPOSIT_ADDRESS") or os.getenv("BOT_POLYMARKET_SOLANA_ADDRESS")
        funder_address = os.getenv("BOT_POLYMARKET_FUNDER_ADDRESS") or None
        session_hint = _parse_session_hint(_read_text_env_or_file("BOT_POLYMARKET_SESSION_HINT", "BOT_POLYMARKET_SESSION_HINT_PATH"))
        proxy_address = os.getenv("BOT_POLYMARKET_PROXY_ADDRESS") or session_hint.get("proxy_address") or None
        authentication_type = os.getenv("BOT_POLYMARKET_AUTHENTICATION_TYPE") or session_hint.get("authentication_type") or None
        return cls(
            wallet_address=wallet_address or proxy_address or funder_address,
            proxy_address=proxy_address or wallet_address or funder_address,
            deposit_address=deposit_address or proxy_address or wallet_address or funder_address,
            authentication_type=authentication_type,
            private_key=os.getenv("BOT_POLYMARKET_PRIVATE_KEY") or None,
            funder_address=funder_address,
            api_key=os.getenv("BOT_POLYMARKET_API_KEY") or None,
            api_secret=os.getenv("BOT_POLYMARKET_API_SECRET") or None,
            api_passphrase=os.getenv("BOT_POLYMARKET_API_PASSPHRASE") or None,
            chain_id=int(os.getenv("BOT_POLYMARKET_CHAIN_ID", str(DEFAULT_CHAIN_ID))),
            signature_type=int(os.getenv("BOT_POLYMARKET_SIGNATURE_TYPE", str(DEFAULT_SIGNATURE_TYPE))),
            clob_host=os.getenv("BOT_POLYMARKET_CLOB_HOST", CLOB_API),
        )


class PolymarketAccountSync:
    def __init__(
        self,
        config: PolymarketAccountConfig,
        http_get=_get_json,
        client_factory=None,
    ):
        self.config = config
        self.http_get = http_get
        self._client_factory = client_factory
        self._client = None

    @classmethod
    def from_env(cls) -> "PolymarketAccountSync":
        return cls(PolymarketAccountConfig.from_env())

    def enabled(self) -> bool:
        return bool(self.config.wallet_address or self.config.proxy_address or self.config.private_key or self.config.api_key)

    def _normalize_position(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "title": payload.get("title") or payload.get("question") or payload.get("slug") or "Unknown market",
            "slug": payload.get("slug"),
            "condition_id": payload.get("conditionId") or payload.get("condition_id"),
            "outcome": payload.get("outcome") or payload.get("side") or payload.get("asset"),
            "size": float(payload.get("size") or payload.get("netPosition") or payload.get("quantity") or 0),
            "avg_price": float(payload.get("avgPrice") or payload.get("avg_price") or payload.get("averagePrice") or 0),
            "current_value": float(payload.get("currentValue") or payload.get("current_value") or 0),
            "initial_value": float(payload.get("initialValue") or payload.get("initial_value") or 0),
            "cash_pnl": float(payload.get("cashPnl") or payload.get("cash_pnl") or 0),
            "percent_pnl": float(payload.get("percentPnl") or payload.get("percent_pnl") or 0),
            "cur_price": float(payload.get("curPrice") or payload.get("cur_price") or 0),
            "redeemable": bool(payload.get("redeemable", False)),
            "mergeable": bool(payload.get("mergeable", False)),
            "end_date": payload.get("endDate") or payload.get("end_date"),
            "proxy_wallet": payload.get("proxyWallet") or payload.get("proxy_wallet"),
            "updated_at": payload.get("updateTime") or payload.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            "raw": payload,
        }

    def _normalize_order(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        status = str(payload.get("status") or payload.get("state") or payload.get("orderStatus") or "unknown").lower()
        created_at = payload.get("createdAt") or payload.get("created_at") or payload.get("placedAt") or payload.get("timestamp")
        updated_at = payload.get("updatedAt") or payload.get("updated_at") or payload.get("updateTime") or created_at or datetime.now(timezone.utc).isoformat()
        return {
            "id": payload.get("orderID") or payload.get("orderId") or payload.get("id"),
            "market_id": payload.get("market") or payload.get("market_id") or payload.get("slug") or payload.get("conditionId"),
            "token_id": payload.get("asset_id") or payload.get("assetId") or payload.get("token_id") or payload.get("tokenId"),
            "side": payload.get("side") or payload.get("outcome") or payload.get("direction") or payload.get("orderSide"),
            "price": float(payload.get("price") or payload.get("limitPrice") or payload.get("avgPrice") or 0),
            "size": float(payload.get("size") or payload.get("originalSize") or payload.get("quantity") or 0),
            "filled_size": float(payload.get("filledSize") or payload.get("filled_size") or payload.get("sizeMatched") or 0),
            "status": status,
            "created_at": created_at,
            "updated_at": updated_at,
            "raw": payload,
        }

    def _build_client(self):
        if self._client is not None:
            return self._client

        if self._client_factory is not None:
            try:
                self._client = self._client_factory(self.config)
                return self._client
            except Exception as exc:
                return {"error": str(exc)}

        if not self.config.private_key:
            return None

        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
        except Exception as exc:
            return {"error": f"py_clob_client unavailable: {exc}"}

        creds = None
        if self.config.api_key and self.config.api_secret and self.config.api_passphrase:
            creds = ApiCreds(
                api_key=self.config.api_key,
                api_secret=self.config.api_secret,
                api_passphrase=self.config.api_passphrase,
            )

        try:
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
        except Exception as exc:
            return {"error": str(exc)}

    def sync(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "enabled": self.enabled(),
            "status": "disabled",
            "wallet_address": self.config.wallet_address,
            "proxy_address": self.config.proxy_address,
            "authentication_type": self.config.authentication_type,
            "deposit_address": self.config.deposit_address,
            "profile": {},
            "positions": [],
            "positions_count": 0,
            "portfolio_value": 0.0,
            "wallet_balance": 0.0,
            "equity": 0.0,
            "balance": {},
            "open_orders_count": 0,
            "open_orders": [],
            "order_history_count": 0,
            "order_history": [],
            "order_source": "clob-open-orders",
            "trading_ready": False,
            "auth_layers": {
                "l1_private_key": bool(self.config.private_key),
                "l2_api_creds": bool(self.config.api_key and self.config.api_secret and self.config.api_passphrase),
            },
            "warnings": [],
            "errors": [],
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }

        account_address = self.config.proxy_address or self.config.wallet_address
        wallet_address = self.config.wallet_address or self.config.proxy_address
        balance_address = self.config.deposit_address or self.config.proxy_address or wallet_address
        if not result["enabled"]:
            result["warnings"].append("Set BOT_POLYMARKET_PROXY_ADDRESS, BOT_POLYMARKET_WALLET_ADDRESS, or BOT_POLYMARKET_FUNDER_ADDRESS to enable live account sync.")
            return result

        read_only_ok = False
        if account_address and _is_evm_address(account_address):
            try:
                profile = self.http_get(f"{GAMMA_API}/public-profile", {"address": account_address})
                result["profile"] = {
                    "name": profile.get("name"),
                    "pseudonym": profile.get("pseudonym"),
                    "x_username": profile.get("xUsername"),
                    "proxy_wallet": profile.get("proxyWallet"),
                    "verified_badge": bool(profile.get("verifiedBadge", False)),
                    "created_at": profile.get("createdAt"),
                    "bio": profile.get("bio"),
                    "raw": profile,
                }
            except Exception as exc:
                result["warnings"].append(f"profile lookup failed: {exc}")

            try:
                positions_raw = self.http_get(f"{DATA_API}/positions", {"user": account_address, "limit": 100})
                if isinstance(positions_raw, list):
                    result["positions"] = [self._normalize_position(p) for p in positions_raw]
                    result["positions_count"] = len(result["positions"])
                    read_only_ok = True
            except Exception as exc:
                result["warnings"].append(f"positions lookup failed: {exc}")

            try:
                values_raw = self.http_get(f"{DATA_API}/value", {"user": account_address})
                total_value = 0.0
                if isinstance(values_raw, list):
                    for item in values_raw:
                        if isinstance(item, dict) and item.get("value") is not None:
                            total_value += float(item.get("value") or 0.0)
                elif isinstance(values_raw, dict) and values_raw.get("value") is not None:
                    total_value = float(values_raw.get("value") or 0.0)
                elif result["positions"]:
                    total_value = sum(float(p.get("current_value") or 0.0) for p in result["positions"])
                result["portfolio_value"] = round(total_value, 4)
                read_only_ok = True
            except Exception as exc:
                result["warnings"].append(f"portfolio value lookup failed: {exc}")
                if result["positions"]:
                    result["portfolio_value"] = round(sum(float(p.get("current_value") or 0.0) for p in result["positions"]), 4)
                    read_only_ok = True

        try:
            wallet_balance = _get_onchain_usdc_balance(balance_address)
            result["wallet_balance"] = round(wallet_balance, 4)
            result["equity"] = round(wallet_balance + result["portfolio_value"], 4)
            read_only_ok = True
        except Exception as exc:
            result["warnings"].append(f"wallet balance lookup failed: {exc}")
            result["equity"] = round(result["portfolio_value"], 4)

        if balance_address and _is_solana_address(balance_address):
            result["warnings"].append("Solana deposit address detected; syncing on-chain USDC only for wallet balance.")

        client = self._build_client()
        if isinstance(client, dict) and client.get("error"):
            result["errors"].append(client["error"])
            result["status"] = "partial"
            return result

        if client is not None:
            try:
                from py_clob_client.clob_types import AssetType, BalanceAllowanceParams, OpenOrderParams

                collateral = client.get_balance_allowance(
                    params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                )
                result["balance"] = {
                    "balance": float(collateral.get("balance") or 0.0),
                    "allowance": float(collateral.get("allowance") or 0.0),
                    "wallet_balance": float(result.get("wallet_balance") or 0.0),
                    "portfolio_value": float(result.get("portfolio_value") or 0.0),
                    "equity": float(result.get("equity") or 0.0),
                    "raw": collateral,
                }
                try:
                    orders = client.get_orders(OpenOrderParams())
                    if isinstance(orders, list):
                        normalized_orders = [self._normalize_order(order) if isinstance(order, dict) else {"raw": order} for order in orders]
                        result["open_orders"] = normalized_orders
                        result["order_history"] = normalized_orders
                        result["open_orders_count"] = len(normalized_orders)
                        result["order_history_count"] = len(normalized_orders)
                except Exception as exc:
                    result["warnings"].append(f"open orders lookup failed: {exc}")
                result["status"] = "connected"
                result["trading_ready"] = True
                return result
            except Exception as exc:
                result["errors"].append(str(exc))
                result["status"] = "partial" if read_only_ok else "error"
                return result

        result["status"] = "read_only" if read_only_ok else "error"
        if not read_only_ok:
            result["warnings"].append("No live data could be fetched from Polymarket.")
        return result
