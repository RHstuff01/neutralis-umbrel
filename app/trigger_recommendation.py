"""Otimização econômica, somente leitura, dos parâmetros do hedge."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Callable


class TriggerRecommendationError(Exception):
    pass


TRIGGERS = (0.50, 0.75, 1.00, 1.25, 1.50, 1.75, 2.00, 2.50, 3.00)
HOLDS = (60, 120, 180, 300, 600)
TRADE_COST = 0.0003  # taxa mais slippage conservador por execução
RECOVERY_BUFFER = 0.001


def _normalize(candles: list[dict[str, Any]]) -> list[tuple[int, float]]:
    values: dict[int, tuple[int, float]] = {}
    for candle in candles:
        try:
            timestamp = int(candle["t"])
            close = float(candle.get("c") or (float(candle["h"]) + float(candle["l"])) / 2)
        except (KeyError, TypeError, ValueError):
            continue
        if timestamp > 0 and close > 0 and math.isfinite(close):
            values[timestamp] = (timestamp, close)
    normalized = [values[key] for key in sorted(values)]
    if len(normalized) < 300:
        raise TriggerRecommendationError("Histórico insuficiente: são necessárias pelo menos 300 velas de 1 minuto")
    return normalized


def _windows(values: list[tuple[int, float]]) -> list[list[tuple[int, float]]]:
    size, stride = 1_440, 720
    if len(values) < size:
        return [values]
    result = [values[index:index + size] for index in range(0, len(values) - size + 1, stride)]
    tail = values[-size:]
    if result[-1][0][0] != tail[0][0]:
        result.append(tail)
    return result


def _target_and_value(price: float, context: dict[str, float]) -> tuple[float, float]:
    """Replica a exposição CLMM real da LP selecionada no preço projetado."""
    lower, upper, liquidity = context["lower"], context["upper"], context["liquidity"]
    sqrt_l, sqrt_u = math.sqrt(lower), math.sqrt(upper)
    if price <= lower:
        asset = liquidity * (1 / sqrt_l - 1 / sqrt_u)
        stable = 0.0
    elif price >= upper:
        asset = 0.0
        stable = liquidity * (sqrt_u - sqrt_l)
    else:
        sqrt_p = math.sqrt(price)
        asset = liquidity * (1 / sqrt_p - 1 / sqrt_u)
        stable = liquidity * (sqrt_p - sqrt_l)
    return asset, asset * price + stable


def _simulate_window(
    candles: list[tuple[int, float]], trigger_percent: float, hold_seconds: int, context: dict[str, float]
) -> dict[str, float]:
    ratio = context["lpPrice"] / context["hypMark"]
    reference_hyp = candles[0][1]
    reference_lp = reference_hyp * ratio
    _, initial_lp_value = _target_and_value(reference_lp, context)
    trigger = trigger_percent / 100
    half_trigger = trigger / 2
    short = 0.0
    base_short = 0.0
    lots: list[dict[str, float]] = []
    realized = costs = 0.0
    trades = losing_exits = 0
    whipsaw_loss = 0.0
    anchor = reference_hyp
    regime = "wait"
    worst_lp = worst_combined = 0.0

    def trade(delta: float, price: float) -> None:
        nonlocal short, costs, trades
        if abs(delta) <= 1e-12:
            return
        short += delta
        costs += abs(delta) * price * TRADE_COST
        trades += 1

    for timestamp, price in candles[1:]:
        lp_price = price * ratio
        target, lp_value = _target_and_value(lp_price, context)
        if regime == "wait":
            if price <= reference_hyp * (1 - half_trigger):
                trade(target, price)
                base_short = short
                anchor = price
                regime = "protected"
            elif price >= reference_hyp * (1 + half_trigger):
                regime = "upside"
        elif regime == "upside" and price <= reference_hyp:
            regime = "wait"
        elif regime == "protected":
            if price >= reference_hyp:
                for lot in lots:
                    realized += (lot["entry"] - price) * lot["size"]
                realized += (reference_hyp * (1 - half_trigger) - price) * base_short
                trade(-short, price)
                base_short, lots, regime = 0.0, [], "upside"
            else:
                # Recupera apenas parcelas adicionais, depois do tempo mínimo.
                kept: list[dict[str, float]] = []
                for lot in lots:
                    if timestamp - lot["openedAt"] >= hold_seconds * 1000 and price >= lot["entry"] * (1 - RECOVERY_BUFFER):
                        pnl = (lot["entry"] - price) * lot["size"]
                        realized += pnl
                        trade(-lot["size"], price)
                        if pnl < 0:
                            losing_exits += 1
                            whipsaw_loss += abs(pnl)
                    else:
                        kept.append(lot)
                lots = kept
                deficit = max(0.0, target - short)
                moved = price <= anchor * (1 - trigger)
                target_deficit = deficit / target if target > 0 else 0.0
                if deficit > 0 and (moved or target_deficit >= 0.10):
                    trade(deficit, price)
                    lots.append({"size": deficit, "entry": price, "openedAt": float(timestamp)})
                    anchor = price

        lp_pnl = lp_value - initial_lp_value
        open_pnl = 0.0
        if base_short:
            open_pnl += (reference_hyp * (1 - half_trigger) - price) * base_short
        open_pnl += sum((lot["entry"] - price) * lot["size"] for lot in lots)
        combined = lp_pnl + realized + open_pnl - costs
        worst_lp = min(worst_lp, lp_pnl)
        worst_combined = min(worst_combined, combined)

    final_price = candles[-1][1]
    final_lp = _target_and_value(final_price * ratio, context)[1] - initial_lp_value
    final_open = ((reference_hyp * (1 - half_trigger) - final_price) * base_short if base_short else 0.0)
    final_open += sum((lot["entry"] - final_price) * lot["size"] for lot in lots)
    combined = final_lp + realized + final_open - costs
    protection = 100.0 if worst_lp >= 0 else max(0.0, min(100.0, (1 - abs(worst_combined) / abs(worst_lp)) * 100))
    return {"combined": combined, "lp": final_lp, "hedge": realized + final_open - costs,
            "worst": worst_combined, "protection": protection, "trades": float(trades),
            "losingExits": float(losing_exits), "whipsaw": whipsaw_loss, "costs": costs}


def simulate_candidate(values: list[tuple[int, float]], trigger: float, hold: int, context: dict[str, float]) -> dict[str, Any]:
    samples = [_simulate_window(window, trigger, hold, context) for window in _windows(values)]
    principal = context["valueUsd"]
    result = {"triggerPercent": trigger, "holdSeconds": hold,
              "combinedResultUsd": mean(x["combined"] for x in samples),
              "lpResultUsd": mean(x["lp"] for x in samples), "hedgeResultUsd": mean(x["hedge"] for x in samples),
              "worstResultUsd": min(x["worst"] for x in samples),
              "protectionPercent": mean(x["protection"] for x in samples),
              "averageTrades": mean(x["trades"] for x in samples),
              "losingExits": sum(x["losingExits"] for x in samples),
              "whipsawLossUsd": mean(x["whipsaw"] for x in samples),
              "estimatedCostsUsd": mean(x["costs"] for x in samples), "sampleWindows": len(samples)}
    penalty = max(0.0, 90 - result["protectionPercent"]) * principal * 0.002
    result["score"] = result["combinedResultUsd"] + result["worstResultUsd"] * 0.35 - penalty
    return result


def _profile(name: str, item: dict[str, Any]) -> dict[str, Any]:
    result = {"name": name, **item}
    for key in ("combinedResultUsd", "lpResultUsd", "hedgeResultUsd", "worstResultUsd", "whipsawLossUsd", "estimatedCostsUsd", "score"):
        result[key] = round(float(result[key]), 2)
    result["protectionPercent"] = round(float(result["protectionPercent"]), 1)
    result["averageTrades"] = round(float(result["averageTrades"]), 1)
    result["losingExits"] = int(result["losingExits"])
    return result


def build_recommendation(candles: list[dict[str, Any]], current_percent: float, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if not context:
        raise TriggerRecommendationError("Selecione e salve uma LP antes de calcular a recomendação")
    required = ("valueUsd", "lower", "upper", "liquidity", "lpPrice", "hypMark")
    try:
        economic = {key: float(context[key]) for key in required}
    except (KeyError, TypeError, ValueError):
        raise TriggerRecommendationError("A LP selecionada não forneceu todos os dados para a simulação econômica") from None
    if not (economic["valueUsd"] > 0 and 0 < economic["lower"] < economic["upper"] and economic["liquidity"] > 0
            and economic["lpPrice"] > 0 and economic["hypMark"] > 0):
        raise TriggerRecommendationError("Os dados econômicos da LP selecionada são inválidos")
    values = _normalize(candles)
    candidates = [simulate_candidate(values, trigger, hold, economic) for trigger in TRIGGERS for hold in HOLDS]
    eligible = [item for item in candidates if item["protectionPercent"] >= 90] or candidates
    balanced = max(eligible, key=lambda x: (x["score"], -x["averageTrades"]))
    protected = max(candidates, key=lambda x: (x["protectionPercent"], x["score"]))
    floor = max(x["score"] for x in eligible) - economic["valueUsd"] * 0.0025
    economical = min((x for x in eligible if x["score"] >= floor), key=lambda x: (x["averageTrades"], -x["score"]))
    first_at, last_at = values[0][0], values[-1][0]
    days = max(0.0, (last_at - first_at) / 86_400_000)
    return {"recommendedPercent": balanced["triggerPercent"], "recommendedHoldSeconds": balanced["holdSeconds"],
            "currentPercent": round(float(current_percent), 2), "lpValueUsd": round(economic["valueUsd"], 2),
            "candleCount": len(values), "coverageDays": round(days, 1),
            "confidence": "alta" if days >= 21 else "média" if days >= 7 else "baixa",
            "sampleStart": datetime.fromtimestamp(first_at / 1000, timezone.utc).isoformat(),
            "sampleEnd": datetime.fromtimestamp(last_at / 1000, timezone.utc).isoformat(),
            "profiles": [_profile("Mais protegido", protected), _profile("Equilibrado", balanced), _profile("Menos operações", economical)],
            "method": "Otimização econômica da LP selecionada em janelas móveis de 24 h, com custos e proteção mínima de 90%"}


def fetch_candles(market: str, requester: Callable[[str, dict[str, Any]], Any], info_url: str, *, days: int = 30, now_ms: int | None = None) -> list[dict[str, Any]]:
    end = int(now_ms if now_ms is not None else time.time() * 1000)
    start, chunk, candles = end - days * 86_400_000, 3_900 * 60_000, {}
    cursor = start
    while cursor < end:
        chunk_end = min(end, cursor + chunk)
        response = requester(info_url, {"type": "candleSnapshot", "req": {"coin": market, "interval": "1m", "startTime": cursor, "endTime": chunk_end}})
        if not isinstance(response, list):
            raise TriggerRecommendationError("A Hyperliquid não retornou o histórico esperado")
        for candle in response:
            if isinstance(candle, dict):
                try: candles[int(candle["t"])] = candle
                except (KeyError, TypeError, ValueError): pass
        cursor = chunk_end + 1
    return [candles[key] for key in sorted(candles)]


def recommend_trigger(market: str, current_percent: float, requester: Callable[[str, dict[str, Any]], Any], info_url: str, context: dict[str, Any]) -> dict[str, Any]:
    result = build_recommendation(fetch_candles(market, requester, info_url), current_percent, context)
    result.update({"market": market, "calculatedAt": datetime.now(timezone.utc).isoformat()})
    return result
