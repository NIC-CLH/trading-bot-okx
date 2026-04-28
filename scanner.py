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

import pandas as pd

import okx_client as okx
import technical_signals as ts
import news_sentiment as ns
import market_microstructure as mm
import onchain
import coinglass_data as cg
import macro_context as macro
import regime_detector as rd
import volume_profile as vp
import ml_scorer as ml
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
    score_cg: float = 0.0,
    score_macro: float = 0.0,
) -> float:
    """
    Score composite final pondéré — 6 dimensions.
    Technique (40%) + News (15%) + Microstructure (15%) + On-Chain (10%)
    + Coinglass/Liquidations (10%) + Macro DXY/DVOL (10%)
    Résultat clampé à [-3.0, +3.0]
    """
    score = (
        score_tech  * 0.40
        + score_news  * 0.15
        + score_ms    * 0.15
        + score_oc    * 0.10
        + score_cg    * 0.10
        + score_macro * 0.10
    )
    return round(max(-3.0, min(3.0, score)), 2)


def build_signal_payload(
    ticker: str,
    tech: dict,
    fundamental: dict,
    microstructure: dict,
    onchain_data: dict,
    coinglass: dict,
    macro: dict,
    portfolio_value: float,
    max_pct: float = 0.20,
) -> dict:
    """Construit le payload complet (6 dimensions d'analyse) pour un signal."""
    sig = tech.get("signal", {})
    score_tech = sig.get("score", 0)
    score_news = fundamental.get("score_global", 0)
    score_ms = microstructure.get("score", 0)
    score_oc = onchain_data.get("score", 0)
    score_cg = coinglass.get("score", 0)
    score_macro = macro.get("score", 0)

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
    score_final = compute_final_score(
        score_tech, score_news, score_ms, score_oc, score_cg, score_macro
    )

    # Assemblage des raisons depuis toutes les sources
    raisons = list(sig.get("signaux", []))[:4]
    ms_signals = microstructure.get("signals", [])
    oc_signals = onchain_data.get("signals", [])
    cg_signals = coinglass.get("signals", [])
    macro_signals = macro.get("signals", [])

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
        "score_cg": score_cg,
        "score_macro": score_macro,
        "verdict": sig.get("verdict", ""),
        "verdict_news": fundamental.get("verdict", ""),
        "verdict_ms": microstructure.get("verdict", ""),
        "verdict_oc": onchain_data.get("verdict", ""),
        "verdict_cg": coinglass.get("verdict", ""),
        "verdict_macro": macro.get("verdict", ""),
        "prix": prix,
        "stop": round(stop, 6) if stop else None,
        "target": round(target, 6) if target else None,
        "rr": round(rr, 1),
        "taille_usd": taille_usd,
        "raisons": raisons,
        "ms_signals": ms_signals,
        "oc_signals": oc_signals,
        "cg_signals": cg_signals,
        "macro_signals": macro_signals,
        "news_signals": fundamental.get("news_signals", []),
        "fear_greed": fundamental.get("fear_greed", {}),
        "ichimoku": ichi,
        "fibonacci": fib,
        "funding_rate": microstructure.get("funding", {}).get("rate"),
        "dvol": macro.get("dvol"),
        "dxy": macro.get("dxy"),
        "ls_ratio": coinglass.get("ls_ratio"),
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

    # Contexte global — calculé UNE SEULE FOIS pour tout le cycle
    macro_data = macro.analyze()
    logger.info(f"Macro : {macro_data['verdict']} (score {macro_data['score']:+.2f})")

    ml_status = ml.get_model_status()
    logger.info(f"ML : {'actif' if ml_status['model_trained'] else f'en collecte ({ml_status[\"n_labeled_trades\"]}/{50} trades)'}")

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

        # 4. Liquidations + OI + L/S ratio (Coinglass)
        coinglass_data = cg.analyze(ticker)

        # 5. Macro context (DXY + DVOL + Google Trends) — ticker-specific pour Trends
        macro_ticker = macro.analyze(ticker)

        # 6. Régime de marché (HMM + GARCH + ruptures)
        df_ticker = ohlcv_data.get(ticker, pd.DataFrame())
        regime_data = rd.analyze(df_ticker)

        # 7. Volume Profile (POC, VAH, VAL, HVN/LVN)
        vp_data = vp.analyze(df_ticker)

        score_final = compute_final_score(
            score_tech,
            fundamental["score_global"],
            microstructure["score"],
            onchain_data["score"],
            coinglass_data["score"],
            macro_ticker["score"],
        )

        # Appliquer le multiplicateur de régime sur le score final
        position_multiplier = regime_data.get("position_multiplier", 1.0)

        # Score ML si disponible (remplace le score manuel après 50 trades)
        temp_payload = {
            "score": score_final, "score_tech": score_tech,
            "score_news": fundamental["score_global"],
            "score_ms": microstructure["score"], "score_oc": onchain_data["score"],
            "score_cg": coinglass_data["score"], "score_macro": macro_ticker["score"],
            "dxy": macro_ticker.get("dxy", 104), "dvol": macro_ticker.get("dvol", 65),
            "ls_ratio": coinglass_data.get("ls_ratio", 1.0),
            "oi_change_4h_pct": coinglass_data.get("oi_change_4h_pct", 0),
        }
        ml_result = ml.predict_score(temp_payload, regime_data, vp_data)
        if ml_result["ml_active"]:
            score_final = ml_result["score_ml"]

        logger.info(
            f"{ticker} | Tech:{score_tech:+.2f} News:{fundamental['score_global']:+.2f} "
            f"MS:{microstructure['score']:+.2f} OC:{onchain_data['score']:+.2f} "
            f"CG:{coinglass_data['score']:+.2f} Macro:{macro_ticker['score']:+.2f} "
            f"Régime:{regime_data['regime']} Vol:{regime_data['vol_regime']} "
            f"VP:{vp_data['score']:+.2f} PosMult:{position_multiplier} "
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
            ticker, tech, fundamental, microstructure, onchain_data,
            coinglass_data, macro_ticker, portfolio_value
        )
        # Enrichir le payload avec régime, VP, ML
        payload["regime"] = regime_data.get("regime", "sideways")
        payload["regime_context"] = regime_data.get("regime_context", "")
        payload["position_multiplier"] = position_multiplier
        payload["vol_regime"] = regime_data.get("vol_regime", "normal")
        payload["vol_annualized"] = regime_data.get("vol_annualized", 80)
        payload["vp_poc"] = vp_data.get("poc")
        payload["vp_vah"] = vp_data.get("vah")
        payload["vp_val"] = vp_data.get("val")
        payload["vp_score"] = vp_data.get("score", 0)
        payload["ml_active"] = ml_result.get("ml_active", False)
        payload["ml_confidence"] = ml_result.get("ml_confidence", 0)
        payload["score"] = score_final  # Score final (ML ou manuel)

        # Enregistrer pour l'entraînement ML futur
        ml.save_signal_for_training(payload, regime_data, vp_data)

        actionable.append(payload)

        # Alerte Telegram enrichie
        alertes.alerte_opportunite_enrichie(payload)
        _alerted_cache[ticker] = time.time()
        logger.info(f"Alerte envoyée : {ticker} score_final={score_final:+.2f}")

        # Exécution autonome — taille ajustée par position_multiplier
        if score_final >= AUTO_EXECUTE_THRESHOLD and fundamental["trade_autorise"]:
            # Réduire la taille si volatilité élevée ou régime incertain
            payload["taille_usd"] = payload["taille_usd"] * position_multiplier
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
