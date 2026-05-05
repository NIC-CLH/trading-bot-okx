"""
Social Radar — sentiment social via CoinGecko (100% gratuit, sans clé API).

Sources :
  - CoinGecko /search/trending  : tokens en tendance right now
  - CoinGecko /coins/{id}       : données communauté (Twitter, Reddit, Telegram)
  - CoinGecko /coins/markets    : social + market momentum combinés

Métriques utilisées :
  trending_rank   : position dans les 7 tokens les plus vus sur CoinGecko
  community_score : followers Twitter + Reddit actifs + Telegram
  price_momentum  : variation 24h et 7j combinées

Score retourné : [-1.0, +1.0] intégré dans le score composite du scanner.

Point d'injection :
  - scanner.py Phase 1, calculé par ticker comme dimension sociale
  - Pas de clé API requise — rate limit CoinGecko : 10-30 req/min
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.coingecko.com/api/v3"
TIMEOUT  = 10

# Cache en mémoire : TTL 1h pour ne pas saturer le rate limit CoinGecko
_cache: dict[str, tuple[float, dict]] = {}
_trending_cache: tuple[float, list] = (0.0, [])
CACHE_TTL = 3600

# Map ticker OKX → id CoinGecko (les plus courants)
# CoinGecko utilise des ids lowercase ex: "bitcoin", "ethereum"
_TICKER_TO_ID: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "BNB": "binancecoin", "XRP": "ripple", "ADA": "cardano",
    "AVAX": "avalanche-2", "DOT": "polkadot", "MATIC": "matic-network",
    "LINK": "chainlink", "UNI": "uniswap", "ATOM": "cosmos",
    "LTC": "litecoin", "BCH": "bitcoin-cash", "NEAR": "near",
    "ARB": "arbitrum", "OP": "optimism", "SUI": "sui",
    "APT": "aptos", "INJ": "injective-protocol", "TIA": "celestia",
    "DOGE": "dogecoin", "SHIB": "shiba-inu", "PEPE": "pepe",
    "WLD": "worldcoin-wld", "AAVE": "aave", "MKR": "maker",
    "CRV": "curve-dao-token", "COMP": "compound-governance-token",
    "SNX": "havven", "LDO": "lido-dao", "FXS": "frax-share",
    "GMX": "gmx", "DYDX": "dydx", "PENDLE": "pendle",
    "JTO": "jito-governance-token", "PYTH": "pyth-network",
    "W": "wormhole", "STRK": "starknet", "ALT": "altlayer",
    "ZK": "zksync", "EIGEN": "eigenlayer", "ENA": "ethena",
    "ETHFI": "ether-fi", "AEVO": "aevo", "IO": "io-net",
    "KNC": "kyber-network-crystal", "ZEN": "zencash",
    "PNUT": "peanut-the-squirrel", "BIO": "bio-protocol",
    "ORDI": "ordinals", "ZRO": "layerzero",
}


def _get_coingecko_id(ticker: str) -> str | None:
    """Retourne l'id CoinGecko pour un ticker OKX."""
    return _TICKER_TO_ID.get(ticker.upper())


def _get_trending() -> list[str]:
    """
    Retourne les tickers en tendance sur CoinGecko (top 7 toutes les 10 min).
    1 seul appel API, résultat mis en cache 1h.
    """
    global _trending_cache
    ts, data = _trending_cache
    if time.time() - ts < CACHE_TTL:
        return data

    try:
        resp = requests.get(f"{BASE_URL}/search/trending", timeout=TIMEOUT)
        resp.raise_for_status()
        coins   = resp.json().get("coins", [])
        tickers = [c["item"]["symbol"].upper() for c in coins]
        _trending_cache = (time.time(), tickers)
        logger.debug(f"[SocialRadar] Trending : {tickers}")
        return tickers
    except Exception as e:
        logger.debug(f"[SocialRadar] trending error : {e}")
        return []


def _get_community_data(ticker: str) -> dict:
    """
    Données communauté CoinGecko pour un ticker.
    Mis en cache 1h — 1 appel par ticker par cycle max.
    """
    now = time.time()
    if ticker in _cache:
        ts, data = _cache[ticker]
        if now - ts < CACHE_TTL:
            return data

    cg_id = _get_coingecko_id(ticker)
    if not cg_id:
        return {}

    try:
        resp = requests.get(
            f"{BASE_URL}/coins/{cg_id}",
            params={
                "localization":   "false",
                "tickers":        "false",
                "market_data":    "true",
                "community_data": "true",
                "developer_data": "false",
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        raw  = resp.json()
        data = {
            "community":   raw.get("community_data", {}),
            "market_data": raw.get("market_data", {}),
            "scores": {
                "community_score":  raw.get("community_score"),
                "liquidity_score":  raw.get("liquidity_score"),
                "public_interest_score": raw.get("public_interest_score"),
            },
        }
        _cache[ticker] = (now, data)
        return data
    except Exception as e:
        logger.debug(f"[SocialRadar] {ticker} community error : {e}")
        return {}


def _compute_score(ticker: str, community_data: dict, trending: list[str]) -> float:
    """
    Score social [-1.0, +1.0] basé sur :
      1. Trending bonus  : +0.4 si dans top 7 CoinGecko
      2. Community score : 0-100 → normalisé
      3. Reddit activité : comptes actifs 48h
      4. Price momentum  : variation 7j (momentum confirme le social)
    """
    score = 0.0

    # 1. Trending bonus — le signal social le plus fort
    if ticker.upper() in trending:
        score += 0.40
        logger.debug(f"[SocialRadar] {ticker} en trending CoinGecko +0.40")

    if not community_data:
        return round(max(-1.0, min(1.0, score)), 3)

    community = community_data.get("community", {})
    scores    = community_data.get("scores", {})
    mkt       = community_data.get("market_data", {})

    # 2. Community score CoinGecko (0-100)
    cs = scores.get("community_score")
    if cs is not None:
        score += (cs - 50) / 200.0   # ±0.25

    # 3. Reddit actifs 48h (proxy de l'engagement récent)
    reddit_active = community.get("reddit_accounts_active_48h") or 0
    if reddit_active > 5000:
        score += 0.10
    elif reddit_active > 1000:
        score += 0.05

    # 4. Momentum prix 7j (le social et le prix se confirment mutuellement)
    chg_7d = mkt.get("price_change_percentage_7d")
    if chg_7d is not None:
        if chg_7d > 20:
            score += 0.15
        elif chg_7d > 10:
            score += 0.08
        elif chg_7d < -20:
            score -= 0.15
        elif chg_7d < -10:
            score -= 0.08

    return round(max(-1.0, min(1.0, score)), 3)


# ── API publique ───────────────────────────────────────────────────────────────

def analyze(ticker: str) -> dict:
    """
    Retourne les métriques sociales et le score pour un ticker.
    Toujours un dict valide — jamais bloquant, sans clé API requise.
    """
    _default = {
        "score":          0.0,
        "trending":       False,
        "community_score": None,
        "reddit_active":  None,
        "telegram_users": None,
        "verdict":        "données sociales indisponibles",
    }

    try:
        trending       = _get_trending()
        community_data = _get_community_data(ticker)
        score          = _compute_score(ticker, community_data, trending)
        is_trending    = ticker.upper() in trending

        community = community_data.get("community", {})
        scores    = community_data.get("scores", {})

        reddit_active  = community.get("reddit_accounts_active_48h")
        telegram_users = community.get("telegram_channel_user_count")
        cs             = scores.get("community_score")

        if is_trending:
            verdict = f"En tendance CoinGecko (top 7) — score social {score:+.2f}"
        elif score >= 0.3:
            verdict = f"Communauté active (CS={cs}, Reddit={reddit_active})"
        elif score <= -0.3:
            verdict = f"Intérêt social faible (CS={cs})"
        else:
            verdict = f"Neutre (CS={cs}, Telegram={telegram_users})"

        logger.info(
            f"[SocialRadar] {ticker} : score={score:+.2f} | "
            f"trending={is_trending} | CS={cs} | "
            f"Reddit_48h={reddit_active} | {verdict[:60]}"
        )

        return {
            "score":           score,
            "trending":        is_trending,
            "community_score": cs,
            "reddit_active":   reddit_active,
            "telegram_users":  telegram_users,
            "verdict":         verdict,
        }

    except Exception as e:
        logger.debug(f"[SocialRadar] {ticker} : {e}")
        return _default


def get_trending(limit: int = 10) -> list[dict]:
    """
    Retourne les tokens en tendance sur CoinGecko right now.
    Utile pour découvrir des opportunités hors watchlist.
    """
    try:
        resp = requests.get(f"{BASE_URL}/search/trending", timeout=TIMEOUT)
        resp.raise_for_status()
        coins = resp.json().get("coins", [])[:limit]
        return [
            {
                "ticker":        c["item"]["symbol"].upper(),
                "name":          c["item"]["name"],
                "trending_rank": c["item"]["score"],
                "market_cap_rank": c["item"].get("market_cap_rank"),
                "score":         0.40,   # trending = signal fort par défaut
            }
            for c in coins
        ]
    except Exception as e:
        logger.debug(f"[SocialRadar] get_trending error : {e}")
        return []
