from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .models import Market
from .parser import parse_end_date

GAMMA_BASE = 'https://gamma-api.polymarket.com'
OPEN_METEO_GEOCODE = 'https://geocoding-api.open-meteo.com/v1/search'
OPEN_METEO_FORECAST = 'https://api.open-meteo.com/v1/forecast'


def _get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _parse_json_field(value: Any, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def discover_weather_markets(min_volume: float = 5000.0, max_results: int = 50, store: Optional[Any] = None) -> List[Market]:
    seen = set()
    results: List[Market] = []

    def ingest(item: Dict[str, Any]):
        q = (item.get("question") or "").strip()
        text = q.lower()
        if not any(k in text for k in ["temperature", "weather", "forecast", "highest temperature", "lowest temperature", "temperature in"]):
            if not re.search(r"\d+\s*°\s*[cf]", text):
                return
        try:
            prices = _parse_json_field(item.get("outcomePrices"), ["0.5", "0.5"])
            token_ids = _parse_json_field(item.get("clobTokenIds"), [None, None])
            yes_price = float(prices[0])
            no_price = float(prices[1])
            volume = float(item.get("volume") or 0.0)
            liquidity = float(item.get("liquidity") or 0.0)
            record = {
                "source": "gamma",
                "market_id": str(item.get("id") or item.get("conditionId") or q),
                "market": {
                    "id": str(item.get("id") or item.get("conditionId") or q),
                    "question": q,
                    "slug": str(item.get("slug") or ""),
                    "condition_id": str(item.get("conditionId") or ""),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume": volume,
                    "liquidity": liquidity,
                    "active": bool(item.get("active", True)),
                    "closed": bool(item.get("closed", False)),
                    "end_date": item.get("endDate"),
                    "category": item.get("category"),
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            if volume < min_volume:
                record["status"] = "filtered_volume"
                if store is not None:
                    try:
                        store.save_market_scan(record)
                    except Exception:
                        pass
                return
            if bool(item.get("closed", False)):
                record["status"] = "filtered_closed"
                if store is not None:
                    try:
                        store.save_market_scan(record)
                    except Exception:
                        pass
                return
            end_date = item.get("endDate")
            if end_date:
                try:
                    if parse_end_date(end_date) and parse_end_date(end_date) < datetime.now().date():
                        record["status"] = "filtered_expired"
                        if store is not None:
                            try:
                                store.save_market_scan(record)
                            except Exception:
                                pass
                        return
                except Exception:
                    pass
            dedupe_key = str(item.get("id") or item.get("conditionId") or q)
            if dedupe_key in seen:
                return
            seen.add(dedupe_key)
            record["status"] = "accepted"
            if store is not None:
                try:
                    store.save_market_scan(record)
                except Exception:
                    pass
            results.append(
                Market(
                    id=dedupe_key,
                    question=q,
                    slug=str(item.get("slug") or ""),
                    condition_id=str(item.get("conditionId") or ""),
                    yes_price=yes_price,
                    no_price=no_price,
                    volume=volume,
                    liquidity=liquidity,
                    active=bool(item.get("active", True)),
                    closed=bool(item.get("closed", False)),
                    end_date=end_date,
                    category=item.get("category"),
                    clob_yes_token=token_ids[0] if len(token_ids) > 0 else None,
                    clob_no_token=token_ids[1] if len(token_ids) > 1 else None,
                )
            )
        except Exception:
            return

    # First, scan currently active markets.
    try:
        active_markets = _get_json(f"{GAMMA_BASE}/markets", {"limit": 200, "active": "true", "closed": "false", "order": "volume", "ascending": "false"})
        for item in active_markets:
            ingest(item)
    except Exception:
        pass

    # Then supplement with targeted search results.
    queries = ["temperature", "weather", "forecast"]
    for query in queries:
        try:
            data = _get_json(f"{GAMMA_BASE}/public-search", {"q": query})
        except Exception:
            continue
        for ev in data.get("events", []):
            for item in ev.get("markets", []):
                ingest(item)

    results.sort(key=lambda x: (x.volume, x.liquidity), reverse=True)
    return results[:max_results]


def geocode_city(city: str) -> Optional[Dict[str, Any]]:
    data = _get_json(OPEN_METEO_GEOCODE, {"name": city, "count": 1, "language": "en", "format": "json"})
    results = data.get("results") or []
    if not results:
        return None
    return results[0]


def forecast_city(lat: float, lon: float, days: int = 14) -> Dict[str, Any]:
    return _get_json(
        OPEN_METEO_FORECAST,
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": ["temperature_2m"],
            "daily": ["temperature_2m_max", "temperature_2m_min", "temperature_2m_mean"],
            "forecast_days": days,
            "timezone": "auto",
        },
        timeout=30,
    )


def mean_and_sigma_for_date(forecast: Dict[str, Any], target_date: str) -> Optional[Dict[str, float]]:
    daily = forecast.get("daily") or {}
    times = daily.get("time") or []
    if target_date not in times:
        return None
    idx = times.index(target_date)
    means = daily.get("temperature_2m_mean") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    mean = float(means[idx]) if idx < len(means) and means[idx] is not None else ((float(highs[idx]) + float(lows[idx])) / 2.0)
    high = float(highs[idx]) if idx < len(highs) and highs[idx] is not None else mean + 2.0
    low = float(lows[idx]) if idx < len(lows) and lows[idx] is not None else mean - 2.0
    sigma = max(1.0, (high - low) / 4.0)
    return {"mean": mean, "high": high, "low": low, "sigma": sigma}
