"""
Gestionnaire de positions — stratégie exits fixes.

Philosophie : une fois en position, le bot est SOURD aux signaux.
On ne sort QUE sur trois événements prédéfinis à l'entrée :
  1. Stop fixe    : -7% du prix d'entrée (absorbe le bruit crypto normal)
  2. Objectif fixe: +12% du prix d'entrée (prise de profit partielle à +7%)
  3. Temps max    : 7 jours — si ni stop ni objectif → on sort et on passe à autre chose

Ce qui est SUPPRIMÉ :
  - Sorties basées sur le score technique (whipsawing)
  - Rotations actives (vendre X pour acheter Y)
  - Trailing stop complexe

Seul le stop dur en prix et le temps décident de la sortie.
"""

import logging
import time as time_module
import sqlite3
from datetime import datetime, timezone

import okx_client as okx
import technical_signals as ts
import alertes
import config

logger = logging.getLogger(__name__)

# ── Paramètres fixes — ne pas toucher sans réflexion ─────────────────────────
STOP_LOSS_PCT        = -0.07   # -7%  → sortie automatique, sans condition
PARTIAL_PROFIT_PCT   =  0.07   # +7%  → vendre 50%, laisser courir
FULL_PROFIT_PCT      =  0.12   # +12% → sortie complète
MAX_HOLDING_DAYS     =  7      # 7 jours max en position, quelle que soit la situation
MAX_POSITIONS        =  4


# ── Données de position ───────────────────────────────────────────────────────

def get_open_positions() -> list[dict]:
    """Retourne les positions ouvertes avec P&L et durée depuis l'entrée."""
    try:
        balances = okx.get_balances()
    except Exception as e:
        logger.error(f"Erreur balances OKX : {e}")
        return []

    stablecoins = {"USDC", "USDT", "BUSD", "DAI"}
    positions = []

    for ticker, qty in balances.items():
        if ticker in stablecoins or qty <= 0:
            continue

        prix_actuel = okx.get_price_usdc(ticker)
        if not prix_actuel:
            continue

        entry_info  = _get_entry_info(ticker)
        prix_entree = entry_info.get("price")
        entry_ts    = entry_info.get("time")

        pnl_pct = ((prix_actuel - prix_entree) / prix_entree * 100) if prix_entree else None
        pnl_usd = (prix_actuel - prix_entree) * qty if prix_entree else None

        days_held = None
        if entry_ts:
            days_held = (time_module.time() - entry_ts) / 86400

        positions.append({
            "ticker":      ticker,
            "qty":         qty,
            "prix_actuel": prix_actuel,
            "prix_entree": prix_entree,
            "entry_ts":    entry_ts,
            "days_held":   days_held,
            "valeur_usd":  qty * prix_actuel,
            "pnl_pct":     pnl_pct,
            "pnl_usd":     pnl_usd,
        })

    return positions


def _get_entry_info(ticker: str) -> dict:
    """Prix d'entrée + timestamp depuis les fills OKX, fallback SQLite."""
    try:
        data = okx._get("/api/v5/trade/fills", {
            "instId": f"{ticker.upper()}-USDC",
            "limit": "20",
        })
        if data:
            buys = [f for f in data if f.get("side") == "buy"]
            if buys:
                total_qty  = sum(float(f["fillSz"]) for f in buys)
                total_cost = sum(float(f["fillSz"]) * float(f["fillPx"]) for f in buys)
                price = total_cost / total_qty if total_qty > 0 else None
                timestamps = [int(f.get("ts", 0)) for f in buys if f.get("ts")]
                entry_ts = max(timestamps) / 1000 if timestamps else None
                return {"price": price, "time": entry_ts}
    except Exception:
        pass

    try:
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT AVG(prix), MAX(timestamp) FROM trades
            WHERE ticker = ? AND side = 'buy' AND statut != 'annulé'
            ORDER BY timestamp DESC LIMIT 5
        """, (ticker,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            entry_ts = None
            try:
                dt = datetime.fromisoformat(row[1])
                entry_ts = dt.timestamp()
            except Exception:
                pass
            return {"price": float(row[0]), "time": entry_ts}
    except Exception:
        pass

    return {"price": None, "time": None}


# Alias compatibilité
def _get_entry_price(ticker: str) -> float | None:
    return _get_entry_info(ticker).get("price")


# ── Évaluation ────────────────────────────────────────────────────────────────

def evaluate_position(pos: dict) -> dict:
    """
    Évalue une position avec la règle des exits fixes.
    Ignore complètement le score technique — seuls le prix et le temps comptent.

    Décisions : HOLD | PARTIAL_SELL | FULL_SELL
    """
    ticker    = pos["ticker"]
    prix      = pos["prix_actuel"]
    entree    = pos["prix_entree"]
    pnl_pct   = pos.get("pnl_pct") or 0
    days_held = pos.get("days_held")
    valeur    = pos["valeur_usd"]

    decision = "HOLD"
    raison   = ""
    urgence  = False

    # ── P1 : Stop dur -7% ────────────────────────────────────────────────────
    if entree and pnl_pct <= STOP_LOSS_PCT * 100:
        decision = "FULL_SELL"
        raison   = f"Stop -7% déclenché ({pnl_pct:.1f}%)"
        urgence  = True

    # ── P2 : Time stop — 7 jours sans atteindre l'objectif ──────────────────
    elif days_held and days_held >= MAX_HOLDING_DAYS and pnl_pct < FULL_PROFIT_PCT * 100:
        decision = "FULL_SELL"
        raison   = f"7 jours écoulés (P&L {pnl_pct:+.1f}%) — on libère le capital"

    # ── P3 : Objectif +12% atteint → sortie complète ─────────────────────────
    elif pnl_pct >= FULL_PROFIT_PCT * 100:
        decision = "FULL_SELL"
        raison   = f"Objectif +12% atteint ({pnl_pct:+.1f}%) 🎯"

    # ── P4 : +7% atteint → prendre la moitié des gains ──────────────────────
    elif pnl_pct >= PARTIAL_PROFIT_PCT * 100:
        decision = "PARTIAL_SELL"
        raison   = f"Mi-chemin +7% ({pnl_pct:+.1f}%) — sécuriser 50%"

    if decision == "HOLD":
        days_str = f"{days_held:.1f}j" if days_held else "?"
        raison = f"En cours ({pnl_pct:+.1f}% | {days_str} / 7j)"

    return {
        "ticker":    ticker,
        "decision":  decision,
        "raison":    raison,
        "urgence":   urgence,
        "pnl_pct":   pnl_pct,
        "pnl_usd":   pos.get("pnl_usd"),
        "valeur":    valeur,
        "qty":       pos["qty"],
        "days_held": days_held,
    }


# ── Exécution ──────────────────────────────────────────────────────────────────

def execute_decision(decision: dict, portfolio_value: float) -> bool:
    ticker = decision["ticker"]
    action = decision["decision"]
    qty    = decision["qty"]

    if action == "FULL_SELL":
        try:
            result   = okx.place_order(ticker=ticker, side="sell",
                                        quantity=qty * 0.999, order_type="market")
            ordre_id = result.get("ordId", "?")
            pnl_str  = f"{decision['pnl_pct']:+.1f}%" if decision["pnl_pct"] is not None else "N/A"
            emoji    = "🟢" if (decision["pnl_pct"] or 0) >= 0 else "🔴"
            days_str = f"{decision['days_held']:.1f}j" if decision.get("days_held") else ""

            alertes.send(
                f"{emoji} *VENTE {ticker}*{f' ({days_str})' if days_str else ''}\n"
                f"Raison : {decision['raison']}\n"
                f"Valeur : `${decision['valeur']:.2f}`\n"
                f"P&L : `{pnl_str}`\n"
                f"ID : `{ordre_id}`"
            )
            logger.info(f"SELL {ticker} — {decision['raison']} | P&L {pnl_str}")
            return True
        except Exception as e:
            logger.error(f"Erreur vente {ticker} : {e}")
            alertes.send(f"❌ Échec vente {ticker} : {str(e)[:120]}")
            return False

    elif action == "PARTIAL_SELL":
        qty_sell = qty * 0.50
        try:
            result   = okx.place_order(ticker=ticker, side="sell",
                                        quantity=qty_sell * 0.999, order_type="market")
            ordre_id = result.get("ordId", "?")
            alertes.send(
                f"🟡 *VENTE PARTIELLE {ticker}* (50%)\n"
                f"Raison : {decision['raison']}\n"
                f"Valeur : `${decision['valeur'] * 0.5:.2f}`\n"
                f"P&L : `{decision['pnl_pct']:+.1f}%`\n"
                f"ID : `{ordre_id}`"
            )
            logger.info(f"PARTIAL SELL {ticker} — {decision['raison']}")
            return True
        except Exception as e:
            logger.error(f"Erreur vente partielle {ticker} : {e}")
            return False

    return False  # HOLD


# ── Filtre BTC 50MA ────────────────────────────────────────────────────────────

def is_btc_uptrend() -> bool:
    """
    Vérifie que BTC est au-dessus de sa moyenne mobile 50 jours.
    Si non → marché potentiellement baissier → on n'achète rien.
    """
    try:
        df = okx.get_ohlcv("BTC", days=60)
        if df.empty or len(df) < 50:
            logger.warning("BTC 50MA : données insuffisantes — filtre désactivé")
            return True  # Par défaut, on laisse passer
        ma50  = df["close"].rolling(50).mean().iloc[-1]
        price = df["close"].iloc[-1]
        uptrend = price > ma50
        logger.info(
            f"BTC 50MA : prix ${price:,.0f} {'>' if uptrend else '<'} MA50 ${ma50:,.0f} "
            f"→ {'✅ achat autorisé' if uptrend else '🚫 marché baissier — achats bloqués'}"
        )
        return uptrend
    except Exception as e:
        logger.warning(f"BTC 50MA check échoué : {e} — filtre désactivé")
        return True


# ── Point d'entrée ────────────────────────────────────────────────────────────

def run(portfolio_value: float, **kwargs) -> dict:
    """
    Gestion complète des positions toutes les 4h.
    Sorties basées uniquement sur stops/objectifs/temps fixes.
    """
    logger.info("Gestion des positions (exits fixes : -7% / +12% / 7j)...")

    positions = get_open_positions()
    if not positions:
        logger.info("Aucune position ouverte.")
        return {"positions": [], "actions": [], "btc_uptrend": is_btc_uptrend()}

    actions      = []
    danger_lines = []

    for pos in positions:
        decision  = evaluate_position(pos)
        pnl       = decision["pnl_pct"] or 0
        days      = f"{decision['days_held']:.1f}j" if decision.get("days_held") else "?"

        logger.info(
            f"{pos['ticker']} | P&L {pnl:+.1f}% | {days} | → {decision['decision']}"
        )

        if decision["decision"] != "HOLD":
            success = execute_decision(decision, portfolio_value)
            if success:
                actions.append(decision)
        else:
            # Alerte danger si proche du stop (-5%) sans l'avoir déclenché
            if pnl <= -5.0:
                emoji = "🔴" if pnl < -6 else "🟠"
                danger_lines.append(
                    f"{emoji} *{pos['ticker']}* `${pos['prix_actuel']:.4f}` "
                    f"P&L `{pnl:+.1f}%` — stop à -7%"
                )

    if danger_lines:
        alertes.send(
            "⚠️ *Positions proches du stop*\n\n"
            + "\n".join(danger_lines)
            + "\n\n_Stop automatique à -7% — surveille si tu veux couper avant._"
        )

    return {"positions": positions, "actions": actions}
