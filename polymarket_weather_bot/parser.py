from __future__ import annotations

import json
import math
import re
from dataclasses import asdict
from datetime import datetime, timezone, date
from typing import Any, Dict, Optional, Tuple

from .models import Market

CITY_STOPWORDS = {
    "will", "be", "the", "in", "at", "on", "by", "of", "for", "a", "an", "to", "this",
    "weather", "temperature", "temp", "degrees", "degree", "c", "f", "celsius", "fahrenheit",
    "high", "low", "between", "above", "below", "more", "less", "than", "next", "day", "city",
    "will", "there", "major", "event", "events", "week", "this", "month", "today", "tomorrow",
}

TEMP_RANGE_RE = re.compile(r"(?P<low>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>[cfCF])?\s*(?:-|–|to|and)\s*(?P<high>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit2>[cfCF])?")
ABOVE_RE = re.compile(r"(?:above|over|greater than|more than|>=|or\s+higher)\s*(?P<threshold>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>[cfCF])?", re.I)
BELOW_RE = re.compile(r"(?:below|under|less than|<=|or\s+below)\s*(?P<threshold>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>[cfCF])?", re.I)
EXACT_RE = re.compile(r"(?P<threshold>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>[cfCF])?\s*(?:on\s+.+)?$", re.I)
DATE_RE = re.compile(r"(\b20\d{2}-\d{2}-\d{2}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{1,2}\b)", re.I)


def _clean_tokens(text: str):
    return [t for t in re.findall(r"[A-Za-z][A-Za-z\-']+", text) if t.lower() not in CITY_STOPWORDS]


def parse_market_question(question: str) -> Dict[str, Any]:
    q = question.strip()
    meta: Dict[str, Any] = {
        "question": q,
        "city": None,
        "date": None,
        "kind": None,
        "low": None,
        "high": None,
        "threshold": None,
        "unit": "C",
        "confidence": 0.0,
        "parse_notes": [],
    }

    date_match = DATE_RE.search(q)
    if date_match:
        meta["date"] = date_match.group(1)
        meta["parse_notes"].append("explicit date detected")

    m = TEMP_RANGE_RE.search(q)
    if m:
        low = float(m.group("low"))
        high = float(m.group("high"))
        unit = (m.group("unit") or m.group("unit2") or "C").upper()
        if unit == "F":
            low = (low - 32.0) * 5.0 / 9.0
            high = (high - 32.0) * 5.0 / 9.0
        meta.update({"kind": "range", "low": min(low, high), "high": max(low, high), "unit": "C", "confidence": 0.92})
        meta["parse_notes"].append("temperature range detected")
    else:
        m = re.search(r"(?P<threshold>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>[cfCF])?\s*(?:or\s+)?higher", q, re.I)
        if m:
            thr = float(m.group("threshold"))
            if (m.group("unit") or "C").upper() == "F":
                thr = (thr - 32.0) * 5.0 / 9.0
            meta.update({"kind": "above", "threshold": thr, "unit": "C", "confidence": 0.82})
            meta["parse_notes"].append("above threshold detected")
        else:
            m = re.search(r"(?P<threshold>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>[cfCF])?\s*(?:or\s+)?lower", q, re.I)
            if not m:
                m = re.search(r"(?P<threshold>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>[cfCF])?\s*(?:or\s+)?below", q, re.I)
            if m:
                thr = float(m.group("threshold"))
                if (m.group("unit") or "C").upper() == "F":
                    thr = (thr - 32.0) * 5.0 / 9.0
                meta.update({"kind": "below", "threshold": thr, "unit": "C", "confidence": 0.82})
                meta["parse_notes"].append("below threshold detected")
            else:
                m = re.search(r"(?P<threshold>-?\d+(?:\.\d+)?)\s*(?:°\s*)?(?P<unit>[cfCF])?", q, re.I)
                if m and re.search(r"\d+\s*°\s*[cfCF]", q):
                    thr = float(m.group("threshold"))
                    if (m.group("unit") or "C").upper() == "F":
                        thr = (thr - 32.0) * 5.0 / 9.0
                    meta.update({"kind": "range", "low": thr - 0.5, "high": thr + 0.5, "unit": "C", "confidence": 0.78})
                    meta["parse_notes"].append("exact temperature coerced to narrow range")

    city = None
    m_city = re.search(r"\bin\s+(.+?)\s+(?:be|on|by)\b", q, re.I)
    if m_city:
        city = m_city.group(1).strip(" ,?.!")
        city = re.sub(r"^(?:the\s+)?(?:highest|lowest|average)\s+temperature\s+", "", city, flags=re.I)
        city = re.sub(r"^(?:temperature|temp)\s+", "", city, flags=re.I)
        city = re.sub(r"\b(?:city|area|region)\b$", "", city, flags=re.I).strip()
    if not city:
        tokens = _clean_tokens(q)
        if tokens:
            candidate = " ".join(tokens)
            candidate = re.sub(r"\b(?:Will|There|Major|Weather|Event|Events|Temperature|Temp|High|Low|Next|This|Week|Month|Day|Today|Tomorrow)\b.*$", "", candidate, flags=re.I).strip()
            if candidate:
                city = candidate
    if city:
        meta["city"] = city
        meta["parse_notes"].append(f"city candidate: {city}")
        if meta["confidence"] < 0.3:
            meta["confidence"] = 0.3

    if meta["kind"] is None:
        meta["parse_notes"].append("no temp bucket detected")

    return meta


def to_percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def normal_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    z = (x - mean) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def range_probability(low: float, high: float, mean: float, sigma: float) -> float:
    return max(0.0, min(1.0, normal_cdf(high, mean, sigma) - normal_cdf(low, mean, sigma)))


def one_tailed_probability(kind: str, threshold: float, mean: float, sigma: float) -> float:
    if kind == "above":
        return 1.0 - normal_cdf(threshold, mean, sigma)
    if kind == "below":
        return normal_cdf(threshold, mean, sigma)
    raise ValueError(kind)


def parse_end_date(end_date: Optional[str]) -> Optional[date]:
    if not end_date:
        return None
    try:
        return datetime.fromisoformat(end_date.replace('Z', '+00:00')).date()
    except Exception:
        return None
