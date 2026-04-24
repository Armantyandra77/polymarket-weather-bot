from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

OPEN_METEO_FORECAST = 'https://api.open-meteo.com/v1/forecast'
NWS_POINTS = 'https://api.weather.gov/points/{lat},{lon}'


def _get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Any:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (PolymarketWeatherBot/1.0)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _to_celsius(value: Any, unit: Optional[str]) -> float:
    temp = float(value)
    if (unit or 'C').upper().startswith('F'):
        return (temp - 32.0) * 5.0 / 9.0
    return temp


def _combine_stats(records: List[Dict[str, Any]]) -> Dict[str, float]:
    active = [r for r in records if r.get('available', True)]
    if not active:
        return {'mean': 0.0, 'high': 0.0, 'low': 0.0, 'sigma': 1.0, 'confidence': 0.0, 'disagreement_c': 0.0, 'weight': 0.0}

    weights = [max(0.01, float(r.get('weight', 1.0))) for r in active]
    total = sum(weights)
    means = [float(r['mean']) for r in active]
    highs = [float(r['high']) for r in active]
    lows = [float(r['low']) for r in active]
    sigmas = [max(0.5, float(r.get('sigma', 1.5))) for r in active]
    confidences = [max(0.0, min(0.99, float(r.get('confidence', 0.0)))) for r in active]

    blend_mean = sum(m * w for m, w in zip(means, weights)) / total
    blend_high = sum(h * w for h, w in zip(highs, weights)) / total
    blend_low = sum(l * w for l, w in zip(lows, weights)) / total
    blend_var = sum(w * (sigma ** 2 + (mean - blend_mean) ** 2) for mean, sigma, w in zip(means, sigmas, weights)) / total
    blend_sigma = math.sqrt(max(1.0, blend_var))
    avg_conf = sum(conf * w for conf, w in zip(confidences, weights)) / total
    disagreement_c = max(means) - min(means) if len(means) > 1 else 0.0
    blend_conf = max(0.10, min(0.98, avg_conf * (1.0 - min(disagreement_c / 12.0, 0.30))))
    return {
        'mean': round(blend_mean, 2),
        'high': round(blend_high, 2),
        'low': round(blend_low, 2),
        'sigma': round(blend_sigma, 2),
        'confidence': round(blend_conf, 3),
        'disagreement_c': round(disagreement_c, 2),
        'weight': round(total, 3),
    }


def _daily_records_from_open_meteo(forecast: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    daily = forecast.get('daily') or {}
    times = daily.get('time') or []
    means = daily.get('temperature_2m_mean') or []
    highs = daily.get('temperature_2m_max') or []
    lows = daily.get('temperature_2m_min') or []
    records: Dict[str, Dict[str, float]] = {}
    for idx, day in enumerate(times):
        if not day:
            continue
        mean = float(means[idx]) if idx < len(means) and means[idx] is not None else None
        high = float(highs[idx]) if idx < len(highs) and highs[idx] is not None else None
        low = float(lows[idx]) if idx < len(lows) and lows[idx] is not None else None
        if mean is None:
            if high is not None and low is not None:
                mean = (high + low) / 2.0
            else:
                continue
        if high is None:
            high = mean + 2.0
        if low is None:
            low = mean - 2.0
        sigma = max(0.8, (high - low) / 4.0)
        records[day] = {
            'mean': round(mean, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'sigma': round(sigma, 2),
            'confidence': 0.72,
        }
    return records


def _daily_records_from_nws(forecast: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    props = forecast.get('properties') or {}
    periods = props.get('periods') or []
    grouped: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: {'temps': [], 'day_temps': []})
    for period in periods:
        start_time = period.get('startTime')
        temp = period.get('temperature')
        if not start_time or temp is None:
            continue
        try:
            day = datetime.fromisoformat(str(start_time).replace('Z', '+00:00')).date().isoformat()
        except Exception:
            continue
        try:
            value = _to_celsius(temp, period.get('temperatureUnit'))
        except Exception:
            continue
        grouped[day]['temps'].append(value)
        if period.get('isDaytime'):
            grouped[day]['day_temps'].append(value)

    records: Dict[str, Dict[str, float]] = {}
    for day, payload in grouped.items():
        temps = payload['day_temps'] or payload['temps']
        if not temps:
            continue
        high = max(temps)
        low = min(temps)
        mean = sum(temps) / len(temps)
        sigma = max(0.8, (high - low) / 4.0)
        records[day] = {
            'mean': round(mean, 2),
            'high': round(high, 2),
            'low': round(low, 2),
            'sigma': round(sigma, 2),
            'confidence': 0.80 if len(temps) >= 2 else 0.68,
        }
    return records


def _nws_forecast(lat: float, lon: float, days: int = 14) -> Optional[Dict[str, Any]]:
    try:
        point = _get_json(NWS_POINTS.format(lat=lat, lon=lon), timeout=25)
        forecast_url = ((point.get('properties') or {}).get('forecast')) or ''
        if not forecast_url:
            return None
        forecast = _get_json(forecast_url, timeout=25)
        forecast['source'] = 'nws'
        forecast['daily_map'] = _daily_records_from_nws(forecast)
        return forecast
    except Exception:
        return None


def _open_meteo_forecast(lat: float, lon: float, days: int = 14) -> Dict[str, Any]:
    forecast = _get_json(
        OPEN_METEO_FORECAST,
        {
            'latitude': lat,
            'longitude': lon,
            'hourly': ['temperature_2m'],
            'daily': ['temperature_2m_max', 'temperature_2m_min', 'temperature_2m_mean'],
            'forecast_days': days,
            'timezone': 'auto',
        },
        timeout=30,
    )
    forecast['source'] = 'open-meteo'
    forecast['daily_map'] = _daily_records_from_open_meteo(forecast)
    return forecast


def _source_summary(source: Dict[str, Any]) -> Dict[str, Any]:
    daily_map = source.get('daily_map') or {}
    daily_dates = sorted(daily_map.keys())
    confidence = 0.0
    if daily_dates:
        confidence = float(daily_map[daily_dates[0]].get('confidence', 0.0))
    return {
        'source': source.get('source', 'unknown'),
        'available_dates': len(daily_dates),
        'confidence': confidence,
        'weight': source.get('weight', 1.0),
    }


def _build_ensemble_daily(source_records: List[Dict[str, Any]]) -> Dict[str, List[float]]:
    dates = sorted({day for source in source_records for day in (source.get('daily_map') or {}).keys()})
    daily = {'time': [], 'temperature_2m_mean': [], 'temperature_2m_max': [], 'temperature_2m_min': []}
    for day in dates:
        day_records = []
        for source in source_records:
            daily_map = source.get('daily_map') or {}
            if day not in daily_map:
                continue
            rec = dict(daily_map[day])
            rec['weight'] = float(source.get('weight', 1.0))
            rec['source'] = source.get('source', 'unknown')
            day_records.append(rec)
        blended = _combine_stats(day_records)
        daily['time'].append(day)
        daily['temperature_2m_mean'].append(blended['mean'])
        daily['temperature_2m_max'].append(blended['high'])
        daily['temperature_2m_min'].append(blended['low'])
    return daily


def build_forecast_ensemble(lat: float, lon: float, days: int = 14, geocoded: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    city = (geocoded or {}).get('name') or (geocoded or {}).get('city') or 'Unknown city'
    open_meteo = _open_meteo_forecast(lat, lon, days=days)
    source_records: List[Dict[str, Any]] = []
    source_records.append({
        'source': 'open-meteo',
        'weight': 0.70,
        'daily_map': open_meteo.get('daily_map') or {},
    })

    country_code = str((geocoded or {}).get('country_code') or '').upper()
    country = str((geocoded or {}).get('country') or '').lower()
    if country_code == 'US' or country == 'united states':
        nws = _nws_forecast(lat, lon, days=days)
        if nws and nws.get('daily_map'):
            source_records.append({
                'source': 'nws',
                'weight': 0.95,
                'daily_map': nws.get('daily_map') or {},
            })

    blend_daily = _build_ensemble_daily(source_records)
    summaries = [_source_summary(src) for src in source_records]
    if len(summaries) == 1:
        primary = summaries[0]
        blend_conf = primary['confidence']
        disagreement_c = 0.0
    else:
        blend_conf = round(sum(item['confidence'] * item['weight'] for item in summaries) / max(0.01, sum(item['weight'] for item in summaries)), 3)
        disagreement_c = round(max(item['confidence'] for item in summaries) - min(item['confidence'] for item in summaries), 3)

    return {
        'city': city,
        'latitude': lat,
        'longitude': lon,
        'source': 'blend' if len(summaries) > 1 else summaries[0]['source'],
        'sources': summaries,
        'blend': {
            'source_count': len(summaries),
            'confidence': blend_conf,
            'disagreement_c': disagreement_c,
        },
        'daily': blend_daily,
    }


def mean_and_sigma_for_date(forecast: Dict[str, Any], target_date: str) -> Optional[Dict[str, float]]:
    daily = forecast.get('daily') or {}
    times = daily.get('time') or []
    if target_date not in times:
        return None
    idx = times.index(target_date)
    means = daily.get('temperature_2m_mean') or []
    highs = daily.get('temperature_2m_max') or []
    lows = daily.get('temperature_2m_min') or []
    mean = float(means[idx]) if idx < len(means) and means[idx] is not None else ((float(highs[idx]) + float(lows[idx])) / 2.0)
    high = float(highs[idx]) if idx < len(highs) and highs[idx] is not None else mean + 2.0
    low = float(lows[idx]) if idx < len(lows) and lows[idx] is not None else mean - 2.0
    sigma = max(1.0, (high - low) / 4.0)
    return {'mean': mean, 'high': high, 'low': low, 'sigma': sigma}
