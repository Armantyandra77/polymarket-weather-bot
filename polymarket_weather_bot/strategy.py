from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Market, Signal, WeatherForecast
from .parser import parse_end_date, parse_market_question, range_probability, one_tailed_probability, to_percent
from .polymarket import forecast_city, geocode_city, mean_and_sigma_for_date


class WeatherStrategy:
    def __init__(
        self,
        min_volume: float = 5000.0,
        max_spread: float = 0.08,
        edge_threshold: float = 0.10,
        max_positions: int = 3,
        max_days_out: int = 14,
    ):
        self.min_volume = min_volume
        self.max_spread = max_spread
        self.edge_threshold = edge_threshold
        self.max_positions = max_positions
        self.max_days_out = max_days_out
        self.allowed_cities = self._parse_csv_env('BOT_ALLOWED_CITIES')
        self.blocked_cities = self._parse_csv_env('BOT_BLOCKED_CITIES')
        self.allowed_terms = self._parse_csv_env('BOT_ALLOWED_TERMS')
        self.blocked_terms = self._parse_csv_env('BOT_BLOCKED_TERMS')

    @staticmethod
    def _parse_csv_env(name: str) -> List[str]:
        raw = os.getenv(name, '')
        return [part.strip().lower() for part in raw.split(',') if part.strip()]

    @staticmethod
    def _matches_any(text: str, needles: List[str]) -> bool:
        lowered = text.lower()
        return any(needle in lowered for needle in needles)

    def analyze_market(self, market: Market) -> Dict[str, Any]:
        meta = parse_market_question(market.question)
        rationale = []
        if market.volume < self.min_volume:
            return {"skip": True, "reason": "volume_below_min", "meta": meta}
        if market.spread > self.max_spread:
            return {"skip": True, "reason": "spread_too_wide", "meta": meta}
        if meta["confidence"] < 0.3:
            return {"skip": True, "reason": "unparseable_question", "meta": meta}

        city = meta.get("city")
        end_date = parse_end_date(market.end_date)
        if not city:
            return {"skip": True, "reason": "missing_city", "meta": meta}
        if not end_date:
            return {"skip": True, "reason": "missing_end_date", "meta": meta}

        today = datetime.now(timezone.utc).date()
        if end_date < today:
            return {"skip": True, "reason": "market_expired", "meta": meta}
        days_out = (end_date - today).days
        if days_out > self.max_days_out:
            return {"skip": True, "reason": "too_far_out", "meta": {**meta, "days_out": days_out}}

        if self.blocked_cities and self._matches_any(city, self.blocked_cities):
            return {"skip": True, "reason": "blocked_city", "meta": {**meta, "city": city}}
        if self.allowed_cities and not self._matches_any(city, self.allowed_cities):
            return {"skip": True, "reason": "city_not_allowed", "meta": {**meta, "city": city}}
        if self.blocked_terms and self._matches_any(market.question, self.blocked_terms):
            return {"skip": True, "reason": "blocked_term", "meta": {**meta, "question": market.question}}
        if self.allowed_terms and not self._matches_any(market.question, self.allowed_terms):
            return {"skip": True, "reason": "term_not_allowed", "meta": {**meta, "question": market.question}}

        geocoded = geocode_city(city)
        if not geocoded:
            return {"skip": True, "reason": "geocode_failed", "meta": meta}

        forecast = forecast_city(float(geocoded["latitude"]), float(geocoded["longitude"]), geocoded=geocoded)
        stats = mean_and_sigma_for_date(forecast, end_date.isoformat())
        if not stats:
            return {"skip": True, "reason": "forecast_out_of_range", "meta": meta}

        kind = meta.get("kind")
        if kind == "range":
            model_prob = range_probability(meta["low"], meta["high"], stats["mean"], stats["sigma"])
            target = f"{meta['low']:.1f}–{meta['high']:.1f}°C"
        elif kind in ("above", "below"):
            model_prob = one_tailed_probability(kind, meta["threshold"], stats["mean"], stats["sigma"])
            direction = ">" if kind == "above" else "<"
            target = f"{direction}{meta['threshold']:.1f}°C"
        else:
            return {"skip": True, "reason": "unsupported_question_type", "meta": meta}

        forecast_blend = forecast.get("blend") or {}
        forecast_confidence = float(forecast_blend.get("confidence") or 0.0)
        market_prob = market.market_prob
        edge = model_prob - market_prob
        confidence = min(0.99, max(0.0, meta.get("confidence", 0.3) * (1.0 - min(market.spread, 0.5)) + 0.1 + forecast_confidence * 0.08))
        action = "HOLD"
        if edge >= self.edge_threshold:
            action = "BUY_YES"
        elif edge <= -self.edge_threshold:
            action = "BUY_NO"
        rationale.append(f"city={city}")
        rationale.append(f"target={target}")
        rationale.append(f"forecast_mean={stats['mean']:.1f}°C")
        rationale.append(f"sigma={stats['sigma']:.1f}")
        rationale.append(f"market_prob={market_prob:.2%}")
        rationale.append(f"model_prob={model_prob:.2%}")
        rationale.append(f"edge={edge:+.2%}")

        signal = Signal(
            market_id=market.id,
            question=market.question,
            city=city,
            date=end_date.isoformat(),
            market_prob=market_prob,
            model_prob=model_prob,
            edge=edge,
            action=action,
            confidence=confidence,
            rationale="; ".join(rationale),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        return {
            "skip": False,
            "signal": signal,
            "meta": meta,
            "forecast": {**forecast, "stats": stats, "city": city, "lat": geocoded["latitude"], "lon": geocoded["longitude"], "date": end_date.isoformat()},
        }

    def should_enter(self, signal: Signal, open_positions: int) -> bool:
        return signal.action in ("BUY_YES", "BUY_NO") and signal.edge >= self.edge_threshold and open_positions < self.max_positions

    def recommended_size(self, signal: Signal, bankroll: float = 100.0) -> float:
        if signal.edge <= 0:
            return 0.0
        # Conservative Kelly-like fraction with cap.
        fraction = min(0.05, max(0.01, signal.edge / 4.0))
        return round(bankroll * fraction, 2)
