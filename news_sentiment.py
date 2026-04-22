"""
Module News & Sentiment — enrichit les signaux techniques avec :
- Actualités récentes par actif (RSS CoinDesk, Cointelegraph)
- Fear & Greed Index
- BTC Dominance
- Score fondamental : -2 (très négatif) à +2 (très positif)

Un signal technique ne part en ordre que si le score fondamental >= 0.
"""

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

# Sources RSS crypto
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

# Mots-clés négatifs et positifs pour scoring
NEGATIVE_KEYWORDS = [
    "hack", "exploit", "scam", "rug", "sec", "lawsuit", "ban", "crash",
    "dump", "sell", "bearish", "fear", "warning", "risk", "fraud",
    "investigation", "shutdown", "delist", "attack", "vulnerability",
    "ponzi", "collapse", "bankrupt", "insolvent", "freeze",
]

POSITIVE_KEYWORDS = [
    "partnership", "launch", "upgrade", "adoption", "bullish", "surge",
    "all-time", "record", "integration", "institutional", "etf", "approval",
    "mainnet", "milestone", "growth", "expansion", "invest", "fund",
    "listing", "rally", "breakout", "accumulate", "whale", "tokenization",
]


# ─── RSS News ────────────────────────────────────────────────────────────────

def fetch_rss_news(ticker: str, max_age_hours: int = 48) -> list[dict]:
    """Récupère les news récentes mentionnant le ticker depuis les flux RSS."""
    ticker_lower = ticker.lower()
    # Noms alternatifs communs
    aliases = {
        "link": ["chainlink", "link"],
        "tia": ["celestia", "tia"],
        "avax": ["avalanche", "avax"],
        "inj": ["injective", "inj"],
        "sol": ["solana", "sol"],
        "dot": ["polkadot", "dot"],
        "near": ["near protocol", "near"],
        "aave": ["aave"],
        "uni": ["uniswap", "uni"],
        "arb": ["arbitrum", "arb"],
        "jto": ["jito", "jto"],
    }
    search_terms = aliases.get(ticker_lower, [ticker_lower, ticker.upper()])
    # Tickers courts (<=4 chars) : word boundary pour éviter faux positifs
    # ex: "link" ne doit pas matcher "linked", "blockchain", etc.
    def matches_ticker(text: str, terms: list) -> bool:
        for term in terms:
            if len(term) <= 4:
                # Word boundary : \bterm\b
                if re.search(r'\b' + re.escape(term) + r'\b', text, re.IGNORECASE):
                    return True
            else:
                if term in text:
                    return True
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    articles = []
    for feed_url in RSS_FEEDS:
        try:
            resp = requests.get(feed_url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")

            for item in items:
                title = item.findtext("title", "").lower()
                description = item.findtext("description", "").lower()

                # Priorité au titre — la description est contexte secondaire
                if not matches_ticker(title, search_terms) and not matches_ticker(description, search_terms):
                    continue

                articles.append({
                    "title": item.findtext("title", ""),
                    "description": item.findtext("description", "")[:200],
                    "url": item.findtext("link", ""),
                    "source": feed_url.split("/")[2],
                })

            time.sleep(0.5)
        except Exception as e:
            logger.debug(f"RSS {feed_url} indisponible : {e}")

    return articles[:10]  # Max 10 articles


# ─── Score sentiment ─────────────────────────────────────────────────────────

def score_articles(articles: list[dict]) -> tuple[float, list[str]]:
    """
    Score de sentiment basé sur les mots-clés dans les titres.
    Retourne (score, liste des signaux détectés).
    """
    if not articles:
        return 0.0, ["Aucune news récente trouvée"]

    score = 0.0
    signals = []

    for article in articles:
        text = (article["title"] + " " + article["description"]).lower()

        pos_hits = [kw for kw in POSITIVE_KEYWORDS if kw in text]
        neg_hits = [kw for kw in NEGATIVE_KEYWORDS if kw in text]

        if pos_hits:
            score += 0.5 * len(pos_hits)
            signals.append(f"✅ {article['title'][:80]}")
        if neg_hits:
            score -= 0.7 * len(neg_hits)
            signals.append(f"⚠️ {article['title'][:80]} [{', '.join(neg_hits)}]")

    # Normalisation [-2, +2]
    score = max(-2.0, min(2.0, score))
    return round(score, 1), signals[:5]


# ─── Fear & Greed ────────────────────────────────────────────────────────────

def fetch_fear_greed() -> dict:
    """Fear & Greed Index (alternative.me)."""
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        data = resp.json()["data"][0]
        value = int(data["value"])
        label = data["value_classification"]

        # Score : extrême fear = opportunité d'achat, extrême greed = danger
        if value <= 20:
            fg_score = 1.0   # Extrême peur = opportunité
        elif value <= 40:
            fg_score = 0.5
        elif value <= 60:
            fg_score = 0.0
        elif value <= 80:
            fg_score = -0.5
        else:
            fg_score = -1.0  # Extrême cupidité = danger

        return {"value": value, "label": label, "score": fg_score}
    except Exception as e:
        logger.warning(f"Fear & Greed indisponible : {e}")
        return {"value": None, "label": "N/A", "score": 0.0}


# ─── BTC Dominance ───────────────────────────────────────────────────────────

def fetch_btc_dominance() -> dict:
    """BTC Dominance depuis CoinGecko (fallback alternative.me)."""
    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/global", timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        raw = resp.json()
        # CoinGecko peut retourner {"data": {...}} ou directement les données
        if "data" in raw:
            raw = raw["data"]
        dom = raw.get("market_cap_percentage", {}).get("btc", 50)
        dom = round(float(dom), 1)

        # Dominance élevée = altcoins sous-pression
        if dom > 60:
            dom_score = -0.5
        elif dom > 55:
            dom_score = 0.0
        elif dom > 50:
            dom_score = 0.5
        else:
            dom_score = 1.0  # Dominance faible = altseason favorable

        return {"dominance": dom, "score": dom_score}
    except Exception as e:
        logger.warning(f"BTC dominance indisponible : {e}")
        return {"dominance": None, "score": 0.0}


# ─── Analyse fondamentale complète ───────────────────────────────────────────

def analyze(ticker: str) -> dict:
    """
    Analyse fondamentale complète pour un ticker.
    Retourne un score global et les détails.
    """
    logger.info(f"Analyse fondamentale : {ticker}")

    # News
    articles = fetch_rss_news(ticker)
    news_score, news_signals = score_articles(articles)

    # Macro
    fg = fetch_fear_greed()
    btc_dom = fetch_btc_dominance()

    # Score global = news (60%) + Fear&Greed (20%) + BTC Dominance (20%)
    global_score = (news_score * 0.6) + (fg["score"] * 0.2) + (btc_dom["score"] * 0.2)
    global_score = round(max(-2.0, min(2.0, global_score)), 2)

    # Verdict
    if global_score >= 1.0:
        verdict = "FONDAMENTAUX FORTS"
    elif global_score >= 0.0:
        verdict = "FONDAMENTAUX NEUTRES"
    elif global_score >= -1.0:
        verdict = "FONDAMENTAUX FAIBLES"
    else:
        verdict = "FONDAMENTAUX NÉGATIFS"

    return {
        "ticker": ticker,
        "score_global": global_score,
        "verdict": verdict,
        "news_score": news_score,
        "news_count": len(articles),
        "news_signals": news_signals,
        "fear_greed": fg,
        "btc_dominance": btc_dom,
        "trade_autorise": global_score >= 0.0,
    }


def run(tickers: list[str]) -> dict[str, dict]:
    """Analyse fondamentale de tous les actifs."""
    results = {}
    for ticker in tickers:
        results[ticker] = analyze(ticker)
        time.sleep(1)
    return results
