"""
Scanner de marché — analyse les meilleures opportunités sur OKX
et déclenche des alertes Telegram uniquement sur signaux forts.
Validation fondamentale (news + Fear&Greed + BTC dominance) requise.

Univers scanné : top 50 paires USDT OKX + watchlist manuelle.
Seuil d'alerte : score composite >= 2.0 ou <= -2.0.
"""

import logging
import time
from datetime import datetime

import okx_client as okx
import technical_signals as ts
import news_sentiment as ns
import market_microstructure as mm
import onchain
import alertes
import config
import execution

logger = logging.getLogger(__name__)

# Seuil alerte Telegram (score composite 4 dimensions)
SIGNAL_THRESHOLD = 1.5      # Alerte envoyée
AUTO_EXECUTE_THRESHOLD = 1.5  # Ordre automatique placé

# Watchlist prioritaire (actifs à fort potentiel)
WATCHLIST_EXTRA = [
    "BTC", "ETH", "XRP", "SOL", "BNB",   # Top market cap — priorité absolue
    "INJ", "SUI", "TIA", "JTO",
    "ARB", "OP", "AVAX", "DOT",
    "LINK", "AAVE", "UNI", "NEAR", "APT",
    "ATOM", "DYDX", "SEI", "STX", "WIF",
    "PENDLE", "ENA", "EIGEN", "W",
]

# Actifs à exclure
EXCLUDE = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD",
    "WBTC", "WETH", "STETH", "BETH",
}

# Cooldown anti-doublon (ticker -> timestamp dernier signal)
_alerted_cache: dict[str, float] = {}
ALERT_COOLDOWN_HOURS = 6


def get_top_volume_tickers(n: int = 50) -> list[str]:
    """Retourne les N tickers USDT avec le plus grand volume 24h sur OKX."""
    try:
        pairs = okx.get_available_pairs()
        # On prend les N premiers (OKX les retourne triés par popularité approximative)
        tickers = [t for t in pairs if t not in EXCLUDE][:n]
        return tickers
    except Exception as e:
        logger.error(f"Erreur get_available_pairs OKX : {e}")
        return []


def is_cooldown_active(ticker: str) -> bool:
    last = _alerted_cache.get(ticker, 0)
    return (time.time() - last) < (ALERT_COOLDOWN_HOURS * 3600)


def compute_final_score(
    score_tech: float,
    score_news: float,
    score_ms: float,
    score_oc: float,
) -> float:
    """
    Score composite final pondéré — 4 dimensions.
    Technique (50%) + News/Fondamental (20%) + Microstructure (20%) + On-Chain (10%)
    Résultat clampé à [-3.0, +3.0]
    """
    score = (
        score_tech * 0.50
        + score_news * 0.20
        + score_ms  * 0.20
        + score_oc  * 0.10
    )
    return round(max(-3.0, min(3.0, score)), 2)


def build_signal_payload(
    ticker: str,
    tech: dict,
    fundamental: dict,
    microstructure: dict,
    onchain_data: dict,
    portfolio_value: float,
    max_pct: float = 0.20,
) -> dict:
    """Construit le payload complet (4 dimensions d'analyse) pour un signal."""
    sig = tech.get("signal", {})
    score_tech = sig.get("score", 0)
    score_news = fundamental.get("score_global", 0)
    score_ms = microstructure.get("score", 0)
    score_oc = onchain_data.get("score", 0)

    prix = tech.get("prix_actuel", 0)
    stop = tech.get("stop_proche")
    target = tech.get("target_proche")
    atr = tech.get("atr_14")

    if not stop and atr:
        stop = prix - atr * 2
    if not target and stop:
        risk = abs(prix - stop)
        target = prix + risk * 2

    rr = abs(target - prix) / abs(prix - stop) if (stop and target and stop != prix) else 2.0

    taille_usd = portfolio_value * max_pct
    score_final = compute_final_score(score_tech, score_news, score_ms, score_oc)

    # Assemblage des raisons depuis toutes les sources
    raisons = list(sig.get("signaux", []))[:5]
    ms_signals = microstructure.get("signals", [])
    oc_signals = onchain_data.get("signals", [])

    # Indicateurs clés pour l'alerte
    ichi = tech.get("ichimoku", {})
    fib = tech.get("fibonacci", {})

    return {
        "ticker": ticker,
        "score": score_final,
        "score_tech": score_tech,
        "score_news": score_news,
        "score_ms": score_ms,
        "score_oc": score_oc,
        "verdict": sig.get("verdict", ""),
        "verdict_news": fundamental.get("verdict", ""),
        "verdict_ms": microstructure.get("verdict", ""),
        "verdict_oc": onchain_data.get("verdict", ""),
        "prix": prix,
        "stop": round(stop, 6) if stop else None,
        "target": round(target, 6) if target else None,
        "rr": round(rr, 1),
        "taille_usd": taille_usd,
        "raisons": raisons,
        "ms_signals": ms_signals,
        "oc_signals": oc_signals,
        "news_signals": fundamental.get("news_signals", []),
        "fear_greed": fundamental.get("fear_greed", {}),
        "ichimoku": ichi,
        "fibonacci": fib,
        "funding_rate": microstructure.get("funding", {}).get("rate"),
        "trade_autorise": fundamental.get("trade_autorise", False),
        "source": "OKX Spot",
    }


def run_scan(portfolio_value: float) -> list[dict]:
    """
    Lance le scan complet OKX avec validation fondamentale.
    Retourne les signaux actionnables et envoie les alertes Telegram.
    """
    logger.info("Démarrage du scan de marché OKX...")

    # Univers : top 50 OKX + watchlist
    top_tickers = get_top_volume_tickers(n=50)
    universe = list(set(top_tickers + WATCHLIST_EXTRA) - EXCLUDE)
    logger.info(f"Univers scanné : {len(universe)} actifs")

    # OHLCV depuis OKX
    ohlcv_data = okx.get_all_ohlcv(universe, days=90)
    logger.info(f"OHLCV chargé : {len(ohlcv_data)} actifs")

    # Analyse technique
    tech_results = ts.run(ohlcv_data)

    # Pré-filtre : seuil technique abaissé à 1.5 car les autres modules
    # peuvent compenser (score final pondéré 4 dimensions)
    candidates = {
        ticker: tech for ticker, tech in tech_results.items()
        if "erreur" not in tech
        and abs(tech.get("signal", {}).get("score", 0)) >= 1.5
        and not is_cooldown_active(ticker)
    }
    logger.info(f"Candidats après pré-filtre technique : {len(candidates)}")

    # Analyse complète uniquement sur les candidats (économie d'API calls)
    actionable = []
    for ticker, tech in candidates.items():
        score_tech = tech.get("signal", {}).get("score", 0)

        # 1. News + Fear&Greed + BTC dominance
        fundamental = ns.analyze(ticker)

        # 2. Microstructure (funding, L/S, taker vol)
        microstructure = mm.analyze(ticker)

        # 3. On-chain (CoinGecko, DefiLlama TVL)
        onchain_data = onchain.analyze(ticker)

        score_final = compute_final_score(score_tech, fundamental["score_global"],
                                          microstructure["score"], onchain_data["score"])

        logger.info(
            f"{ticker} | Tech:{score_tech:+.2f} News:{fundamental['score_global']:+.2f} "
            f"MS:{microstructure['score']:+.2f} OC:{onchain_data['score']:+.2f} "
            f"→ FINAL:{score_final:+.2f}"
        )

        # Blocage si fondamentaux franchement négatifs (score < -0.5)
        if fundamental["score_global"] < -0.5:
            alertes.send(
                f"⏸ *{ticker}* tech {score_tech:+.1f} bloqué\n"
                f"Fondamentaux : {fundamental['verdict']} ({fundamental['score_global']:+.2f})"
            )
            continue

        # Le score final doit dépasser le seuil d'alerte
        if abs(score_final) < SIGNAL_THRESHOLD:
            logger.info(f"{ticker} : score {score_final:+.2f} < {SIGNAL_THRESHOLD} — ignoré")
            continue

        payload = build_signal_payload(
            ticker, tech, fundamental, microstructure, onchain_data, portfolio_value
        )
        actionable.append(payload)

        # Alerte Telegram enrichie (4 dimensions)
        alertes.alerte_opportunite_enrichie(payload)
        _alerted_cache[ticker] = time.time()
        logger.info(f"Alerte envoyée : {ticker} score_final={score_final:+.2f}")

        # Exécution autonome uniquement si score très fort ET fondamentaux OK
        if score_final >= AUTO_EXECUTE_THRESHOLD and fundamental["trade_autorise"]:
            execution.execute_signal(payload, portfolio_value)

        time.sleep(1)  # anti-flood Telegram + API

    if not actionable:
        logger.info("Aucun signal actionnable détecté.")
    else:
        logger.info(f"{len(actionable)} signal(s) envoyé(s)")

    return actionable


def run_once(portfolio_value: float = None):
    """Point d'entrée pour un scan manuel."""
    if portfolio_value is None:
        try:
            balances = okx.get_balances()
            usdc = balances.get("USDC", 0) + balances.get("USDT", 0)
            portfolio_value = usdc if usdc > 0 else 718.0
        except Exception:
            portfolio_value = 718.0

    signals = run_scan(portfolio_value)

    print(f"\n{'━'*55}")
    print(f"  Scan OKX terminé — {len(signals)} signal(s) actionnable(s)")
    if signals:
        for s in signals:
            print(f"  {s['ticker']:10} score={s['score']:+.2f}  {s['verdict']:25} news={s['score_news']:+.2f}")
    print(f"{'━'*55}\n")
    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    run_once()
