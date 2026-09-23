"""Cálculo isolado da recomendação de gatilho por volatilidade histórica.

Este módulo é deliberadamente somente leitura: ele não conhece configuração,
estado do monitor nem execução de ordens.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Callable


class TriggerRecommendationError(Exception):
    pass


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise TriggerRecommendationError("Histórico insuficiente para recomendar o gatilho")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _round_step(value: float, step: float = 0.05) -> float:
    return round(math.ceil((value - 1e-12) / step) * step, 2)


def recommend_hold_seconds(
    normalized: list[tuple[int, float, float]], trigger_percent: float
) -> tuple[int, int]:
    """Estima a duração útil contra recuperações rápidas, em degraus de 1 min."""
    candidates = (60, 120, 180, 300, 600)
    trigger = trigger_percent / 100
    peak = normalized[0][1]
    opened_at: int | None = None
    entry = 0.0
    durations: list[float] = []
    for timestamp, high, low in normalized[1:]:
        if opened_at is None:
            peak = max(peak, high)
            if low <= peak * (1 - trigger):
                opened_at = timestamp
                entry = peak * (1 - trigger)
        elif high >= entry * 0.999:
            durations.append(max(60.0, (timestamp - opened_at) / 1000))
            opened_at = None
            peak = high
    if not durations:
        return 300, 0
    desired = _percentile(durations, 0.60)
    selected = next((candidate for candidate in candidates if candidate >= desired), candidates[-1])
    return selected, len(durations)


def build_recommendation(candles: list[dict[str, Any]], current_percent: float) -> dict[str, Any]:
    """Recomenda uma banda usando o percentil 90 das quedas móveis de 5 min.

    A primeira entrada do Neutralis ocorre em meia banda. Por isso a queda
    histórica tolerada é multiplicada por dois para virar o gatilho exibido.
    """
    normalized: list[tuple[int, float, float]] = []
    for candle in candles:
        try:
            timestamp = int(candle["t"])
            high = float(candle["h"])
            low = float(candle["l"])
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp > 0 and high > 0 and low > 0 and math.isfinite(high) and math.isfinite(low):
            normalized.append((timestamp, high, low))
    normalized = sorted(set(normalized))
    if len(normalized) < 300:
        raise TriggerRecommendationError("Histórico insuficiente: são necessárias pelo menos 300 velas de 1 minuto")

    drawdowns: list[float] = []
    for index in range(len(normalized) - 4):
        window = normalized[index:index + 5]
        peak = window[0][1]
        worst = 0.0
        for _, high, low in window:
            peak = max(peak, high)
            worst = max(worst, (peak - low) / peak * 100)
        drawdowns.append(worst)

    tolerated_drop = _percentile(drawdowns, 0.90)
    recommended = _round_step(min(3.0, max(0.50, tolerated_drop * 2)))
    recommended_hold, recovery_samples = recommend_hold_seconds(normalized, recommended)
    first_at, last_at = normalized[0][0], normalized[-1][0]
    coverage_days = max(0.0, (last_at - first_at) / 86_400_000)
    confidence = "alta" if coverage_days >= 21 and len(normalized) >= 15_000 else "média" if coverage_days >= 7 and len(normalized) >= 5_000 else "baixa"
    return {
        "recommendedPercent": recommended,
        "recommendedHoldSeconds": recommended_hold,
        "recoverySampleCount": recovery_samples,
        "currentPercent": round(float(current_percent), 2),
        "candleCount": len(normalized),
        "coverageDays": round(coverage_days, 1),
        "confidence": confidence,
        "sampleStart": datetime.fromtimestamp(first_at / 1000, timezone.utc).isoformat(),
        "sampleEnd": datetime.fromtimestamp(last_at / 1000, timezone.utc).isoformat(),
        "downside90Percent": round(tolerated_drop, 3),
        "method": "90% das quedas móveis de 5 minutos e duração de 60% das recuperações; banda entre 0,50% e 3,00%",
    }


def fetch_candles(
    market: str,
    requester: Callable[[str, dict[str, Any]], Any],
    info_url: str,
    *,
    days: int = 14,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Busca em blocos para não depender do limite de velas por resposta."""
    end = int(now_ms if now_ms is not None else time.time() * 1000)
    start = end - days * 86_400_000
    chunk = 3_900 * 60_000
    candles: dict[int, dict[str, Any]] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + chunk)
        response = requester(info_url, {
            "type": "candleSnapshot",
            "req": {"coin": market, "interval": "1m", "startTime": cursor, "endTime": chunk_end},
        })
        if not isinstance(response, list):
            raise TriggerRecommendationError("A Hyperliquid não retornou o histórico esperado")
        for candle in response:
            if isinstance(candle, dict):
                try:
                    candles[int(candle["t"])] = candle
                except (KeyError, TypeError, ValueError):
                    continue
        cursor = chunk_end + 1
    return [candles[key] for key in sorted(candles)]


def recommend_trigger(
    market: str,
    current_percent: float,
    requester: Callable[[str, dict[str, Any]], Any],
    info_url: str,
) -> dict[str, Any]:
    candles = fetch_candles(market, requester, info_url)
    result = build_recommendation(candles, current_percent)
    result["market"] = market
    result["calculatedAt"] = datetime.now(timezone.utc).isoformat()
    return result
