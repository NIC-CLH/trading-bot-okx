"""
Module News & Sentiment — enrichit les signaux techniques avec :
- Actualités récentes par actif (RSS CoinDesk, Cointelegraph, Decrypt)
- Finnhub Crypto News (NLP pro — 60 req/min gratuit)
- CryptoPanic sentiment agrégé
- Fear & Greed Index
- BTC Dominance
- Score fondamental : -2 (très négatif) à +2 (très positif)

Un signal technique ne part en ordre que si le score fondamental >= 0.

Variables d'environnement requises :
  FINNHUB_API_KEY  — https://finnhub.io (gratuit, 60 req/min)
"""

import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")

# Alerte visible au démarrage si des sources news sont mortes (clés absentes).
# Sans ça, le score news tombe à ~0 silencieusement et le filtre narrative est aveugle.
if not FINNHUB_API_KEY:
    logger.warning("FINNHUB_API_KEY absente — source Finnhub désactivée (score news dégradé)")
# CryptoPanic : offre gratuite supprimée (avril 2026) — source abandonnée volontairement.
# Les poids rebasculent sur RSS 50% + Finnhub 25% (cas `elif has_finnhub` plus bas).
if not os.getenv("CRYPTOPANIC_TOKEN", ""):
    logger.info("CryptoPanic non configuré (API devenue payante) — RSS + Finnhub seulement")

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


# ─── Finnhub Crypto News + Sentiment ────────────────────────────────────────

# Noms complets des tokens pour matcher les titres d'articles
# (le feed crypto général ne tague pas par symbole — on filtre par mention)
_TOKEN_NAMES = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "XRP": ["xrp", "ripple"],
    "SOL": ["solana"], "BNB": ["bnb", "binance coin"], "ADA": ["cardano"],
    "AVAX": ["avalanche"], "DOT": ["polkadot"], "LINK": ["chainlink"],
    "UNI": ["uniswap"], "AAVE": ["aave"], "NEAR": ["near protocol"],
    "ARB": ["arbitrum"], "OP": ["optimism"], "INJ": ["injective"],
    "SUI": ["sui"], "APT": ["aptos"], "ATOM": ["cosmos"], "DYDX": ["dydx"],
    "DOGE": ["dogecoin"], "LTC": ["litecoin"], "TRX": ["tron"],
    "ICP": ["internet computer"], "JTO": ["jito"], "ONDO": ["ondo"],
    "WLD": ["worldcoin"], "PENDLE": ["pendle"], "ZEC": ["zcash"],
}

# Cache du feed crypto général — 1 requête par scan au lieu de 55
_finnhub_feed_cache: dict = {"ts": 0.0, "articles": []}
_FINNHUB_FEED_TTL = 900  # 15 min


def _get_finnhub_crypto_feed() -> list[dict]:
    """Feed crypto général Finnhub (endpoint /news, le seul qui marche pour les cryptos)."""
    now = time.time()
    if now - _finnhub_feed_cache["ts"] < _FINNHUB_FEED_TTL and _finnhub_feed_cache["articles"]:
        return _finnhub_feed_cache["articles"]
    resp = requests.get(
        "https://finnhub.io/api/v1/news",
        params={"category": "crypto", "token": FINNHUB_API_KEY},
        timeout=8,
    )
    if resp.status_code != 200:
        logger.debug(f"Finnhub feed crypto : HTTP {resp.status_code}")
        return _finnhub_feed_cache["articles"]
    articles = resp.json()
    if isinstance(articles, list) and articles:
        _finnhub_feed_cache["ts"] = now
        _finnhub_feed_cache["articles"] = articles
    return _finnhub_feed_cache["articles"]


def fetch_finnhub_news(ticker: str, max_age_hours: int = 24) -> tuple[float, list[str]]:
    """
    Score news Finnhub pour un ticker, à partir du feed crypto général.

    L'endpoint company-news ne renvoie rien pour les paires crypto (réservé
    aux actions) — on filtre le feed général par mention du token à la place.

    Returns : (score -1.0 à +1.0, liste de signaux)
    """
    if not FINNHUB_API_KEY:
        return 0.0, []

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    try:
        articles = _get_finnhub_crypto_feed()
        if not articles:
            return 0.0, []

        terms = _TOKEN_NAMES.get(ticker.upper(), []) + [ticker.upper()]
        recent = []
        for a in articles:
            ts_article = a.get("datetime", 0)
            if not ts_article or ts_article < cutoff.timestamp():
                continue
            text = f"{a.get('headline', '')} {a.get('summary', '')}"
            for term in terms:
                if re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE):
                    recent.append(a)
                    break

        if not recent:
            return 0.0, []

        # Score basé sur sentiment NLP via mots-clés (Finnhub ne donne pas le score direct sur le plan gratuit)
        score, signals = score_articles([{
            "title": a.get("headline", ""),
            "description": a.get("summary", "")[:200],
        } for a in recent[:8]])

        # Normaliser à [-1.0, +1.0]
        score_normalized = max(-1.0, min(1.0, score / 2.0))
        if recent:
            signals.insert(0, f"Finnhub: {len(recent)} articles ({max_age_hours}h)")

        logger.debug(f"Finnhub {ticker} : {len(recent)} articles, score={score_normalized:+.2f}")
        return score_normalized, signals

    except Exception as e:
        logger.debug(f"Finnhub {ticker} : {e}")
        return 0.0, []


def fetch_cryptopanic_sentiment(ticker: str) -> tuple[float, list[str]]:
    """
    CryptoPanic — agrège les nouvelles crypto avec votes communautaires.
    API publique gratuite sans clé (rate limit léger).

    Returns : (score -1.0 à +1.0, signaux)
    """
    try:
        resp = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={
                "auth_token": os.getenv("CRYPTOPANIC_TOKEN", ""),
                "currencies": ticker.upper(),
                "filter": "hot",
                "public": "true",
            },
            timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return 0.0, []

        data = resp.json()
        results = data.get("results", [])[:10]
        if not results:
            return 0.0, []

        score = 0.0
        signals = []

        for post in results:
            sentiment = post.get("votes", {})
            bullish = sentiment.get("bullish", 0) or 0
            bearish = sentiment.get("bearish", 0) or 0
            important = sentiment.get("important", 0) or 0
            total = bullish + bearish + 1  # Éviter division par zéro

            if total > 5:  # Seulement si suffisamment de votes
                ratio = (bullish - bearish) / total
                weight = min(1.0, (bullish + bearish) / 20)  # Plus de votes = plus de poids
                score += ratio * weight

                if ratio > 0.3 and important > 2:
                    signals.append(f"🔺 CryptoPanic hot: {post.get('title', '')[:70]}")
                elif ratio < -0.3 and important > 2:
                    signals.append(f"🔻 CryptoPanic bearish: {post.get('title', '')[:70]}")

        score = round(max(-1.0, min(1.0, score)), 2)
        return score, signals[:3]

    except Exception as e:
        logger.debug(f"CryptoPanic {ticker} : {e}")
        return 0.0, []


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

    Sources :
    - RSS (CoinDesk, Cointelegraph, Decrypt) — 40%
    - Finnhub NLP pro — 20% (si FINNHUB_API_KEY disponible)
    - CryptoPanic sentiment communautaire — 10% (si CRYPTOPANIC_TOKEN disponible)
    - Fear & Greed Index — 15%
    - BTC Dominance — 15%

    Retourne un score global et les détails.
    """
    logger.info(f"Analyse fondamentale : {ticker}")

    # 1. RSS classique
    articles = fetch_rss_news(ticker)
    rss_score, rss_signals = score_articles(articles)

    # 2. Finnhub NLP
    finnhub_score, finnhub_signals = fetch_finnhub_news(ticker)
    has_finnhub = FINNHUB_API_KEY != ""

    # 3. CryptoPanic
    cp_score, cp_signals = fetch_cryptopanic_sentiment(ticker)
    has_cryptopanic = bool(os.getenv("CRYPTOPANIC_TOKEN", ""))

    # 4. Fear & Greed
    fg = fetch_fear_greed()

    # 5. BTC Dominance
    btc_dom = fetch_btc_dominance()

    # Pondération dynamique selon les sources disponibles
    if has_finnhub and has_cryptopanic:
        # Toutes sources disponibles
        news_combined = (rss_score * 0.40 + finnhub_score * 0.20 + cp_score * 0.10)
        fg_weight = 0.15
        dom_weight = 0.15
    elif has_finnhub:
        news_combined = (rss_score * 0.50 + finnhub_score * 0.25)
        fg_weight = 0.15
        dom_weight = 0.10
    elif has_cryptopanic:
        news_combined = (rss_score * 0.50 + cp_score * 0.15)
        fg_weight = 0.20
        dom_weight = 0.15
    else:
        # Fallback RSS seul
        news_combined = rss_score * 0.60
        fg_weight = 0.20
        dom_weight = 0.20

    global_score = news_combined + fg["score"] * fg_weight + btc_dom["score"] * dom_weight
    global_score = round(max(-2.0, min(2.0, global_score)), 2)

    # Signaux agrégés
    all_signals = rss_signals[:3]
    if finnhub_signals:
        all_signals.extend(finnhub_signals[:2])
    if cp_signals:
        all_signals.extend(cp_signals[:1])

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
        "news_score": rss_score,
        "finnhub_score": finnhub_score,
        "cryptopanic_score": cp_score,
        "news_count": len(articles),
        "news_signals": all_signals,
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
