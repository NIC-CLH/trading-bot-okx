"""
Scanner de marché — analyse TOUTES les opportunités disponibles sur OKX EEA.

Univers scanné : toutes les paires USDC avec volume > 500k$/jour.
Triées par volume décroissant — les plus liquides analysées en premier.
Seuil d'alerte : score composite >= 1.5.
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
import capital_allocator as ca
import correlation_guard
import relative_strength as rs_mod
import stablecoin_dominance as sd_mod
import token_unlocks
import news_interpreter
import regime_context
import social_radar

logger = logging.getLogger(__name__)

# Seuil alerte Telegram (score composite 4 dimensions)
SIGNAL_THRESHOLD = 1.5      # Alerte envoyée
AUTO_EXECUTE_THRESHOLD = 1.5  # Ordre automatique placé

# Rotation et sizing délégués à capital_allocator.py
# Les seuils ROTATION_SCORE_MIN (2.0) et ROTATION_USDC_RATIO (60%) sont définis là-bas.

# Actifs à exclure (stablecoins + wrapped tokens)
EXCLUDE = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP",
    "WBTC", "WETH", "STETH", "BETH", "BBTC",
}

# Volume minimum 24h en USDC pour être scanné
# 500k$ = token suffisamment liquide pour entrer/sortir sans slippage
MIN_VOLUME_USDC = 500_000

# Cooldown anti-doublon (ticker -> timestamp dernier signal)
_alerted_cache: dict[str, float] = {}
ALERT_COOLDOWN_HOURS = 6


def get_universe() -> list[str]:
    """
    Retourne TOUS les actifs disponibles sur OKX EEA avec volume suffisant,
    triés par volume décroissant (les plus importants analysés en premier).
    Pas de limite arbitraire — on scanne tout ce qui est disponible.
    """
    try:
        pairs = okx.get_available_pairs(min_volume_usdc=MIN_VOLUME_USDC)
        universe = [t for t in pairs if t not in EXCLUDE]
        logger.info(f"Univers OKX EEA : {len(universe)} actifs (volume > ${MIN_VOLUME_USDC/1e6:.1f}M/j)")
        return universe
    except Exception as e:
        logger.error(f"Erreur get_universe : {e}")
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
) -> dict:
    """
    Construit le payload complet (6 dimensions d'analyse) pour un signal.
    La taille de position N'est PAS calculée ici — c'est capital_allocator
    qui en est responsable en Phase 3, après tri par score.
    """
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
        stop = max(prix - atr * 2, prix * 0.001)  # plancher : jamais négatif
    if not target and stop:
        risk = abs(prix - stop)
        target = prix + risk * 2

    rr = abs(target - prix) / abs(prix - stop) if (stop and target and stop != prix) else 2.0

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


# _try_rotation supprimée — remplacée par capital_allocator.execute_rotation()


def run_scan(portfolio_value: float) -> list[dict]:
    """
    Lance le scan complet OKX avec validation fondamentale.
    Retourne les signaux actionnables et envoie les alertes Telegram.
    """
    logger.info("Démarrage du scan de marché OKX...")

    # ── Filtre BTC 50MA : n'acheter que dans un marché haussier ──────────────
    from position_manager import is_btc_uptrend
    if not is_btc_uptrend():
        logger.info("BTC sous sa MA50 — marché baissier — scan annulé (pas d'achats)")
        try:
            alertes.send(
                "🚫 *Scan annulé — BTC en dessous de sa moyenne 50 jours*\n"
                "_Le bot n'achète pas en marché baissier. Il reprendra automatiquement "
                "quand BTC repassera au-dessus._"
            )
        except Exception:
            pass
        return []

    # Univers complet : tout ce qui est disponible sur OKX EEA
    universe = get_universe()
    if not universe:
        logger.error("Univers vide — abandon du scan")
        return []
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
    ml_info = "actif" if ml_status["model_trained"] else f"en collecte ({ml_status['n_labeled_trades']}/50 trades)"
    logger.info(f"ML : {ml_info}")

    # Dominance stablecoins — score macro global (calculé une fois)
    try:
        sd_data  = sd_mod.analyze()
        sd_score = sd_data.get("score", 0.0)
        logger.info(
            f"Stablecoin dominance : {sd_data.get('usdt_d', 0):.1f}% "
            f"(score {sd_score:+.2f}, {sd_data.get('verdict', '')})"
        )
    except Exception:
        sd_score = 0.0

    # Régime contextuel enrichi (LLM) — calculé une fois par cycle
    try:
        regime_ctx = regime_context.enrich(
            technical_regime={},   # sera enrichi après la boucle si besoin
            macro_data=macro_data,
            stablecoin_score=sd_score,
        )
        ctx_mult = regime_ctx.get("multiplicateur_adj", 1.0)
        logger.info(
            f"[RegimeCtx] {regime_ctx.get('regime_narratif')} | "
            f"biais={regime_ctx.get('biais')} | mult_adj={ctx_mult:.2f}"
        )
    except Exception:
        regime_ctx = {}
        ctx_mult   = 1.0

    # Positions ouvertes pour le garde de corrélation (snapshot avant la boucle)
    import position_manager as pm
    try:
        open_positions_cached = pm.get_open_positions()
    except Exception:
        open_positions_cached = []

    # ── Phase 1 : Analyser tous les candidats, collecter les payloads ────────
    # On NE exécute PAS encore — on veut d'abord connaître tous les scores
    # pour financer les meilleurs signaux en priorité.
    actionable = []
    for ticker, tech in candidates.items():
        # ── Chaque ticker est analysé indépendamment ─────────────────────────
        # Un module externe qui retourne {} ou lève une exception ne doit
        # pas tuer les autres tickers. On skip ce ticker et on continue.
        try:
            score_tech = tech.get("signal", {}).get("score", 0)

            # 1. News + Fear&Greed + BTC dominance
            fundamental = ns.analyze(ticker)

            # 2. Microstructure (funding, L/S, taker vol)
            microstructure = mm.analyze(ticker)

            # 3. On-chain (CoinGecko, DefiLlama TVL)
            onchain_data = onchain.analyze(ticker)

            # 4. Liquidations + OI + L/S ratio (Coinglass)
            coinglass_data = cg.analyze(ticker)

            # 5. Macro context (DXY + DVOL + Google Trends) — ticker-specific
            macro_ticker = macro.analyze(ticker)

            # 6. Régime de marché (HMM + GARCH + ruptures)
            df_ticker = ohlcv_data.get(ticker, pd.DataFrame())
            regime_data = rd.analyze(df_ticker)

            # 7. Volume Profile (POC, VAH, VAL, HVN/LVN)
            vp_data = vp.analyze(df_ticker)

            # 8. Filtre weekly trend — réduit le score_tech si tendance baissière
            try:
                weekly     = ts.is_weekly_uptrend(df_ticker)
                weekly_adj = weekly.get("score_adj", 1.0)
            except Exception:
                weekly_adj = 1.0
            score_tech_adj = round(score_tech * weekly_adj, 3)
            if weekly_adj < 1.0:
                logger.info(
                    f"{ticker} : tendance weekly baissière "
                    f"— score_tech {score_tech:+.2f} → {score_tech_adj:+.2f}"
                )

            # 9. Force relative vs BTC (ajoute une composante directionnelle)
            rs_data = {}
            try:
                rs_data = rs_mod.get_relative_strength(ticker, ohlcv_data)
                s_rs    = rs_data.get("score", 0.0)
            except Exception:
                s_rs = 0.0

            # 10. Social Radar (LunarCrush) — sentiment Twitter/Reddit/Telegram
            try:
                social_data = social_radar.analyze(ticker)
                s_social    = social_data.get("score", 0.0)
            except Exception:
                social_data = {}
                s_social    = 0.0

            # 11. Token unlocks — bloquer les entrées avant un unlock majeur
            try:
                unlock_data = token_unlocks.check_unlock(ticker)
                if unlock_data.get("score", 0) <= -0.5:
                    logger.info(
                        f"{ticker} : unlock imminent — bloqué "
                        f"({unlock_data.get('reason', 'unlock proche')})"
                    )
                    continue
            except Exception:
                pass

            # 11. Corrélation guard — bloquer si trop corrélé au portefeuille
            try:
                is_corr, corr_reason = correlation_guard.is_correlated(
                    ticker, open_positions_cached, ohlcv_data
                )
                if is_corr:
                    logger.info(f"{ticker} : bloqué (corrélation) — {corr_reason}")
                    continue
            except Exception:
                pass

            # ── Scores avec .get() — un module qui renvoie {} donne score=0 ─
            s_news  = fundamental.get("score_global", 0)
            s_ms    = microstructure.get("score", 0)
            s_oc    = onchain_data.get("score", 0)
            s_cg    = coinglass_data.get("score", 0)
            # Stablecoin dominance blend avec macro (50/50)
            s_macro_raw = macro_ticker.get("score", 0)
            s_macro = round((s_macro_raw + sd_score) / 2.0, 3)

            # Score final : score_tech ajusté weekly + composantes additionnelles
            score_final = compute_final_score(
                score_tech_adj, s_news, s_ms, s_oc, s_cg, s_macro,
            )
            # RS vs BTC : ajustement ±10%
            score_final = round(max(-3.0, min(3.0, score_final + s_rs * 0.10)), 2)
            # Social Radar (LunarCrush) : ajustement ±10%
            score_final = round(max(-3.0, min(3.0, score_final + s_social * 0.10)), 2)

            # Appliquer le multiplicateur de régime sur le score final
            position_multiplier = regime_data.get("position_multiplier", 1.0)

            # Score ML si disponible (remplace le score manuel après 50 trades)
            temp_payload = {
                "score": score_final, "score_tech": score_tech,
                "score_news": s_news, "score_ms": s_ms,
                "score_oc": s_oc, "score_cg": s_cg, "score_macro": s_macro,
                "dxy": macro_ticker.get("dxy", 104), "dvol": macro_ticker.get("dvol", 65),
                "ls_ratio": coinglass_data.get("ls_ratio", 1.0),
                "oi_change_4h_pct": coinglass_data.get("oi_change_4h_pct", 0),
            }
            ml_result = ml.predict_score(temp_payload, regime_data, vp_data)
            if ml_result.get("ml_active"):
                score_final = ml_result["score_ml"]

            logger.info(
                f"{ticker} | Tech:{score_tech_adj:+.2f}(x{weekly_adj}) "
                f"RS:{s_rs:+.2f} News:{s_news:+.2f} "
                f"MS:{s_ms:+.2f} OC:{s_oc:+.2f} "
                f"CG:{s_cg:+.2f} Macro:{s_macro:+.2f}(SD:{sd_score:+.2f}) "
                f"Régime:{regime_data.get('regime','?')} Vol:{regime_data.get('vol_regime','?')} "
                f"VP:{vp_data.get('score', 0):+.2f} PosMult:{position_multiplier} "
                f"→ FINAL:{score_final:+.2f}"
            )

            # 12. News Interpreter (LLM) — interprétation causale des événements
            try:
                news_interp = news_interpreter.interpret(
                    ticker       = ticker,
                    news_data    = fundamental,
                    score_context= {"score": score_final, "score_tech": score_tech_adj,
                                    "regime": regime_data.get("regime", "sideways")},
                )
                if news_interp.get("bloquer"):
                    logger.info(
                        f"{ticker} : bloqué par News Interpreter — "
                        f"{news_interp.get('raison', '')}"
                    )
                    continue
                # Impact positif/négatif ajusté sur le score final (±5%)
                if news_interp.get("impact") == "bullish":
                    score_final = round(min(3.0, score_final + 0.15), 2)
                elif news_interp.get("impact") == "bearish":
                    score_final = round(max(-3.0, score_final - 0.15), 2)
            except Exception:
                news_interp = {}

            # Appliquer le multiplicateur contextuel du Regime Enricher
            score_final = round(max(-3.0, min(3.0, score_final * ctx_mult)), 2)

            # Blocage si fondamentaux franchement négatifs ou trade non autorisé.
            # Filtré ici (Phase 1) pour ne pas envoyer d'alertes Telegram
            # pour des signaux qui ne seraient jamais exécutés en Phase 3.
            if s_news < -0.5:
                logger.debug(f"{ticker} : tech {score_tech:+.1f} bloqué — fondamentaux {s_news:+.2f}")
                continue

            if not fundamental.get("trade_autorise", False):
                logger.debug(f"{ticker} : trade_autorise=False — ignoré")
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
            payload["regime"]            = regime_data.get("regime", "sideways")
            payload["regime_context"]    = regime_data.get("regime_context", "")
            payload["position_multiplier"] = position_multiplier
            payload["vol_regime"]        = regime_data.get("vol_regime", "normal")
            payload["vol_annualized"]    = regime_data.get("vol_annualized", 80)
            payload["vp_poc"]            = vp_data.get("poc")
            payload["vp_vah"]            = vp_data.get("vah")
            payload["vp_val"]            = vp_data.get("val")
            payload["vp_score"]          = vp_data.get("score", 0)
            payload["ml_active"]         = ml_result.get("ml_active", False)
            payload["ml_confidence"]     = ml_result.get("ml_confidence", 0)
            payload["score"]             = score_final  # Score final (ML ou manuel)
            payload["trade_autorise"]    = fundamental.get("trade_autorise", False)
            # Nouveaux modules Sprint 1+2
            payload["weekly_adj"]        = weekly_adj
            payload["score_rs"]          = s_rs
            payload["rs_verdict"]        = rs_data.get("verdict", "")
            payload["sd_score"]          = sd_score
            payload["score_tech_adj"]    = score_tech_adj
            # Agents LLM
            payload["score_social"]        = s_social
            payload["social_galaxy"]       = social_data.get("galaxy_score")
            payload["social_alt_rank"]     = social_data.get("alt_rank")
            payload["social_sentiment"]    = social_data.get("sentiment")
            payload["news_interp_impact"]  = news_interp.get("impact", "neutral")
            payload["news_interp_raison"]  = news_interp.get("raison", "")
            payload["regime_narratif"]     = regime_ctx.get("regime_narratif", "")
            payload["regime_biais"]        = regime_ctx.get("biais", "neutre")
            payload["ctx_mult"]            = ctx_mult

            # Enregistrer pour l'entraînement ML futur
            ml.save_signal_for_training(payload, regime_data, vp_data)

            actionable.append(payload)

            # Alerte Telegram enrichie (immédiate — indépendante de l'exécution)
            alertes.alerte_opportunite_enrichie(payload)
            _alerted_cache[ticker] = time.time()
            logger.info(f"Signal collecté : {ticker} score_final={score_final:+.2f}")

        except Exception as _e:
            logger.error(f"[Scan] {ticker} : analyse échouée — {_e}")
            continue

        time.sleep(1)  # anti-flood API

    # ── Phase 2 : Trier par score décroissant ────────────────────────────────
    # CRITIQUE : le signal le plus fort est financé en priorité.
    # Sans ce tri, un signal faible traité en premier vide le budget USDC
    # et prive le meilleur signal de capital (bug BIO/TRX du 1er mai).
    actionable.sort(key=lambda p: p["score"], reverse=True)

    if actionable:
        ordre = " > ".join(f"{p['ticker']}({p['score']:+.2f})" for p in actionable)
        logger.info(f"Ordre d'execution par conviction : {ordre}")

    # ── Phase 3 : Exécuter dans l'ordre (meilleur score = premier servi) ─────
    for payload in actionable:
        if payload["score"] >= AUTO_EXECUTE_THRESHOLD and payload.get("trade_autorise"):
            ticker = payload["ticker"]
            score  = payload["score"]

            # ── Capital Allocator : taille + rotation + mémoire ──────────────
            usdc_dispo      = execution.get_usdt_balance()
            open_positions  = pm.get_open_positions()

            alloc = ca.calculate_allocation(
                ticker         = ticker,
                score          = score,
                portfolio_value= portfolio_value,
                usdc_available = usdc_dispo,
                open_positions = open_positions,
                context        = {
                    "score":      score,
                    "regime":     payload.get("regime", "unknown"),
                    "vol_regime": payload.get("vol_regime", "normal"),
                },
            )

            # Multiplicateur de régime connu ici — utilisé pour pré-valider
            # AVANT d'exécuter la rotation (évite de vendre une position pour rien).
            pos_mult = payload.get("position_multiplier", 1.0)

            # Rotation : libérer du capital si besoin avant d'acheter
            if alloc["rotation_needed"] and alloc["rotation_candidate"]:
                # Pré-check : même si la rotation libère tout le capital,
                # la taille finale sera-t-elle >= $20 ?
                taille_max_post_rotation = alloc["target_usdt"] * pos_mult
                if taille_max_post_rotation < 20:
                    logger.warning(
                        f"[Phase 3] {ticker} : taille max post-rotation "
                        f"${taille_max_post_rotation:.0f} < $20 — rotation annulée "
                        f"(évite de vendre {alloc['rotation_candidate']['ticker']} pour rien)"
                    )
                else:
                    rotated = ca.execute_rotation(alloc["rotation_candidate"], payload)
                    if rotated:
                        # Recalculer contre le budget CIBLE (target_usdt), pas l'ancien cap.
                        usdc_dispo = execution.get_usdt_balance()
                        alloc["taille_allouee"] = min(
                            alloc["target_usdt"], usdc_dispo * 0.95
                        )

            # Taille finale après régime
            taille_finale = round(alloc["taille_allouee"] * pos_mult, 2)

            # Injecter la taille dans le payload — execution.py va la lire
            payload["taille_allouee"] = taille_finale

            if taille_finale < 20:
                logger.warning(
                    f"[Phase 3] {ticker} : taille ${taille_finale:.0f} < $20 après régime "
                    f"x{pos_mult} — ordre ignoré (allocateur: {alloc['reasoning']})"
                )
                continue

            logger.info(
                f"[Phase 3] {ticker} : taille=${taille_finale:.0f} "
                f"(allocateur: {alloc['reasoning']}, régime x{pos_mult})"
            )

            execution.execute_signal(payload, portfolio_value)

            # ── Mémoriser l'entrée si l'ordre a été confirmé par OKX ─────────
            if payload.get("ordre_execute"):
                try:
                    import ruflo_memory as rm
                    rm.store_trade_entry(payload)
                except Exception:
                    pass

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
