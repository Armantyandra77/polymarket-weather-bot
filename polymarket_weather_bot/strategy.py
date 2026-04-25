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
        # Risk management defaults are intentionally conservative for weather markets,
        # where many questions are correlated by city/date and forecast error can spike.
        self.min_confidence = float(os.getenv('BOT_RISK_MIN_CONFIDENCE', '0.92'))
        self.max_position_fraction = float(os.getenv('BOT_RISK_MAX_POSITION_FRACTION', '0.03'))
        self.max_total_exposure_fraction = float(os.getenv('BOT_RISK_MAX_TOTAL_EXPOSURE_FRACTION', '0.10'))
        self.max_city_exposure_fraction = float(os.getenv('BOT_RISK_MAX_CITY_EXPOSURE_FRACTION', '0.04'))
        self.min_trade_usd = float(os.getenv('BOT_RISK_MIN_TRADE_USD', '1'))
        self.kelly_fraction = float(os.getenv('BOT_RISK_KELLY_FRACTION', '0.15'))
        self.max_city_positions = int(os.getenv('BOT_RISK_MAX_CITY_POSITIONS', '1'))

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

    @staticmethod
    def _position_city(position: Dict[str, Any]) -> str:
        meta = position.get('meta') or {}
        if isinstance(meta, dict):
            city = meta.get('city')
            if city:
                return str(city)
        question = str(position.get('question') or '')
        parsed = parse_market_question(question)
        return str(parsed.get('city') or '')

    @staticmethod
    def _position_value(position: Dict[str, Any]) -> float:
        budget = position.get('budget')
        if budget is not None:
            try:
                return max(0.0, float(budget))
            except Exception:
                pass
        quantity = float(position.get('quantity') or 0.0)
        avg_entry_price = float(position.get('avg_entry_price') or 0.0)
        return max(0.0, quantity * avg_entry_price)

    def _active_positions(self, positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            p for p in positions
            if str(p.get('status', 'open')).lower() == 'open'
        ]

    def _total_exposure(self, positions: List[Dict[str, Any]]) -> float:
        return sum(self._position_value(p) for p in self._active_positions(positions))

    def _city_exposure(self, city: str, positions: List[Dict[str, Any]]) -> float:
        city = str(city or '').strip().lower()
        if not city:
            return 0.0
        return sum(
            self._position_value(p)
            for p in self._active_positions(positions)
            if self._position_city(p).strip().lower() == city
        )

    def _city_position_count(self, city: str, positions: List[Dict[str, Any]]) -> int:
        city = str(city or '').strip().lower()
        if not city:
            return 0
        return sum(1 for p in self._active_positions(positions) if self._position_city(p).strip().lower() == city)

    def should_enter(self, signal: Signal, open_positions: int) -> bool:
        if signal.action not in ("BUY_YES", "BUY_NO"):
            return False
        if abs(signal.edge) < self.edge_threshold:
            return False
        if signal.confidence < self.min_confidence:
            return False
        return open_positions < self.max_positions

    def passes_risk_limits(self, signal: Signal, positions: List[Dict[str, Any]], bankroll: float = 100.0) -> bool:
        active_positions = self._active_positions(positions)
        if not active_positions:
            return True
        total_limit = bankroll * self.max_total_exposure_fraction
        city_limit = bankroll * self.max_city_exposure_fraction
        total_exposure = self._total_exposure(active_positions)
        city_exposure = self._city_exposure(signal.city, active_positions)
        if total_exposure >= total_limit:
            return False
        if city_exposure >= city_limit:
            return False
        if self._city_position_count(signal.city, active_positions) >= self.max_city_positions:
            return False
        return True

    def recommended_size(self, signal: Signal, bankroll: float = 100.0, positions: Optional[List[Dict[str, Any]]] = None) -> float:
        if signal.action not in ("BUY_YES", "BUY_NO"):
            return 0.0
        edge = abs(float(signal.edge))
        if edge < self.edge_threshold:
            return 0.0
        confidence = max(0.0, min(1.0, float(signal.confidence)))
        confidence_scale = 0.0
        if confidence >= self.min_confidence:
            denom = max(1e-9, 1.0 - self.min_confidence)
            confidence_scale = min(1.0, (confidence - self.min_confidence) / denom)
        raw_fraction = edge * self.kelly_fraction * max(0.35, confidence_scale)
        fraction = max(0.01, min(self.max_position_fraction, raw_fraction))
        amount = bankroll * fraction
        if positions:
            active_positions = self._active_positions(positions)
            total_remaining = max(0.0, bankroll * self.max_total_exposure_fraction - self._total_exposure(active_positions))
            city_remaining = max(0.0, bankroll * self.max_city_exposure_fraction - self._city_exposure(signal.city, active_positions))
            amount = min(amount, total_remaining, city_remaining)
        if amount < self.min_trade_usd:
            return 0.0
        return round(amount, 2)
