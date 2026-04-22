"""
Module 4 — Gestion du Risque & Position Sizing
Kelly partiel, stop-loss ATR, take-profit R/R, alertes.
"""

import logging

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


# ─── Kelly Criterion partiel ──────────────────────────────────────────────────

def compute_kelly_size(
    win_rate: float,
    avg_win_pct: float,
    avg_loss_pct: float,
    portfolio_value: float,
    kelly_fraction: float = config.KELLY_FRACTION,
) -> dict:
    """
    Kelly partiel : f* = (p/|loss| - q/|win|) × kelly_fraction
    où p = win_rate, q = 1 - win_rate.

    win_rate      : probabilité de gain estimée [0, 1]
    avg_win_pct   : gain moyen en % (ex: 0.30 pour 30%)
    avg_loss_pct  : perte moyenne en % (ex: 0.10 pour 10%)
    """
    if avg_loss_pct <= 0 or avg_win_pct <= 0:
        return {"kelly_pct": 0, "kelly_usd": 0, "full_kelly": 0}

    q = 1 - win_rate
    # Kelly formula : (p/b - q) / (1/b) = p - q/b   where b = avg_win/avg_loss
    b = avg_win_pct / avg_loss_pct
    full_kelly = (win_rate * b - q) / b

    full_kelly = max(0, full_kelly)  # jamais négatif
    partial_kelly = full_kelly * kelly_fraction

    return {
        "full_kelly_pct": round(full_kelly * 100, 2),
        "kelly_partiel_pct": round(partial_kelly * 100, 2),
        "kelly_partiel_usd": round(partial_kelly * portfolio_value, 2),
        "ratio_b": round(b, 2),
    }


# ─── Stop-Loss basé sur ATR ───────────────────────────────────────────────────

def compute_atr_stop(
    current_price: float,
    atr: float,
    direction: str = "long",
    multiplier: float = config.ATR_STOP_MULTIPLIER,
) -> dict:
    """
    Stop-loss à current_price ± ATR × multiplier.
    direction : 'long' ou 'short'
    """
    stop_distance = atr * multiplier
    stop_pct = stop_distance / current_price * 100

    if direction == "long":
        stop_price = current_price - stop_distance
    else:
        stop_price = current_price + stop_distance

    return {
        "stop_price": round(stop_price, 6),
        "stop_distance_pct": round(stop_pct, 2),
        "atr_multiplie": round(stop_distance, 6),
    }


# ─── Take-Profit R/R 2:1 minimum ─────────────────────────────────────────────

def compute_take_profit(
    entry_price: float,
    stop_price: float,
    min_rr: float = config.MIN_RR_RATIO,
    targets: list[float] = None,
) -> dict:
    """
    Calcule les niveaux TP avec R/R minimum.
    targets : ratios R/R souhaités (défaut [1:1, 2:1, 3:1])
    """
    if targets is None:
        targets = [1.0, 2.0, 3.0]

    risk = abs(entry_price - stop_price)
    if risk == 0:
        return {}

    direction = "long" if stop_price < entry_price else "short"
    tp_levels = []

    for rr in targets:
        if direction == "long":
            tp = entry_price + risk * rr
        else:
            tp = entry_price - risk * rr

        tp_levels.append({
            "rr_ratio": rr,
            "tp_price": round(tp, 6),
            "gain_pct": round(risk * rr / entry_price * 100, 2),
        })

    min_tp = tp_levels[0] if tp_levels else None
    valid_rr = min_tp["rr_ratio"] >= min_rr if min_tp else False

    return {
        "risk_usd": round(risk, 6),
        "risk_pct": round(risk / entry_price * 100, 2),
        "tp_levels": tp_levels,
        "rr_valide": valid_rr,
    }


# ─── Analyse de position ──────────────────────────────────────────────────────

def analyze_position(
    ticker: str,
    current_price: float,
    atr: float | None,
    buy_price: float,
    portfolio_value: float,
    position_value: float,
    technical_signal: dict = None,
    win_rate: float = 0.50,
) -> dict:
    """Analyse complète d'une position : stop, TP, sizing, alertes."""

    result = {
        "ticker": ticker,
        "recommandations": [],
        "alertes": [],
    }

    if not atr or atr <= 0:
        result["alertes"].append("ATR indisponible — stop-loss manuel requis")
        return result

    # Stop ATR
    stop_data = compute_atr_stop(current_price, atr, direction="long")
    result["stop_loss"] = stop_data

    # Take-Profit
    tp_data = compute_take_profit(
        entry_price=current_price,
        stop_price=stop_data["stop_price"],
    )
    result["take_profit"] = tp_data

    # Kelly partiel
    # Estimation R/R depuis ATR : win moyen = 2× ATR, loss = 1× ATR
    avg_win = stop_data["stop_distance_pct"] / 100 * 2
    avg_loss = stop_data["stop_distance_pct"] / 100
    kelly_data = compute_kelly_size(
        win_rate=win_rate,
        avg_win_pct=avg_win,
        avg_loss_pct=avg_loss,
        portfolio_value=portfolio_value,
    )
    result["kelly"] = kelly_data

    # Alerte concentration
    position_pct = (position_value / portfolio_value) * 100 if portfolio_value else 0
    result["position_pct_portfolio"] = round(position_pct, 2)

    if position_pct > config.MAX_POSITION_PCT * 100:
        result["alertes"].append(
            f"Position trop grande : {position_pct:.1f}% du portefeuille "
            f"(max recommandé : {config.MAX_POSITION_PCT*100:.0f}%)"
        )

    # Recommandations basées sur le signal technique
    if technical_signal:
        score = technical_signal.get("score", 0)
        if score >= 1.5:
            result["recommandations"].append(
                f"Signal fort haussier (score {score}) — "
                f"Stop: ${stop_data['stop_price']:,.4f} | "
                f"TP1: ${tp_data['tp_levels'][0]['tp_price']:,.4f} | "
                f"TP2: ${tp_data['tp_levels'][1]['tp_price']:,.4f}"
            )
        elif score <= -1.5:
            result["recommandations"].append(
                f"Signal fort baissier (score {score}) — "
                f"Réduction de position suggérée"
            )

    # Alerte actif sans historique suffisant
    return result


# ─── Rapport gestion du risque ────────────────────────────────────────────────

def run(
    snapshot: dict,
    technical_results: dict,
    risk_data: dict,
) -> dict:
    """Génère les recommandations de gestion du risque pour toutes les positions."""

    portfolio_value = snapshot["valeur_totale_usd"]
    positions_analysis = []

    for pos in snapshot["positions"]:
        ticker = pos["ticker"]
        if pos["is_stablecoin"] or not pos["valeur_actuelle"]:
            continue

        tech = technical_results.get(ticker, {})
        atr = tech.get("atr_14")
        signal = tech.get("signal", {})
        current_price = pos["prix_actuel"]
        position_value = pos["valeur_actuelle"]

        analysis = analyze_position(
            ticker=ticker,
            current_price=current_price or 0,
            atr=atr,
            buy_price=pos["prix_achat"],
            portfolio_value=portfolio_value,
            position_value=position_value,
            technical_signal=signal,
        )
        positions_analysis.append(analysis)

    # Résumé des alertes globales
    all_alerts = risk_data.get("alertes_risque", []) + snapshot.get("alertes_concentration", [])
    for pa in positions_analysis:
        all_alerts.extend(pa.get("alertes", []))

    return {
        "positions_analyse": positions_analysis,
        "alertes_globales": all_alerts,
        "nb_alertes": len(all_alerts),
    }
