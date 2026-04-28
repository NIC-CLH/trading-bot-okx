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

COINGLASS_BASE = "https://open-api.coinglass.com/public/v2"
COINGLASS_BASE_V3 = "https://open-api-v3.coinglass.com"
API_KEY = os.getenv("COINGLASS_API_KEY", "")

# Correspondance ticker → symbol Coinglass
SYMBOL_MAP = {
    "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP",
    "BNB": "BNB", "AVAX": "AVAX", "LINK": "LINK", "NEAR": "NEAR",
    "TIA": "TIA", "INJ": "INJ", "ARB": "ARB", "OP": "OP",
    "AAVE": "AAVE", "UNI": "UNI", "DOT": "DOT", "ATOM": "ATOM",
    "APT": "APT", "SUI": "SUI",
}


def _get(endpoint: str, params: dict = None) -> dict:
    headers = {"coinglassSecret": API_KEY} if API_KEY else {}
    try:
        resp = requests.get(
            f"{COINGLASS_BASE_V3}{endpoint}",
            params=params or {},
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("code") == "0" or data.get("success"):
                return data.get("data", {})
    except Exception as e:
        logger.debug(f"Coinglass {endpoint} : {e}")
    return {}


def get_liquidations_24h(ticker: str) -> dict:
    """
    Liquidations des dernières 24h pour un actif.
    Retourne le montant total liquidé en longs et en shorts.
    Signal : pic de liquidations longs = potentiel bottom / shorts = potentiel top
    """
    symbol = SYMBOL_MAP.get(ticker.upper(), ticker.upper())
    try:
        # Endpoint public (sans API key)
        resp = requests.get(
            "https://fapi.coinglass.com/api/futures/liquidation/detail/chart",
            params={"symbol": symbol, "timeType": "1", "exchangeName": ""},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("data"):
                d = data["data"]
                # Dernière bougie 24h
                longs_list = d.get("longLiquidationList", [])
                shorts_list = d.get("shortLiquidationList", [])
                total_long = sum(longs_list[-24:]) if longs_list else 0
                total_short = sum(abs(x) for x in shorts_list[-24:]) if shorts_list else 0
                return {
                    "long_liq_24h": total_long,
                    "short_liq_24h": total_short,
                    "ratio_ls": total_long / total_short if total_short > 0 else 1.0,
                }
    except Exception as e:
        logger.debug(f"Liquidations {ticker} : {e}")
    return {"long_liq_24h": 0, "short_liq_24h": 0, "ratio_ls": 1.0}


def get_open_interest(ticker: str) -> dict:
    """
    Open Interest total sur tous les exchanges futures.
    OI croissant + prix montant = tendance forte (bullish)
    OI croissant + prix baissant = distribution (bearish)
    OI décroissant = débouclage de positions = volatilité probable
    """
    symbol = SYMBOL_MAP.get(ticker.upper(), ticker.upper())
    try:
        resp = requests.get(
            "https://fapi.coinglass.com/api/futures/openInterest/chart",
            params={"symbol": symbol, "timeType": "h1", "exchangeName": ""},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("data"):
                oi_list = data["data"].get("dataMap", {})
                # Total OI toutes exchanges
                all_oi = []
                for exchange_data in oi_list.values():
                    if isinstance(exchange_data, list) and exchange_data:
                        all_oi.append(exchange_data[-1] if exchange_data else 0)

                if all_oi:
                    oi_current = sum(all_oi)
                    # Variation OI sur 4h
                    oi_4h_ago = sum(
                        (exchange_data[-4] if len(exchange_data) >= 4 else exchange_data[0])
                        for exchange_data in oi_list.values()
                        if isinstance(exchange_data, list) and exchange_data
                    )
                    oi_change_pct = ((oi_current - oi_4h_ago) / oi_4h_ago * 100) if oi_4h_ago else 0
                    return {"oi_current": oi_current, "oi_change_4h_pct": oi_change_pct}
    except Exception as e:
        logger.debug(f"Open Interest {ticker} : {e}")
    return {"oi_current": 0, "oi_change_4h_pct": 0}


def get_long_short_ratio(ticker: str) -> dict:
    """
    Ratio Long/Short sur les comptes des traders (Binance, OKX, Bybit).
    > 1.5 = trop de longs = danger de squeeze baissier
    < 0.7 = trop de shorts = danger de short squeeze haussier
    """
    symbol = SYMBOL_MAP.get(ticker.upper(), ticker.upper())
    try:
        resp = requests.get(
            "https://fapi.coinglass.com/api/futures/globalLongShortAccountRatio/chart",
            params={"symbol": symbol, "timeType": "h1"},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success") and data.get("data"):
                ratios = data["data"].get("longShortRatioList", [])
                if ratios:
                    ratio = float(ratios[-1])
                    return {"ls_ratio": ratio, "ls_bias": "long_heavy" if ratio > 1.5 else "short_heavy" if ratio < 0.7 else "balanced"}
    except Exception as e:
        logger.debug(f"L/S ratio {ticker} : {e}")
    return {"ls_ratio": 1.0, "ls_bias": "balanced"}


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
