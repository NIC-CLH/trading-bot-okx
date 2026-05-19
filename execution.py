"""
Module d'exécution automatique des ordres sur OKX.
Reçoit un signal, place l'ordre, notifie Telegram.

Sizing : délégué à capital_allocator.py (score → tier 12%/17%/22% + mémoire ruflo).
Fallback interne (CONVICTION_MULTIPLIERS) utilisé uniquement si appelé sans scanner.
"""

import logging
import sqlite3
from datetime import datetime

import alertes
import config
import okx_client as okx

logger = logging.getLogger(__name__)

BUDGET_PCT = 0.95       # 95% du capital peut être investi (5% gardé pour frais)
MAX_TRADE_PCT = 0.25    # Plafond fallback : max 25% du capital par position (aligné avec capital_allocator HARD_CAP_PCT)

# Multiplicateurs de conviction selon la force du signal
# Signal fort → position plus grosse dans la limite du budget
CONVICTION_MULTIPLIERS = {
    2.5: 1.25,   # Score >= 2.5 : +25% (signal exceptionnel)
    2.0: 1.0,    # Score >= 2.0 : taille normale
    1.5: 0.75,   # Score >= 1.5 : -25% (signal limite)
}


def get_trading_budget(portfolio_value: float) -> float:
    return portfolio_value * BUDGET_PCT


def get_max_trade_size(portfolio_value: float) -> float:
    return get_trading_budget(portfolio_value) * MAX_TRADE_PCT


def get_usdt_balance() -> float:
    """Récupère le solde USDT disponible sur OKX."""
    try:
        balances = okx.get_balances()
        return balances.get("USDT", 0.0) + balances.get("USDC", 0.0)
    except Exception as e:
        logger.error(f"Erreur récupération balance OKX : {e}")
        return 0.0


def log_trade(trade: dict, db_path: str = config.DB_PATH):
    """Enregistre chaque trade en base SQLite."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                exchange TEXT,
                ticker TEXT,
                side TEXT,
                quantite REAL,
                prix REAL,
                montant_usdt REAL,
                stop_loss REAL,
                take_profit REAL,
                signal_score REAL,
                score_news REAL,
                ordre_id TEXT,
                statut TEXT
            )
        """)
        cursor.execute("""
            INSERT INTO trades
            (timestamp, exchange, ticker, side, quantite, prix, montant_usdt,
             stop_loss, take_profit, signal_score, score_news, ordre_id, statut)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.utcnow().isoformat(),
            "okx",
            trade.get("ticker"),
            trade.get("side"),
            trade.get("quantite"),
            trade.get("prix"),
            trade.get("montant_usdt"),
            trade.get("stop_loss"),
            trade.get("take_profit"),
            trade.get("signal_score"),
            trade.get("score_news", 0),
            trade.get("ordre_id"),
            trade.get("statut", "ouvert"),
        ))
        conn.commit()


def execute_signal(signal: dict, portfolio_value: float) -> bool:
    """
    Exécute un signal d'achat/vente sur OKX.
    Retourne True si l'ordre est passé avec succès.
    """
    ticker = signal["ticker"]
    score = signal.get("score", 0)
    score_news = signal.get("score_news", 0)
    prix = signal.get("prix")
    stop = signal.get("stop")
    target = signal.get("target")
    trade_autorise = signal.get("trade_autorise", False)

    # Vérifications préalables
    if stop is None or target is None or not prix:
        logger.warning(f"{ticker} : stop/target/prix manquant — ordre annulé")
        return False

    if abs(score) < 1.5:
        logger.info(f"{ticker} : score {score:+.2f} insuffisant — ordre annulé")
        return False

    if not trade_autorise:
        logger.debug(f"{ticker} : fondamentaux négatifs (score_news={score_news:+.2f}) — ordre bloqué")
        return False

    # ── Forcer le stop à max -4% du prix d'entrée ────────────────────────────
    # Les signaux techniques peuvent produire des stops très larges (ATR-based).
    # On plafonne à -4% pour rester cohérent avec la stratégie rotation rapide.
    MAX_STOP_DISTANCE_PCT = 0.04
    if score > 0:  # achat — stop en dessous
        min_stop = prix * (1 - MAX_STOP_DISTANCE_PCT)
        stop = max(stop, min_stop)   # on garde le stop le plus haut (le moins risqué)
    else:          # vente short — stop au dessus
        max_stop = prix * (1 + MAX_STOP_DISTANCE_PCT)
        stop = min(stop, max_stop)

    # Recalculer le target pour maintenir R/R >= 1.5
    risque = abs(prix - stop)
    target_min = prix + risque * 1.5 if score > 0 else prix - risque * 1.5
    if score > 0:
        target = max(target, target_min)
    else:
        target = min(target, target_min)

    # ── Budget ───────────────────────────────────────────────────────────────
    # Si capital_allocator a déjà calculé la taille → la réutiliser directement.
    # Sinon (appel direct sans scanner) → fallback sur l'ancienne logique conviction.
    usdt_available = get_usdt_balance()

    if usdt_available < 30:
        # Pas d'alerte Telegram — juste un log (évite le spam quand budget épuisé)
        logger.info(f"{ticker} : USDC insuffisant (${usdt_available:.2f} < $30) — ignoré")
        return False

    taille_allouee = signal.get("taille_allouee")  # injectée par scanner/capital_allocator

    if taille_allouee and taille_allouee > 0:
        # Chemin principal : capital_allocator a fait le travail
        trade_size_usdt = min(taille_allouee, usdt_available * 0.95)
        logger.info(
            f"{ticker} : taille allocateur=${taille_allouee:.0f} "
            f"| usdc_dispo=${usdt_available:.0f} "
            f"→ taille finale=${trade_size_usdt:.0f}"
        )
    else:
        # Fallback : ancienne logique par conviction score
        max_trade_base = get_max_trade_size(portfolio_value)
        score_abs = abs(score)
        conviction = 0.75
        for threshold, mult in sorted(CONVICTION_MULTIPLIERS.items(), reverse=True):
            if score_abs >= threshold:
                conviction = mult
                break
        max_trade = max_trade_base * conviction
        trade_size_usdt = min(max_trade, usdt_available * 0.95)
        logger.info(
            f"{ticker} : portfolio=${portfolio_value:.0f} | max_trade=${max_trade:.0f} "
            f"(conviction x{conviction}) | usdc_dispo=${usdt_available:.0f} "
            f"→ taille finale=${trade_size_usdt:.0f} [fallback]"
        )

    if trade_size_usdt < 20:
        logger.warning(f"{ticker} : taille calculée ${trade_size_usdt:.2f} < $20 — ignoré")
        return False

    quantite = trade_size_usdt / prix
    side = "buy" if score > 0 else "sell"

    logger.info(
        f"Exécution OKX : {side} {quantite:.4f} {ticker} @ ${prix:.4f} "
        f"| SL: ${stop:.4f} | TP: ${target:.4f} | Score: {score:+.2f}"
    )

    # ── Seul l'ordre OKX peut faire échouer l'exécution ────────────────────────
    # log_trade (SQLite) et alertes.send (Telegram) sont non-bloquants :
    # leur échec ne doit pas masquer un ordre OKX réussi ni empêcher
    # signal["ordre_execute"] d'être positionné à True.
    try:
        result = okx.place_order(
            ticker=ticker,
            side=side,
            usdt_amount=trade_size_usdt if side == "buy" else None,
            quantity=quantite if side == "sell" else None,
            order_type="market",
            stop_loss=stop,
            take_profit=target,
        )
    except Exception as e:
        err_str = str(e)
        logger.error(f"Échec ordre OKX {ticker} : {e}")
        # "All operations failed (code 1)" = paire non disponible EEA ou instrument suspendu
        # → ajouter le ticker à EXCLUDE dans scanner.py et alert_scanner.py
        if "All operations failed" in err_str or "(code 1)" in err_str:
            logger.warning(
                f"{ticker} : 'All operations failed' = paire probablement restreinte EEA. "
                f"Ajouter '{ticker}' à EXCLUDE dans scanner.py et alert_scanner.py."
            )
            try:
                alertes.send(
                    f"⚠️ *Ordre {ticker} bloqué* (OKX) : paire probablement restreinte EEA\n"
                    f"`{err_str[:100]}`\n"
                    f"_Ajouter '{ticker}' à EXCLUDE pour stopper les tentatives._"
                )
            except Exception:
                pass
        else:
            try:
                alertes.send(f"❌ *Échec ordre {ticker}* (OKX) : {err_str[:100]}")
            except Exception:
                pass
        return False

    # Ordre confirmé par OKX — flag positionné immédiatement
    ordre_id = result.get("ordId", "unknown")
    signal["ordre_execute"] = True
    logger.info(f"Ordre OKX {ordre_id} exécuté avec succès")

    # Log SQLite (non-bloquant)
    try:
        log_trade({
            "ticker": ticker,
            "side": side,
            "quantite": quantite,
            "prix": prix,
            "montant_usdt": trade_size_usdt,
            "stop_loss": stop,
            "take_profit": target,
            "signal_score": score,
            "score_news": score_news,
            "ordre_id": ordre_id,
            "statut": "ouvert",
        })
    except Exception as e:
        logger.warning(f"log_trade SQLite échoué (ordre quand même exécuté) : {e}")

    # Notification Telegram (non-bloquante)
    try:
        direction = "📈 ACHAT" if side == "buy" else "📉 VENTE"
        rr = abs(target - prix) / abs(prix - stop) if stop != prix else 0
        verdict_news = signal.get("verdict_news", "")
        msg = f"""✅ *Ordre exécuté automatiquement*

{direction} *{ticker}* sur OKX
Prix d'entrée : `${prix:.4f}`
Quantité : `{quantite:.4f} {ticker}`
Montant : `${trade_size_usdt:.2f} USDT`

🛡 Stop-loss : `${stop:.4f}`
🎯 Take-profit : `${target:.4f}`
⚖️ R/R : {rr:.1f}:1

📊 Score technique : `{signal.get('score_tech', score):+.1f}`
📰 Score fondamental : `{score_news:+.2f}` ({verdict_news})
🆔 Ordre : `{ordre_id}`
_Vous pouvez annuler manuellement sur OKX si besoin._"""
        alertes.send(msg)
    except Exception:
        pass

    return True
