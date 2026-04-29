"""
Gestionnaire de positions — cerveau de la gestion active du portefeuille.

Toutes les 4h, pour chaque position ouverte :
  1. Calcule le P&L actuel
  2. Réévalue le signal technique
  3. Décide : tenir / renforcer / prendre profit partiel / sortir

Règles de gestion :
  - Stop-loss dynamique (trailing stop si +15% de gain)
  - Sortie si signal technique retourne fortement négatif (score < -1.5)
  - Prise de profit partielle à +20% (vend 50%, laisse courir 50%)
  - Sortie totale si signal < -2.0 ou perte > stop initial
  - Renforcement si signal > 2.5 et position < 20% du portfolio
"""

import logging
import sqlite3
from datetime import datetime, timezone

import okx_client as okx
import technical_signals as ts
import regime_detector as rd
import alertes
import config

logger = logging.getLogger(__name__)

# Paramètres de gestion
STOP_SIGNAL_EXIT = -1.0       # Score technique → sortie défensive (abaissé de -1.5)
HARD_SIGNAL_EXIT = -1.8       # Score technique → sortie urgente (abaissé de -2.0)
PARTIAL_PROFIT_PCT = 0.15     # +15% → prise de profit partielle (50%)
TRAILING_STOP_TRIGGER = 0.12  # +12% → active le trailing stop
TRAILING_STOP_DISTANCE = 0.06 # Trailing stop à 6% sous le plus haut
REINFORCE_SCORE = 2.5         # Score → renforcement possible
MAX_POSITIONS = 4
MIN_USDC_RESERVE_PCT = 0.05   # Garder 5% en USDC (frais + opportunités)

# Stop loss en prix — indépendant du score technique
HARD_PRICE_STOP_PCT = -0.10   # -10% depuis entrée → sortie automatique (stop loss dur)
SOFT_PRICE_STOP_PCT = -0.07   # -7% + score négatif → sortie défensive
MAX_HOLDING_DAYS = 10         # Position perdante depuis > 10 jours → sortie


def get_open_positions() -> list[dict]:
    """
    Récupère les positions ouvertes depuis OKX + enrichit avec le prix d'entrée depuis SQLite.
    Retourne la liste des positions avec P&L calculé.
    """
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

        valeur = qty * prix_actuel

        # Récupérer le prix d'entrée depuis SQLite
        prix_entree = _get_entry_price(ticker)
        pnl_pct = ((prix_actuel - prix_entree) / prix_entree * 100) if prix_entree else None
        pnl_usd = (prix_actuel - prix_entree) * qty if prix_entree else None

        positions.append({
            "ticker": ticker,
            "qty": qty,
            "prix_actuel": prix_actuel,
            "prix_entree": prix_entree,
            "valeur_usd": valeur,
            "pnl_pct": pnl_pct,
            "pnl_usd": pnl_usd,
        })

    return positions


def _get_entry_price(ticker: str) -> float | None:
    """
    Récupère le prix moyen d'entrée depuis l'historique OKX (fills récents).
    Fallback sur SQLite local si disponible.
    """
    # 1. Essai via OKX fills (fonctionne sur GitHub Actions)
    try:
        data = okx._get("/api/v5/trade/fills", {
            "instId": f"{ticker.upper()}-USDC",
            "limit": "20",
        })
        if data:
            buys = [f for f in data if f.get("side") == "buy"]
            if buys:
                total_qty = sum(float(f["fillSz"]) for f in buys)
                total_cost = sum(float(f["fillSz"]) * float(f["fillPx"]) for f in buys)
                return total_cost / total_qty if total_qty > 0 else None
    except Exception:
        pass

    # 2. Fallback SQLite local
    try:
        conn = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT AVG(prix) FROM trades
            WHERE ticker = ? AND side = 'buy' AND statut != 'annulé'
            ORDER BY timestamp DESC LIMIT 5
        """, (ticker,))
        row = cursor.fetchone()
        conn.close()
        return float(row[0]) if row and row[0] else None
    except Exception:
        return None


def _get_highest_price_since_entry(ticker: str, entry_price: float) -> float:
    """Prix le plus haut depuis l'entrée (pour trailing stop)."""
    try:
        df = okx.get_ohlcv(ticker, days=30)
        if df.empty:
            return entry_price
        return max(float(df["high"].max()), entry_price)
    except Exception:
        return entry_price


def _get_regime_adjusted_thresholds(regime_data: dict) -> dict:
    """
    Ajuste les seuils de sortie selon le régime de marché (HMM + GARCH).

    En régime BEAR : on sort plus tôt (seuils relevés vers 0).
    Volatilité extrême : stop prix plus serré, profit pris plus vite.
    """
    regime = regime_data.get("regime", "sideways")
    vol_regime = regime_data.get("vol_regime", "normal")
    recent_break = regime_data.get("recent_break", False)

    # Seuils de base
    hard_signal = HARD_SIGNAL_EXIT    # -1.8
    soft_signal = STOP_SIGNAL_EXIT    # -1.0
    hard_price = HARD_PRICE_STOP_PCT  # -10%
    soft_price = SOFT_PRICE_STOP_PCT  # -7%
    partial_profit = PARTIAL_PROFIT_PCT  # +15%
    trailing_trigger = TRAILING_STOP_TRIGGER  # +12%

    # Régime BEAR : sortir plus rapidement des positions longues
    if regime == "bear":
        hard_signal = -1.2   # Seuil d'urgence relevé
        soft_signal = -0.6   # Sortie défensive plus tôt
        hard_price = -0.07   # Stop dur réduit à -7%
        soft_price = -0.04   # Stop défensif réduit à -4%
        partial_profit = 0.08  # Prendre profit dès +8% en bear
        trailing_trigger = 0.07
        logger.debug("Régime BEAR : seuils de sortie resserrés")

    # Volatilité élevée ou extrême : encore plus conservateur
    if vol_regime in ("elevated", "extreme"):
        hard_price = min(hard_price, -0.06)
        soft_price = min(soft_price, -0.03)
        partial_profit = min(partial_profit, 0.10)
        trailing_trigger = min(trailing_trigger, 0.08)
        logger.debug(f"Volatilité {vol_regime} : stops resserrés")

    # Rupture structurelle récente : prudence accrue
    if recent_break:
        soft_signal = min(soft_signal, -0.5)
        logger.debug("Rupture structurelle récente : sortie défensive abaissée")

    return {
        "hard_signal": hard_signal,
        "soft_signal": soft_signal,
        "hard_price_pct": hard_price * 100,
        "soft_price_pct": soft_price * 100,
        "partial_profit_pct": partial_profit * 100,
        "trailing_trigger_pct": trailing_trigger * 100,
    }


def evaluate_position(pos: dict, tech: dict, regime_data: dict = None) -> dict:
    """
    Évalue une position et retourne la décision de gestion.
    Décisions : HOLD | PARTIAL_SELL | FULL_SELL | REINFORCE | TRAILING_STOP

    regime_data : optionnel, output de regime_detector.analyze()
    Quand fourni, les seuils s'adaptent au régime HMM + volatilité GARCH.
    """
    ticker = pos["ticker"]
    prix = pos["prix_actuel"]
    entree = pos["prix_entree"]
    pnl_pct = pos["pnl_pct"] or 0
    valeur = pos["valeur_usd"]

    sig = tech.get("signal", {})
    score = sig.get("score", 0)
    verdict = sig.get("verdict", "")

    # Ajustement des seuils selon le régime
    regime_info = regime_data or {}
    t = _get_regime_adjusted_thresholds(regime_info)
    regime_name = regime_info.get("regime", "sideways")
    regime_suffix = f" [régime {regime_name}]" if regime_data else ""

    decision = "HOLD"
    raison = ""
    urgence = False

    # ── PRIORITÉ 1 : Stop loss dur en prix ────────────────────────────────
    if entree and pnl_pct <= t["hard_price_pct"]:
        decision = "FULL_SELL"
        raison = f"Stop loss prix ({pnl_pct:.1f}%){regime_suffix}"
        urgence = True

    # ── PRIORITÉ 2 : Stop défensif prix + score négatif ───────────────────
    elif entree and pnl_pct <= t["soft_price_pct"] and score < 0:
        decision = "FULL_SELL"
        raison = f"Stop défensif ({pnl_pct:.1f}% + score {score:+.2f}){regime_suffix}"
        urgence = True

    # ── PRIORITÉ 3 : Signal très baissier ─────────────────────────────────
    elif score <= t["hard_signal"]:
        decision = "FULL_SELL"
        raison = f"Signal très baissier (score {score:+.2f}){regime_suffix}"
        urgence = True

    # ── PRIORITÉ 4 : Sortie défensive signal négatif ──────────────────────
    elif score <= t["soft_signal"] and pnl_pct < 3:
        decision = "FULL_SELL"
        raison = f"Signal négatif (score {score:+.2f}) + position marginale{regime_suffix}"

    # ── PRIORITÉ 5 : Prise de profit ──────────────────────────────────────
    elif pnl_pct >= t["partial_profit_pct"]:
        if score > 0:
            decision = "PARTIAL_SELL"
            raison = f"Prise de profit partielle (+{pnl_pct:.1f}%){regime_suffix}"
        else:
            decision = "FULL_SELL"
            raison = f"Prise de profit totale (+{pnl_pct:.1f}%) — momentum s'essoufle{regime_suffix}"

    # ── PRIORITÉ 6 : Trailing stop ────────────────────────────────────────
    elif pnl_pct >= t["trailing_trigger_pct"] and entree:
        plus_haut = _get_highest_price_since_entry(ticker, entree)
        trailing_stop = plus_haut * (1 - TRAILING_STOP_DISTANCE)
        if prix < trailing_stop:
            decision = "FULL_SELL"
            raison = f"Trailing stop (${prix:.4f} < ${trailing_stop:.4f}){regime_suffix}"

    # ── HOLD par défaut ────────────────────────────────────────────────────
    if decision == "HOLD":
        raison = f"Signal {score:+.2f} — {verdict}{regime_suffix}"

    return {
        "ticker": ticker,
        "decision": decision,
        "raison": raison,
        "urgence": urgence,
        "score": score,
        "pnl_pct": pnl_pct,
        "pnl_usd": pos.get("pnl_usd"),
        "valeur": valeur,
        "qty": pos["qty"],
        "regime": regime_name,
    }


def execute_decision(decision: dict, portfolio_value: float) -> bool:
    """Exécute la décision de gestion sur OKX."""
    ticker = decision["ticker"]
    action = decision["decision"]
    qty = decision["qty"]

    if action == "FULL_SELL":
        try:
            result = okx.place_order(
                ticker=ticker,
                side="sell",
                quantity=qty * 0.999,  # 0.1% marge pour les frais
                order_type="market",
            )
            ordre_id = result.get("ordId", "?")
            pnl_str = f"{decision['pnl_pct']:+.1f}%" if decision['pnl_pct'] else "N/A"
            emoji = "🟢" if (decision['pnl_pct'] or 0) > 0 else "🔴"

            alertes.send(
                f"{emoji} *VENTE {ticker}* (OKX)\n"
                f"Raison : {decision['raison']}\n"
                f"Quantité : `{qty:.4f} {ticker}`\n"
                f"Valeur : `${decision['valeur']:.2f}`\n"
                f"P&L : `{pnl_str}`\n"
                f"ID : `{ordre_id}`"
            )
            logger.info(f"SELL {ticker} exécuté — {decision['raison']}")
            return True
        except Exception as e:
            logger.error(f"Erreur vente {ticker} : {e}")
            alertes.send(f"❌ Échec vente {ticker} : {str(e)[:100]}")
            return False

    elif action == "PARTIAL_SELL":
        qty_sell = qty * 0.50  # Vend 50%
        try:
            result = okx.place_order(
                ticker=ticker,
                side="sell",
                quantity=qty_sell * 0.999,
                order_type="market",
            )
            ordre_id = result.get("ordId", "?")
            alertes.send(
                f"🟡 *VENTE PARTIELLE {ticker}* (50%)\n"
                f"Raison : {decision['raison']}\n"
                f"Quantité vendue : `{qty_sell:.4f} {ticker}`\n"
                f"P&L : `{decision['pnl_pct']:+.1f}%`\n"
                f"ID : `{ordre_id}`"
            )
            logger.info(f"PARTIAL SELL {ticker} — {decision['raison']}")
            return True
        except Exception as e:
            logger.error(f"Erreur vente partielle {ticker} : {e}")
            return False

    return False  # HOLD — rien à faire


def run(portfolio_value: float, ohlcv_data: dict = None) -> dict:
    """
    Gestion complète des positions ouvertes.
    Appelé à chaque cycle de scan (toutes les 4h).
    """
    logger.info("Gestion des positions ouvertes...")

    positions = get_open_positions()
    if not positions:
        logger.info("Aucune position ouverte.")
        return {"positions": [], "actions": []}

    # Charger les données techniques si pas déjà disponibles
    if ohlcv_data is None:
        tickers = [p["ticker"] for p in positions]
        ohlcv_data = okx.get_all_ohlcv(tickers, days=90)

    tech_results = ts.run(ohlcv_data)

    # Régime de marché par ticker — une seule passe GARCH/HMM
    regime_results = {}
    for ticker in [p["ticker"] for p in positions]:
        df_ticker = ohlcv_data.get(ticker)
        if df_ticker is not None and not df_ticker.empty:
            try:
                regime_results[ticker] = rd.analyze(df_ticker)
            except Exception as e:
                logger.warning(f"Régime {ticker} : {e}")
                regime_results[ticker] = {}

    actions = []

    # Résumé des positions → Telegram toutes les 4h
    lines = ["📦 *Positions ouvertes*\n"]
    for pos in positions:
        ticker = pos["ticker"]
        pnl_str = f"{pos['pnl_pct']:+.1f}%" if pos['pnl_pct'] is not None else "N/A"
        emoji = "🟢" if (pos['pnl_pct'] or 0) > 0 else "🔴"
        tech = tech_results.get(ticker, {})
        score = tech.get("signal", {}).get("score", 0) if tech else 0
        reg = regime_results.get(ticker, {})
        reg_icon = {"bull": "📈", "bear": "📉", "sideways": "↔️"}.get(reg.get("regime", ""), "")
        lines.append(
            f"{emoji} *{ticker}* `${pos['prix_actuel']:.4f}` "
            f"P&L: `{pnl_str}` Score: `{score:+.2f}` {reg_icon}"
        )
    alertes.send("\n".join(lines))

    # Évaluation et exécution des décisions
    for pos in positions:
        ticker = pos["ticker"]
        tech = tech_results.get(ticker, {})
        regime_data = regime_results.get(ticker, {})

        if not tech or "erreur" in tech:
            logger.warning(f"{ticker} : pas de données techniques")
            continue

        decision = evaluate_position(pos, tech, regime_data)
        logger.info(
            f"{ticker} | P&L {decision['pnl_pct']:+.1f}% | "
            f"Score {decision['score']:+.2f} | Régime {decision.get('regime','?')} | "
            f"→ {decision['decision']}"
        )

        if decision["decision"] != "HOLD":
            execute_decision(decision, portfolio_value)
            actions.append(decision)

    return {"positions": positions, "actions": actions}
