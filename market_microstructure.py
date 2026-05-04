"""
Market Microstructure — données OKX temps réel.

Indicateurs professionnels utilisés par les desks institutionnels :
- Funding Rate (perpétuels) : sentiment du marché dérivés
- Open Interest : flux d'argent réel entrant/sortant
- Long/Short Ratio : positionnement de la foule (contre-tendance)
- Taker Volume : agressivité acheteurs vs vendeurs
- Liquidation Heatmap : niveaux où les stops sautent

Score microstructure : -1.5 à +1.5
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

OKX_PUBLIC = "https://eea.okx.com"


def _get_public(path: str, params: dict = None) -> list | dict:
    """Appel API publique OKX (sans auth)."""
    try:
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{OKX_PUBLIC}{path}?{query}"
        else:
            url = f"{OKX_PUBLIC}{path}"
        resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        data = resp.json()
        if data.get("code") == "0":
            return data.get("data", [])
    except Exception as e:
        logger.debug(f"OKX public {path} : {e}")
    return []


# ─── Funding Rate ─────────────────────────────────────────────────────────────

def get_funding_rate(ticker: str) -> dict:
    """
    Taux de financement des contrats perpétuels.

    Interprétation pro :
    - Funding > +0.1% : les longs paient → marché suracheté → retournement probable
    - Funding < -0.05% : les shorts paient → capitulation → rebond probable
    - Funding proche 0 : marché équilibré, neutre

    C'est l'un des meilleurs indicateurs de sentiment à court terme en crypto.
    """
    # Essaie USDT-SWAP d'abord (standard), puis USDC-SWAP (OKX EEA fallback)
    inst_id = f"{ticker.upper()}-USDT-SWAP"
    data = _get_public("/api/v5/public/funding-rate", {"instId": inst_id})
    if not data:
        data = _get_public("/api/v5/public/funding-rate",
                           {"instId": f"{ticker.upper()}-USDC-SWAP"})

    if not data:
        return {"rate": None, "score": 0.0, "signal": "N/A"}

    try:
        rate = float(data[0].get("fundingRate", 0))
        rate_pct = rate * 100  # En pourcentage

        # Score : funding très positif = danger longs = signal baissier
        if rate_pct > 0.1:
            score = -1.0
            signal = f"Funding +{rate_pct:.3f}% — longs surexposés ⚠️"
        elif rate_pct > 0.05:
            score = -0.5
            signal = f"Funding +{rate_pct:.3f}% — légère surchauffe"
        elif rate_pct < -0.05:
            score = 1.0
            signal = f"Funding {rate_pct:.3f}% — shorts paient, rebond potentiel 🎯"
        elif rate_pct < -0.02:
            score = 0.5
            signal = f"Funding {rate_pct:.3f}% — légère capitulation"
        else:
            score = 0.0
            signal = f"Funding {rate_pct:.3f}% — neutre"

        return {"rate": rate_pct, "score": score, "signal": signal}

    except Exception as e:
        logger.debug(f"Funding rate {ticker} : {e}")
        return {"rate": None, "score": 0.0, "signal": "N/A"}


# ─── Open Interest ────────────────────────────────────────────────────────────

def get_open_interest(ticker: str) -> dict:
    """
    Open Interest (OI) — montant total des positions ouvertes.

    Interprétation pro :
    - Prix monte + OI monte : nouvelle liquidité = tendance forte et saine
    - Prix monte + OI baisse : short covering = rebond technique fragile
    - Prix baisse + OI monte : nouvelles positions short = pression vendeuse réelle
    - Prix baisse + OI baisse : liquidation longs = fin de baisse probable
    """
    inst_id = f"{ticker.upper()}-USDT-SWAP"
    data = _get_public("/api/v5/public/open-interest", {"instId": inst_id})
    if not data:
        data = _get_public("/api/v5/public/open-interest",
                           {"instId": f"{ticker.upper()}-USDC-SWAP"})

    if not data:
        return {"oi": None, "score": 0.0, "signal": "N/A"}

    try:
        oi = float(data[0].get("oiCcy", 0))  # En token
        oi_usd = float(data[0].get("oi", 0))  # En contrats

        return {
            "oi": round(oi, 0),
            "oi_usd": round(oi_usd, 0),
            "score": 0.0,  # Score calculé en combinaison avec le prix
            "signal": f"OI: {oi:,.0f} tokens ouverts",
        }
    except Exception as e:
        logger.debug(f"Open Interest {ticker} : {e}")
        return {"oi": None, "score": 0.0, "signal": "N/A"}


# ─── Long/Short Ratio ────────────────────────────────────────────────────────

def get_long_short_ratio(ticker: str) -> dict:
    """
    Ratio Long/Short des comptes traders OKX.

    Interprétation contra-tendance (outil contrarian) :
    - L/S > 2.0 : la foule est majoritairement longue → risque de retournement
    - L/S < 0.5 : la foule est majoritairement short → squeeze potentiel
    - L/S entre 0.8 et 1.2 : équilibré, pas de signal fort

    "When everyone is long, who's left to buy?" — adage pro
    """
    try:
        data = _get_public(
            "/api/v5/rubik/stat/contracts/long-short-account-ratio",
            {"instId": f"{ticker.upper()}-USDT", "period": "5m"}
        )
        if not data:
            return {"ratio": None, "score": 0.0, "signal": "N/A"}

        ratio = float(data[0].get("longShortRatio", 1.0))

        if ratio > 2.5:
            score = -1.0
            signal = f"L/S {ratio:.2f} — foule surexposée longs ⚠️"
        elif ratio > 1.8:
            score = -0.5
            signal = f"L/S {ratio:.2f} — majorité longue"
        elif ratio < 0.4:
            score = 1.0
            signal = f"L/S {ratio:.2f} — short squeeze potentiel 🎯"
        elif ratio < 0.6:
            score = 0.5
            signal = f"L/S {ratio:.2f} — majorité short"
        else:
            score = 0.0
            signal = f"L/S {ratio:.2f} — équilibré"

        return {"ratio": round(ratio, 3), "score": score, "signal": signal}

    except Exception as e:
        logger.debug(f"L/S ratio {ticker} : {e}")
        return {"ratio": None, "score": 0.0, "signal": "N/A"}


# ─── Taker Volume ─────────────────────────────────────────────────────────────

def get_taker_volume(ticker: str) -> dict:
    """
    Volume taker buy vs sell — mesure l'agressivité des acheteurs.

    Les takers agressifs (market orders) montrent la conviction réelle.
    Ratio > 1.5 : acheteurs agressifs dominent → momentum haussier
    Ratio < 0.67 : vendeurs agressifs dominent → pression baissière
    """
    try:
        data = _get_public(
            "/api/v5/rubik/stat/taker-volume",
            {"instId": f"{ticker.upper()}-USDT", "instType": "SPOT", "period": "5m"}
        )
        if not data:
            return {"ratio": None, "score": 0.0, "signal": "N/A"}

        buy_vol = float(data[0].get("buyVol", 0))
        sell_vol = float(data[0].get("sellVol", 1))
        ratio = buy_vol / sell_vol if sell_vol > 0 else 1.0

        if ratio > 1.8:
            score = 0.75
            signal = f"Takers buy/sell {ratio:.2f} — acheteurs dominants 💪"
        elif ratio > 1.3:
            score = 0.4
            signal = f"Takers {ratio:.2f} — légère pression acheteuse"
        elif ratio < 0.55:
            score = -0.75
            signal = f"Takers {ratio:.2f} — vendeurs dominants 📉"
        elif ratio < 0.77:
            score = -0.4
            signal = f"Takers {ratio:.2f} — légère pression vendeuse"
        else:
            score = 0.0
            signal = f"Takers {ratio:.2f} — équilibré"

        return {"ratio": round(ratio, 3), "buy_vol": buy_vol, "sell_vol": sell_vol,
                "score": score, "signal": signal}

    except Exception as e:
        logger.debug(f"Taker volume {ticker} : {e}")
        return {"ratio": None, "score": 0.0, "signal": "N/A"}


# ─── Liquidation Levels ───────────────────────────────────────────────────────

def get_liquidation_context(ticker: str) -> dict:
    """
    Niveaux de liquidation récents — indique où les stops vont sauter.
    Les algos chassent ces niveaux avant de repartir dans la tendance.
    """
    try:
        # Liquidations long récentes
        liq_long = _get_public(
            "/api/v5/public/liquidation-orders",
            {"instType": "SWAP", "instId": f"{ticker.upper()}-USDT-SWAP",  # fallback USDC-SWAP géré au niveau caller
             "side": "buy", "state": "filled", "limit": "20"}
        )
        liq_short = _get_public(
            "/api/v5/public/liquidation-orders",
            {"instType": "SWAP", "instId": f"{ticker.upper()}-USDT-SWAP",  # fallback USDC-SWAP géré au niveau caller
             "side": "sell", "state": "filled", "limit": "20"}
        )

        long_count = len(liq_long) if isinstance(liq_long, list) else 0
        short_count = len(liq_short) if isinstance(liq_short, list) else 0

        # Beaucoup de liquidations long récentes = oversold, rebond probable
        if long_count > 15:
            score = 0.5
            signal = f"Liquidations longs récentes : {long_count} — flush potentiellement terminé"
        elif short_count > 15:
            score = -0.5
            signal = f"Liquidations shorts récentes : {short_count} — squeeze possible"
        else:
            score = 0.0
            signal = f"Liq: {long_count} longs / {short_count} shorts"

        return {"long_liqs": long_count, "short_liqs": short_count,
                "score": score, "signal": signal}

    except Exception as e:
        logger.debug(f"Liquidations {ticker} : {e}")
        return {"long_liqs": 0, "short_liqs": 0, "score": 0.0, "signal": "N/A"}


# ─── Analyse microstructure complète ─────────────────────────────────────────

def analyze(ticker: str) -> dict:
    """
    Analyse complète de la microstructure de marché.
    Score : -1.5 à +1.5
    """
    logger.info(f"Microstructure : {ticker}")

    funding = get_funding_rate(ticker)
    ls_ratio = get_long_short_ratio(ticker)
    taker = get_taker_volume(ticker)
    liqs = get_liquidation_context(ticker)
    oi = get_open_interest(ticker)

    # Score composite microstructure
    # Funding (40%) + L/S (25%) + Taker (25%) + Liquidations (10%)
    score = (
        funding["score"] * 0.40
        + ls_ratio["score"] * 0.25
        + taker["score"] * 0.25
        + liqs["score"] * 0.10
    )
    score = round(max(-1.5, min(1.5, score)), 2)

    signals = []
    for item in [funding, ls_ratio, taker, liqs]:
        if item.get("signal") and item["signal"] != "N/A":
            signals.append(item["signal"])

    if score >= 0.8:
        verdict = "MICROSTRUCTURE HAUSSIÈRE"
    elif score >= 0.3:
        verdict = "MICROSTRUCTURE LÉGÈREMENT HAUSSIÈRE"
    elif score <= -0.8:
        verdict = "MICROSTRUCTURE BAISSIÈRE"
    elif score <= -0.3:
        verdict = "MICROSTRUCTURE LÉGÈREMENT BAISSIÈRE"
    else:
        verdict = "MICROSTRUCTURE NEUTRE"

    time.sleep(0.5)

    return {
        "ticker": ticker,
        "score": score,
        "verdict": verdict,
        "signals": signals,
        "funding": funding,
        "long_short": ls_ratio,
        "taker_volume": taker,
        "liquidations": liqs,
        "open_interest": oi,
    }


def run(tickers: list[str]) -> dict[str, dict]:
    """Analyse microstructure de plusieurs actifs."""
    results = {}
    for ticker in tickers:
        results[ticker] = analyze(ticker)
    return results
