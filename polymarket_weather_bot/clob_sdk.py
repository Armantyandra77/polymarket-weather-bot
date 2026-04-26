from __future__ import annotations

from typing import Any, Dict, Mapping

from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, AssetType, BalanceAllowanceParams, MarketOrderArgs, OpenOrderParams, OrderArgs, OrderType

SDK_NAME = "py_clob_client_v2"


def sdk_name() -> str:
    return SDK_NAME


def create_or_derive_api_creds(client: Any):
    creator = getattr(client, "create_or_derive_api_key", None) or getattr(client, "create_or_derive_api_creds", None)
    if creator is None:
        raise AttributeError("ClobClient does not expose create_or_derive_api_key/create_or_derive_api_creds")
    return creator()


def resolve_funder_address(
    funder_address: str | None = None,
    proxy_address: str | None = None,
    wallet_address: str | None = None,
) -> str | None:
    for candidate in (funder_address, proxy_address, wallet_address):
        if candidate:
            return candidate
    return None


def resolve_signature_type(
    signature_type: int | None = None,
    *,
    authentication_type: str | None = None,
    proxy_address: str | None = None,
    wallet_address: str | None = None,
) -> int:
    auth = (authentication_type or "").strip().lower()
    if auth:
        if any(token in auth for token in ("magic", "email", "google")):
            return 1
        if any(token in auth for token in ("browser", "embedded", "privy", "turnkey", "gnosis", "safe")):
            return 2
        if "eoa" in auth:
            return 0

    if signature_type in (1, 2, 3):
        return int(signature_type)

    if proxy_address:
        return 2

    if wallet_address and not wallet_address.startswith("0x"):
        return 1

    return int(signature_type or 0)


def normalize_balance_allowance(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {
            "balance": 0.0,
            "allowance": 0.0,
            "allowances": {},
            "raw": payload,
        }

    balance_raw = payload.get("balance")
    balance = 0.0
    try:
        if balance_raw is not None:
            balance = float(balance_raw)
    except Exception:
        balance = 0.0

    allowances = payload.get("allowances")
    allowance = payload.get("allowance")
    if allowance is None and isinstance(allowances, Mapping):
        total = 0.0
        for value in allowances.values():
            try:
                total += float(value)
            except Exception:
                continue
        allowance = total
    try:
        allowance_value = float(allowance or 0.0)
    except Exception:
        allowance_value = 0.0

    return {
        "balance": balance,
        "allowance": allowance_value,
        "allowances": dict(allowances) if isinstance(allowances, Mapping) else {},
        "raw": payload,
    }


def fetch_open_orders(client: Any, params: Any = None):
    method = getattr(client, "get_open_orders", None)
    if method is None:
        raise AttributeError("ClobClient does not expose get_open_orders")
    if params is None:
        try:
            return method()
        except TypeError:
            return method(None)
    try:
        return method(params)
    except TypeError:
        return method()
