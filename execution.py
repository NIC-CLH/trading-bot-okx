"""
Module d'exécution automatique des ordres sur OKX.
Reçoit un signal, calcule la taille, place l'ordre, notifie Telegram.
Budget max : 10% du portefeuille total, 20% du budget par trade.
"""

import logging
import sqlite3
from datetime import datetime

import alertes
import config
import okx_client as okx

logger = logging.getLogger(__name__)

BUDGET_PCT = 0.95       # 95% du capital peut être investi (5% gardé pour frais)
MAX_TRADE_PCT = 0.25    # Max 25% du capital par position (4 positions max)


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
    conn = sqlite3.connect(db_path)
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
    conn.close()


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
    if not stop or not target or not prix:
        logger.warning(f"{ticker} : stop/target/prix manquant — ordre annulé")
        return False

    if abs(score) < 1.5:
        logger.info(f"{ticker} : score {score:+.2f} insuffisant — ordre annulé")
        return False

    if not trade_autorise:
        logger.info(f"{ticker} : fondamentaux négatifs (score_news={score_news:+.2f}) — ordre bloqué")
        return False

    # Taille de position
    max_trade = get_max_trade_size(portfolio_value)
    usdt_available = get_usdt_balance()

    if usdt_available < 10:
        alertes.send(f"⚠️ Budget USDT/USDC insuffisant : ${usdt_available:.2f} disponible")
        return False

    trade_size_usdt = min(max_trade, usdt_available * 0.95)  # garde 5% pour frais

    if trade_size_usdt < 5:
        logger.warning(f"Trade trop petit : ${trade_size_usdt:.2f} — ignoré")
        return False

    quantite = trade_size_usdt / prix
    side = "buy" if score > 0 else "sell"

    logger.info(
        f"Exécution OKX : {side} {quantite:.4f} {ticker} @ ${prix:.4f} "
        f"| SL: ${stop:.4f} | TP: ${target:.4f} | Score: {score:+.2f}"
    )

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

        ordre_id = result.get("ordId", "unknown")

        # Log SQLite
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

        # Notification Telegram
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
        logger.info(f"Ordre OKX {ordre_id} exécuté avec succès")
        return True

    except Exception as e:
        logger.error(f"Échec ordre OKX {ticker} : {e}")
        alertes.send(f"❌ *Échec ordre {ticker}* (OKX) : {str(e)[:100]}")
        return False
