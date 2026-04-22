"""
On-Chain & Macro Intelligence — données gratuites.

Sources :
- CoinGecko (developer API gratuit) : exchange flows, market data
- DefiLlama : TVL des protocoles DeFi
- Alternative.me : Fear & Greed (déjà dans news_sentiment)
- Glassnode public : métriques on-chain de base
- CoinMarketCap (API clé optionnelle)

Score on-chain : -1.0 à +1.0
"""

import logging
import time

import requests

logger = logging.getLogger(__name__)

# Mapping ticker -> CoinGecko ID
COINGECKO_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "LINK": "chainlink", "AVAX": "avalanche-2", "DOT": "polkadot",
    "NEAR": "near", "AAVE": "aave", "UNI": "uniswap", "ARB": "arbitrum",
    "TIA": "celestia", "INJ": "injective-protocol", "JTO": "jito-governance-token",
    "SUI": "sui", "APT": "aptos", "OP": "optimism", "STX": "blockstack",
    "WIF": "dogwifcoin", "PENDLE": "pendle", "ENA": "ethena",
    "ATOM": "cosmos", "DYDX": "dydx-chain",
}

# Mapping ticker -> DefiLlama protocol slug
DEFILLAMA_SLUGS = {
    "AAVE": "aave-v3", "UNI": "uniswap-v3", "LINK": "chainlink",
    "ARB": "arbitrum", "OP": "optimism", "SOL": "raydium",
    "PENDLE": "pendle", "ENA": "ethena", "DYDX": "dydx-v4",
    "INJ": "helix", "JTO": "jito",
}


# ─── CoinGecko ────────────────────────────────────────────────────────────────

def get_coingecko_metrics(ticker: str) -> dict:
    """
    Métriques CoinGecko : échanges, sentiment, développeur, communauté.
    Indique si des baleines entrent/sortent des exchanges.
    """
    coin_id = COINGECKO_IDS.get(ticker.upper())
    if not coin_id:
        return {"score": 0.0, "signal": "ID CoinGecko inconnu"}

    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "true",
                "developer_data": "false",
                "sparkline": "false",
            },
            timeout=12,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = resp.json()

        score = 0.0
        signals = []
        md = data.get("market_data", {})

        # Variation de volume 24h (volume élevé confirme le mouvement)
        vol_24h = float(md.get("total_volume", {}).get("usd", 0))
        mc = float(md.get("market_cap", {}).get("usd", 1))
        vol_to_mc = vol_24h / mc if mc > 0 else 0

        if vol_to_mc > 0.25:
            score += 0.3
            signals.append(f"Volume/MC {vol_to_mc:.2f} — forte activité")
        elif vol_to_mc < 0.02:
            score -= 0.1
            signals.append(f"Volume/MC {vol_to_mc:.2f} — activité faible")

        # Variation de prix multi-timeframes
        change_1h = float(md.get("price_change_percentage_1h_in_currency", {}).get("usd", 0) or 0)
        change_7d = float(md.get("price_change_percentage_7d_in_currency", {}).get("usd", 0) or 0)
        change_30d = float(md.get("price_change_percentage_30d_in_currency", {}).get("usd", 0) or 0)

        # Momentum multi-timeframe
        if change_7d > 15 and change_30d > 20:
            score += 0.4
            signals.append(f"Momentum fort: 7j +{change_7d:.1f}%, 30j +{change_30d:.1f}%")
        elif change_7d > 5 and change_30d > 5:
            score += 0.2
        elif change_7d < -15 and change_30d < -20:
            score -= 0.4
            signals.append(f"Momentum négatif: 7j {change_7d:.1f}%, 30j {change_30d:.1f}%")
        elif change_7d < -5:
            score -= 0.2

        # Distance depuis ATH (potentiel de récupération)
        ath = float(md.get("ath", {}).get("usd", 0) or 0)
        current = float(md.get("current_price", {}).get("usd", 0) or 0)
        if ath > 0 and current > 0:
            ath_pct = (current - ath) / ath * 100
            if ath_pct > -20:
                signals.append(f"À {abs(ath_pct):.0f}% de l'ATH")
            elif ath_pct < -80:
                score += 0.2  # Très loin de l'ATH = potentiel upside énorme
                signals.append(f"À {abs(ath_pct):.0f}% de l'ATH — fort potentiel")

        # Sentiment communauté (votes positifs/négatifs)
        sentiment_up = float(data.get("sentiment_votes_up_percentage") or 0)
        if sentiment_up > 75:
            score += 0.2
            signals.append(f"Sentiment communauté positif ({sentiment_up:.0f}%)")
        elif sentiment_up < 30:
            score -= 0.2
            signals.append(f"Sentiment communauté négatif ({sentiment_up:.0f}%)")

        score = round(max(-1.0, min(1.0, score)), 2)

        return {
            "score": score,
            "signals": signals[:4],
            "vol_to_mc": round(vol_to_mc, 4),
            "change_7d": change_7d,
            "change_30d": change_30d,
            "ath_pct": round(ath_pct, 1) if ath > 0 else None,
        }

    except Exception as e:
        logger.debug(f"CoinGecko {ticker} : {e}")
        return {"score": 0.0, "signals": [], "vol_to_mc": None}


# ─── DefiLlama TVL ────────────────────────────────────────────────────────────

def get_defillama_tvl(ticker: str) -> dict:
    """
    TVL (Total Value Locked) depuis DefiLlama.
    Croissance TVL = adoption réelle du protocole = signal haussier fondamental.
    Chute TVL = fuite de capital = signal baissier.
    """
    slug = DEFILLAMA_SLUGS.get(ticker.upper())
    if not slug:
        return {"score": 0.0, "tvl": None, "signal": "Non DeFi ou slug inconnu"}

    try:
        resp = requests.get(
            f"https://api.llama.fi/protocol/{slug}",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = resp.json()

        tvl = data.get("tvl")
        if not tvl or not isinstance(tvl, list) or len(tvl) < 30:
            return {"score": 0.0, "tvl": None, "signal": "Données TVL insuffisantes"}

        # Prend les 30 dernières entrées
        recent = tvl[-30:]
        tvl_now = float(recent[-1].get("totalLiquidityUSD", 0))
        tvl_7d = float(recent[-7].get("totalLiquidityUSD", 0)) if len(recent) >= 7 else tvl_now
        tvl_30d = float(recent[0].get("totalLiquidityUSD", 0))

        change_7d = (tvl_now - tvl_7d) / tvl_7d * 100 if tvl_7d > 0 else 0
        change_30d = (tvl_now - tvl_30d) / tvl_30d * 100 if tvl_30d > 0 else 0

        score = 0.0
        signal = ""

        if change_7d > 20:
            score = 0.5
            signal = f"TVL +{change_7d:.0f}% (7j) — adoption forte 🚀"
        elif change_7d > 10:
            score = 0.3
            signal = f"TVL +{change_7d:.0f}% (7j) — croissance saine"
        elif change_7d > 5:
            score = 0.1
            signal = f"TVL +{change_7d:.0f}% (7j)"
        elif change_7d < -20:
            score = -0.5
            signal = f"TVL {change_7d:.0f}% (7j) — fuite de capital ⚠️"
        elif change_7d < -10:
            score = -0.3
            signal = f"TVL {change_7d:.0f}% (7j) — déclin notable"
        else:
            signal = f"TVL stable ({tvl_now/1e9:.2f}B USD)"

        tvl_fmt = f"${tvl_now/1e9:.2f}B" if tvl_now >= 1e9 else f"${tvl_now/1e6:.0f}M"

        return {
            "score": round(score, 2),
            "tvl": tvl_fmt,
            "tvl_raw": tvl_now,
            "change_7d": round(change_7d, 1),
            "change_30d": round(change_30d, 1),
            "signal": signal,
        }

    except Exception as e:
        logger.debug(f"DefiLlama {ticker} : {e}")
        return {"score": 0.0, "tvl": None, "signal": "DefiLlama indisponible"}


# ─── Market Cap Rank ─────────────────────────────────────────────────────────

def get_market_rank_momentum(ticker: str) -> dict:
    """
    Évolution du rang market cap sur CoinGecko.
    Un actif qui monte dans le classement = force relative croissante.
    """
    coin_id = COINGECKO_IDS.get(ticker.upper())
    if not coin_id:
        return {"score": 0.0, "rank": None}

    try:
        resp = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": "30", "interval": "daily"},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        data = resp.json()
        prices = data.get("prices", [])
        market_caps = data.get("market_caps", [])

        if len(prices) < 7 or len(market_caps) < 7:
            return {"score": 0.0, "rank": None}

        # Vérifier si le MC croît plus vite que le prix (adoption organique)
        mc_now = market_caps[-1][1]
        mc_7d = market_caps[-7][1]
        price_now = prices[-1][1]
        price_7d = prices[-7][1]

        mc_change = (mc_now - mc_7d) / mc_7d * 100 if mc_7d > 0 else 0
        price_change = (price_now - price_7d) / price_7d * 100 if price_7d > 0 else 0

        # Si MC croît plus vite que le prix = nouvelles entrées (bullish)
        delta = mc_change - price_change
        score = 0.2 if delta > 5 else (-0.2 if delta < -5 else 0)

        return {
            "score": score,
            "mc_change_7d": round(mc_change, 1),
            "price_change_7d": round(price_change, 1),
            "mc_vs_price_delta": round(delta, 1),
        }

    except Exception as e:
        logger.debug(f"Market rank {ticker} : {e}")
        return {"score": 0.0, "rank": None}


# ─── Analyse on-chain complète ────────────────────────────────────────────────

def analyze(ticker: str) -> dict:
    """
    Analyse on-chain et macro complète.
    Score final : -1.0 à +1.0
    """
    logger.info(f"On-chain : {ticker}")

    cg = get_coingecko_metrics(ticker)
    tvl = get_defillama_tvl(ticker)
    rank = get_market_rank_momentum(ticker)

    time.sleep(1.5)  # Rate limit CoinGecko (free: 30 req/min)

    # Score composite : CoinGecko (50%) + TVL (30%) + Rank momentum (20%)
    score = (
        cg["score"] * 0.50
        + tvl["score"] * 0.30
        + rank["score"] * 0.20
    )
    score = round(max(-1.0, min(1.0, score)), 2)

    signals = []
    signals.extend(cg.get("signals", []))
    if tvl.get("signal") and tvl["signal"] != "N/A":
        signals.append(tvl["signal"])

    if score >= 0.5:
        verdict = "ON-CHAIN BULLISH"
    elif score >= 0.2:
        verdict = "ON-CHAIN LÉGÈREMENT POSITIF"
    elif score <= -0.5:
        verdict = "ON-CHAIN BEARISH"
    elif score <= -0.2:
        verdict = "ON-CHAIN LÉGÈREMENT NÉGATIF"
    else:
        verdict = "ON-CHAIN NEUTRE"

    return {
        "ticker": ticker,
        "score": score,
        "verdict": verdict,
        "signals": signals[:5],
        "coingecko": cg,
        "tvl": tvl,
        "rank_momentum": rank,
    }


def run(tickers: list[str]) -> dict[str, dict]:
    """Analyse on-chain de plusieurs actifs (respecte rate limits)."""
    results = {}
    for i, ticker in enumerate(tickers):
        results[ticker] = analyze(ticker)
        if i < len(tickers) - 1:
            time.sleep(2)  # CoinGecko free: 30 req/min
    return results
