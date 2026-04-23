from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Market:
    id: str
    question: str
    slug: str
    condition_id: str
    yes_price: float
    no_price: float
    volume: float
    liquidity: float
    active: bool
    closed: bool
    end_date: Optional[str] = None
    category: Optional[str] = None
    clob_yes_token: Optional[str] = None
    clob_no_token: Optional[str] = None

    @property
    def spread(self) -> float:
        return abs(self.yes_price - (1.0 - self.no_price))

    @property
    def market_prob(self) -> float:
        return self.yes_price


@dataclass(frozen=True)
class WeatherForecast:
    city: str
    latitude: float
    longitude: float
    date: str
    expected_temp_c: float
    low_temp_c: float
    high_temp_c: float
    sigma_c: float
    confidence: float
    source: str = "open-meteo"


@dataclass(frozen=True)
class Signal:
    market_id: str
    question: str
    city: str
    date: str
    market_prob: float
    model_prob: float
    edge: float
    action: str
    confidence: float
    rationale: str
    generated_at: str


@dataclass(frozen=True)
class Position:
    market_id: str
    question: str
    side: str
    quantity: float
    avg_entry_price: float
    current_price: float
    market_prob: float
    model_prob: float
    opened_at: str
    updated_at: str
    status: str = "open"
    order_id: Optional[str] = None
    source: str = "paper"
    budget: Optional[float] = None
    meta: Optional[Dict[str, Any]] = None

    @property
    def unrealized_pnl(self) -> float:
        if self.side.upper() == "YES":
            return (self.current_price - self.avg_entry_price) * self.quantity
        return ((1.0 - self.current_price) - (1.0 - self.avg_entry_price)) * self.quantity

    @property
    def cost_basis(self) -> float:
        return self.avg_entry_price * self.quantity


@dataclass(frozen=True)
class Trade:
    market_id: str
    side: str
    quantity: float
    price: float
    reason: str
    created_at: str
    mode: str = "paper"
    order_id: Optional[str] = None
    status: str = "filled"
    source: str = "paper"
    meta: Optional[Dict[str, Any]] = None
