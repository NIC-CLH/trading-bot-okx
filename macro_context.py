"""
Contexte macro-économique — filtre global sur tous les signaux.

Sources :
- FRED API (gratuit) : DXY, taux Fed, rendements obligataires
- Deribit (gratuit) : DVOL — volatilité implicite BTC/ETH
- PyTrends (gratuit) : intérêt de recherche Google pour détecter FOMO/panique retail

Logique : si le macro est contre nous (DXY fort, Fed hawkish, DVOL élevé),
on réduit la conviction des signaux techniques même si ceux-ci sont positifs.

Score global : -1.0 à +1.0 (multiplicateur de contexte)
"""

import logging
import os
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

FRED_API_KEY = os.getenv("FRED_API_KEY", "")  # Gratuit sur fred.stlouisfed.org


# ── 1. FRED API — DXY + Fed Rate ─────────────────────────────────────────────

def get_dxy() -> dict:
    """
    Dollar Index (DXY) — corrélation négative forte avec crypto.
    DXY monte → crypto baisse (risk-off)
    DXY baisse → crypto monte (risk-on)
    """
    if not FRED_API_KEY:
        # Fallback sans clé : estimation via Yahoo Finance
        return _get_dxy_yahoo()

    try:
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "DTWEXBGS",  # Nominal Broad Dollar Index
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "limit": 10,
                "sort_order": "desc",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            obs = resp.json().get("observations", [])
            obs = [o for o in obs if o.get("value") != "."]
            if len(obs) >= 2:
                current = float(obs[0]["value"])
                prev = float(obs[1]["value"])
                change_pct = (current - prev) / prev * 100
                return {"dxy": current, "dxy_change_pct": change_pct, "source": "FRED"}
    except Exception as e:
        logger.debug(f"FRED DXY : {e}")

    return _get_dxy_yahoo()


def _get_dxy_yahoo() -> dict:
    """Fallback DXY via Yahoo Finance (pas de clé requise)."""
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if resp.status_code == 200:
            result = resp.json().get("chart", {}).get("result", [])
            if result:
                closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                closes = [c for c in closes if c is not None]
                if len(closes) >= 2:
                    current = closes[-1]
                    prev = closes[-2]
                    change_pct = (current - prev) / prev * 100
                    return {"dxy": current, "dxy_change_pct": change_pct, "source": "Yahoo"}
    except Exception as e:
        logger.debug(f"Yahoo DXY : {e}")
    return {"dxy": 104.0, "dxy_change_pct": 0.0, "source": "default"}


def get_fed_rate() -> dict:
    """Taux directeur Fed — taux élevé = contexte défavorable crypto."""
    if not FRED_API_KEY:
        return {"fed_rate": None, "source": "unavailable"}
    try:
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": "FEDFUNDS",
                "api_key": FRED_API_KEY,
                "file_type": "json",
                "limit": 3,
                "sort_order": "desc",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            obs = resp.json().get("observations", [])
            obs = [o for o in obs if o.get("value") != "."]
            if obs:
                rate = float(obs[0]["value"])
                return {"fed_rate": rate, "source": "FRED"}
    except Exception as e:
        logger.debug(f"FRED Fed rate : {e}")
    return {"fed_rate": None, "source": "unavailable"}


# ── 2. Deribit DVOL — Volatilité implicite ───────────────────────────────────

def get_dvol(asset: str = "BTC") -> dict:
    """
    DVOL = Deribit Volatility Index (comme le VIX mais pour crypto).
    DVOL < 50 → faible volatilité, marché calme → signaux plus fiables
    DVOL 50-80 → volatilité modérée → normal
    DVOL > 80 → volatilité extrême → signaux moins fiables, risque élevé
    DVOL > 120 → panique / euphorie → contre-indicateur (souvent un bottom/top)
    """
    asset = asset.upper()
    if asset not in ("BTC", "ETH"):
        asset = "BTC"  # DVOL disponible uniquement sur BTC et ETH

    try:
        resp = requests.get(
            "https://www.deribit.com/api/v2/public/get_volatility_index_data",
            params={
                "currency": asset,
                "start_timestamp": int((time.time() - 86400) * 1000),  # 24h
                "end_timestamp": int(time.time() * 1000),
                "resolution": "3600",  # 1h
            },
            timeout=10,
        )
        if resp.status_code == 200:
            result = resp.json().get("result", {})
            data = result.get("data", [])
            if data:
                # [timestamp, open, high, low, close]
                current_dvol = float(data[-1][4])
                dvol_24h_ago = float(data[0][4]) if len(data) > 1 else current_dvol
                dvol_change = current_dvol - dvol_24h_ago
                return {
                    "dvol": current_dvol,
                    "dvol_change_24h": dvol_change,
                    "regime": (
                        "calm" if current_dvol < 50 else
                        "normal" if current_dvol < 80 else
                        "elevated" if current_dvol < 120 else
                        "extreme"
                    ),
                    "asset": asset,
                }
    except Exception as e:
        logger.debug(f"Deribit DVOL {asset} : {e}")
    return {"dvol": 65.0, "dvol_change_24h": 0, "regime": "normal", "asset": asset}


# ── 3. PyTrends — Intérêt de recherche Google ────────────────────────────────

def get_google_trends(ticker: str) -> dict:
    """
    Intérêt de recherche Google pour le ticker (7 jours).
    Pic soudain → FOMO retail → souvent proche d'un top
    Intérêt très bas → indifférence → souvent proche d'un bottom (accumulation silencieuse)
    Score 0-100 (relatif au pic historique dans la période)
    """
    try:
        from pytrends.request import TrendReq
        pt = TrendReq(hl="en-US", tz=0, timeout=(10, 25))

        keyword = f"{ticker} crypto"
        pt.build_payload([keyword], timeframe="now 7-d", geo="")

        interest = pt.interest_over_time()
        if not interest.empty and keyword in interest.columns:
            values = interest[keyword].tolist()
            if values:
                current = values[-1]
                avg = sum(values) / len(values)
                trend = "rising" if current > avg * 1.3 else "falling" if current < avg * 0.7 else "stable"
                return {
                    "trends_score": current,
                    "trends_avg_7d": round(avg, 1),
                    "trend": trend,
                }
    except Exception as e:
        logger.debug(f"PyTrends {ticker} : {e}")
    return {"trends_score": 50, "trends_avg_7d": 50, "trend": "stable"}


# ── Score macro global ────────────────────────────────────────────────────────

def analyze(ticker: str = None) -> dict:
    """
    Contexte macro global — indépendant du ticker (sauf Google Trends).
    Score : -1.0 (macro très défavorable) à +1.0 (macro très favorable)
    """
    score = 0.0
    signals = []

    # DXY
    dxy_data = get_dxy()
    dxy_change = dxy_data.get("dxy_change_pct", 0)
    dxy_val = dxy_data.get("dxy", 104)

    if dxy_change < -0.5:
        score += 0.3
        signals.append(f"DXY en baisse {dxy_change:.2f}% → contexte risk-on favorable")
    elif dxy_change > 0.5:
        score -= 0.3
        signals.append(f"DXY en hausse +{dxy_change:.2f}% → contexte risk-off défavorable")

    # Niveau absolu DXY
    if dxy_val > 108:
        score -= 0.2
        signals.append(f"DXY élevé ({dxy_val:.1f}) → dollar fort = pression crypto")
    elif dxy_val < 100:
        score += 0.2
        signals.append(f"DXY faible ({dxy_val:.1f}) → dollar faible = favorable crypto")

    # DVOL BTC
    dvol_data = get_dvol("BTC")
    dvol = dvol_data.get("dvol", 65)
    regime = dvol_data.get("regime", "normal")
    dvol_change = dvol_data.get("dvol_change_24h", 0)

    if regime == "extreme":
        # Volatilité extrême = contre-indicateur (panique ou euphorie = souvent reversal)
        score += 0.2 if dvol_change < 0 else -0.2
        signals.append(f"DVOL extrême ({dvol:.0f}) → marché en panique/euphorie — contre-indicateur")
    elif regime == "elevated" and dvol_change > 10:
        score -= 0.2
        signals.append(f"DVOL en forte hausse ({dvol:.0f}+{dvol_change:.0f}) → volatilité croissante, prudence")
    elif regime == "calm":
        score += 0.1
        signals.append(f"DVOL bas ({dvol:.0f}) → marché calme, signaux techniques plus fiables")

    # Google Trends (si ticker fourni)
    if ticker:
        trends = get_google_trends(ticker)
        trend = trends.get("trend", "stable")
        trends_score = trends.get("trends_score", 50)

        if trend == "rising" and trends_score > 70:
            score -= 0.2  # FOMO retail = souvent proche d'un top
            signals.append(f"Google Trends {ticker} en pic ({trends_score}) → attention FOMO retail")
        elif trend == "falling" and trends_score < 20:
            score += 0.2  # Désintérêt = souvent proche d'un bottom
            signals.append(f"Google Trends {ticker} au plus bas ({trends_score}) → accumulation silencieuse possible")

    score = round(max(-1.0, min(1.0, score)), 2)

    if not signals:
        signals.append("Contexte macro neutre")

    verdict = (
        "Macro favorable — risk-on" if score > 0.3 else
        "Macro défavorable — risk-off" if score < -0.3 else
        "Macro neutre"
    )

    logger.info(
        f"Macro : score={score:+.2f} | DXY={dxy_val:.1f} ({dxy_change:+.2f}%) "
        f"| DVOL={dvol:.0f} ({regime}) | {verdict}"
    )

    return {
        "score": score,
        "verdict": verdict,
        "signals": signals,
        "dxy": dxy_val,
        "dxy_change_pct": dxy_change,
        "dvol": dvol,
        "dvol_regime": regime,
        "fed_rate": get_fed_rate().get("fed_rate"),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = analyze("BTC")
    print(f"\nMacro score : {result['score']:+.2f} — {result['verdict']}")
    print(f"DXY : {result['dxy']:.1f} ({result['dxy_change_pct']:+.2f}%)")
    print(f"DVOL BTC : {result['dvol']:.0f} ({result['dvol_regime']})")
    for s in result["signals"]:
        print(f"  • {s}")
