"""
Coinglass — Données de liquidations et Open Interest.

Les zones de liquidation sont des niveaux de prix où des milliers d'ordres
leveragés seront forcés à se fermer. Le marché est attiré vers ces zones
comme un aimant — c'est l'un des signaux prédictifs les plus puissants en crypto.

Données gratuites : liquidations 24h, Open Interest, Long/Short ratio.
API key optionnelle (coinglass.com → créer un compte gratuit → API key).
"""

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# Coinglass abandonne le 23/09/2026 : endpoints fapi en 404, API v3 payante.
# Les memes donnees (L/S, OI, flux taker) sont gratuites chez OKX.

# Seuils de desequilibre du ratio long/short (comptes, pas volume).
# Au-dela : positionnement extreme -> risque de squeeze dans le sens inverse.
LS_LONG_HEAVY  = 1.30   # trop de longs -> risque de liquidation baissiere
LS_SHORT_HEAVY = 0.77   # trop de shorts -> risque de short squeeze haussier

# Correspondance ticker → symbol Coinglass
SYMBOL_MAP = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP",
    "BNB": "BNB", "AVAX": "AVAX", "LINK": "LINK", "NEAR": "NEAR",
    "TIA": "TIA", "INJ": "INJ", "ARB": "ARB", "OP": "OP",
    "AAVE": "AAVE", "UNI": "UNI", "DOT": "DOT", "ATOM": "ATOM",
    "APT": "APT", "SUI": "SUI",
}


def _okx_stat(endpoint: str, params: dict) -> list:
    """Statistiques publiques OKX (rubik). Retourne [] en cas d'echec."""
    try:
        import okx_client as _okx
        return _okx._get(endpoint, params) or []
    except Exception as e:
        logger.debug(f"OKX stat {endpoint} : {e}")
        return []


def get_long_short_ratio(ticker: str) -> dict:
    """
    Ratio comptes long/short via OKX (gratuit).
    Remplace Coinglass : leurs endpoints fapi renvoient 404 depuis 2026 et
    l'API v3 exige une cle payante.
    """
    data = _okx_stat("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                     {"ccy": ticker.upper(), "period": "1H"})
    if not data:
        return {"ls_ratio": 1.0, "ls_bias": "balanced", "disponible": False}
    try:
        ratio = float(data[0][1])
    except (IndexError, ValueError, TypeError):
        return {"ls_ratio": 1.0, "ls_bias": "balanced", "disponible": False}

    bias = ("long_heavy" if ratio >= LS_LONG_HEAVY
            else "short_heavy" if ratio <= LS_SHORT_HEAVY
            else "balanced")
    return {"ls_ratio": round(ratio, 3), "ls_bias": bias, "disponible": True}


def get_open_interest(ticker: str) -> dict:
    """Variation d'open interest sur 4h via OKX (gratuit)."""
    data = _okx_stat("/api/v5/rubik/stat/contracts/open-interest-volume",
                     {"ccy": ticker.upper(), "period": "1H"})
    if len(data) < 5:
        return {"oi_change_4h_pct": 0.0, "disponible": False}
    try:
        # OKX renvoie du plus recent au plus ancien : [ts, oi, volume]
        oi_now = float(data[0][1])
        oi_4h  = float(data[4][1])
        if oi_4h <= 0:
            return {"oi_change_4h_pct": 0.0, "disponible": False}
        return {
            "oi_change_4h_pct": round((oi_now - oi_4h) / oi_4h * 100, 2),
            "disponible": True,
        }
    except (IndexError, ValueError, TypeError):
        return {"oi_change_4h_pct": 0.0, "disponible": False}


def get_liquidations_24h(ticker: str) -> dict:
    """
    Proxy de pression acheteur/vendeur via le volume taker OKX (gratuit).
    Les vraies donnees de liquidation ne sont plus accessibles sans abonnement ;
    le desequilibre taker capte le meme phenomene (qui subit la pression).
    """
    data = _okx_stat("/api/v5/rubik/stat/taker-volume",
                     {"ccy": ticker.upper(), "instType": "CONTRACTS", "period": "1H"})
    if len(data) < 24:
        return {"long_liq_24h": 0.0, "short_liq_24h": 0.0, "disponible": False}
    try:
        # [ts, sellVol, buyVol] sur les 24 dernieres heures
        sell = sum(float(r[1]) for r in data[:24])
        buy  = sum(float(r[2]) for r in data[:24])
        # On mappe sur la semantique d'origine : pression vendeuse -> longs liquides
        return {"long_liq_24h": sell, "short_liq_24h": buy, "disponible": True}
    except (IndexError, ValueError, TypeError):
        return {"long_liq_24h": 0.0, "short_liq_24h": 0.0, "disponible": False}


def analyze(ticker: str) -> dict:
    """
    Analyse complète Coinglass pour un ticker.
    Score : -1.5 à +1.5
    """
    liq = get_liquidations_24h(ticker)
    oi = get_open_interest(ticker)
    ls = get_long_short_ratio(ticker)

    score = 0.0
    signals = []

    # ── Liquidations ─────────────────────────────────────────────────────────
    long_liq = liq.get("long_liq_24h", 0)
    short_liq = liq.get("short_liq_24h", 0)

    if short_liq > long_liq * 2:
        # Beaucoup de shorts liquidés → pression haussière
        score += 0.5
        signals.append(f"Liquidations shorts dominantes (${short_liq/1e6:.1f}M) → pression haussière")
    elif long_liq > short_liq * 2:
        # Beaucoup de longs liquidés → pression baissière
        score -= 0.5
        signals.append(f"Liquidations longs dominantes (${long_liq/1e6:.1f}M) → pression baissière")

    # ── Open Interest ─────────────────────────────────────────────────────────
    oi_change = oi.get("oi_change_4h_pct", 0)
    if oi_change > 5:
        score += 0.4
        signals.append(f"OI en hausse +{oi_change:.1f}% sur 4h → conviction haussière")
    elif oi_change < -5:
        score -= 0.4
        signals.append(f"OI en baisse {oi_change:.1f}% sur 4h → débouclage de positions")

    # ── Long/Short ratio ──────────────────────────────────────────────────────
    ls_ratio = ls.get("ls_ratio", 1.0)
    bias = ls.get("ls_bias", "balanced")

    if bias == "short_heavy":
        # Trop de shorts = potentiel short squeeze
        score += 0.6
        signals.append(f"L/S ratio {ls_ratio:.2f} — trop de shorts → risque short squeeze haussier")
    elif bias == "long_heavy":
        # Trop de longs = potentiel dump
        score -= 0.6
        signals.append(f"L/S ratio {ls_ratio:.2f} — trop de longs → risque liquidation baissière")

    score = round(max(-1.5, min(1.5, score)), 2)

    if not signals:
        signals.append("Données liquidations neutres")

    verdict = (
        "Positioning favorable" if score > 0.5 else
        "Positioning défavorable" if score < -0.5 else
        "Positioning neutre"
    )

    logger.info(f"Coinglass {ticker} : score={score:+.2f} | L/S={ls_ratio:.2f} | OI Δ={oi_change:+.1f}%")

    return {
        "score": score,
        "disponible": bool(
            ls.get("disponible") or oi.get("disponible") or liq.get("disponible")
        ),
        "verdict": verdict,
        "signals": signals,
        "ls_ratio": ls_ratio,
        "oi_change_4h_pct": oi_change,
        "long_liq_24h_m": round(long_liq / 1e6, 2),
        "short_liq_24h_m": round(short_liq / 1e6, 2),
    }


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    for t in ["BTC", "ETH", "SOL", "XRP"]:
        r = analyze(t)
        print(f"{t}: score={r['score']:+.2f} | {r['verdict']} | {r['signals'][0]}")
        time.sleep(1)
