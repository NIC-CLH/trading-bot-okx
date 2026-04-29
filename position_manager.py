"""
Gestionnaire de positions — stratégie rotation rapide.

Objectif : faire grossir le portfolio le plus vite possible en
bondissant de signal en signal, prise de profit en prise de profit.

Règles :
  - Stop dur   : -4%  → sortie immédiate, sans condition
  - Stop souple: -2%  + score négatif → sortie défensive
  - Profit rapide : +5% → vendre 50% (laisser courir)
               +8% → sortir complètement si momentum s'essouffle
  - Time stop  : 36h sans atteindre +1% → rotation vers mieux
  - Rotation active : si meilleur signal disponible (écart >= 1.2)
    ET position stagnante (<2% gain) → on switche

Frais OKX : 0.1% par trade → 0.2% aller-retour.
Chaque trade doit viser +1% net minimum pour être rentable.
"""

import logging
import time as time_module
import sqlite3
from datetime import datetime, timezone

import okx_client as okx
import technical_signals as ts
import regime_detector as rd
import alertes
import config

logger = logging.getLogger(__name__)

# ── Stratégie agressive ───────────────────────────────────────────────────────
HARD_PRICE_STOP_PCT    = -0.04   # -4%  → coupe automatiquement, sans condition
SOFT_PRICE_STOP_PCT    = -0.02   # -2%  + score négatif → sortie défensive
PARTIAL_PROFIT_PCT     =  0.05   # +5%  → prendre 50% des gains
FULL_PROFIT_PCT        =  0.08   # +8%  → sortir totalement si momentum faible
TRAILING_STOP_TRIGGER  =  0.05   # +5%  → activer le trailing stop
TRAILING_STOP_DISTANCE =  0.02   # 2%   trailing (court, pour garder les gains)
STOP_SIGNAL_EXIT       = -0.8    # Score → sortie défensive (était -1.0)
HARD_SIGNAL_EXIT       = -1.5    # Score → sortie urgente (était -1.8)

# Time stop — position stagnante
MAX_HOLDING_HOURS      = 36      # 36h max sans progresser
STAGNANT_PCT_THRESHOLD =  1.0   # Si P&L < +1% après 24h → candidat rotation

# Rotation active
ROTATION_SCORE_GAP     =  1.2   # Écart score pour justifier une rotation
ROTATION_MAX_PNL       =  3.0   # Ne pas tourner si position déjà à +3% ou plus

# Frais
FEE_ROUND_TRIP_PCT     =  0.20  # 0.1% × 2 = 0.2% aller-retour OKX
MIN_NET_GAIN_PCT       =  1.0   # Gain net minimum pour qu'un trade vaille le coup

MAX_POSITIONS = 4
MIN_USDC_RESERVE_PCT = 0.05


# ── Données d'entrée ──────────────────────────────────────────────────────────

def get_open_positions() -> list[dict]:
    """Retourne les positions ouvertes avec P&L et timestamp d'entrée."""
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
        entry_info = _get_entry_info(ticker)
        prix_entree = entry_info.get("price")
        entry_ts    = entry_info.get("time")

        pnl_pct = ((prix_actuel - prix_entree) / prix_entree * 100) if prix_entree else None
        pnl_usd = (prix_actuel - prix_entree) * qty if prix_entree else None

        hours_held = None
        if entry_ts:
            hours_held = (time_module.time() - entry_ts) / 3600

        positions.append({
            "ticker":      ticker,
            "qty":         qty,
            "prix_actuel": prix_actuel,
            "prix_entree": prix_entree,
            "entry_ts":    entry_ts,
            "hours_held":  hours_held,
            "valeur_usd":  valeur,
            "pnl_pct":     pnl_pct,
            "pnl_usd":     pnl_usd,
        })

    return positions


def _get_entry_info(ticker: str) -> dict:
    """Prix d'entrée + timestamp depuis les fills OKX."""
    # 1. OKX fills API
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

    # 2. Fallback SQLite
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
                from datetime import datetime
                dt = datetime.fromisoformat(row[1])
                entry_ts = dt.timestamp()
            except Exception:
                pass
            return {"price": float(row[0]), "time": entry_ts}
    except Exception:
        pass

    return {"price": None, "time": None}


# Alias pour compatibilité
def _get_entry_price(ticker: str) -> float | None:
    return _get_entry_info(ticker).get("price")


def _get_highest_price_since_entry(ticker: str, entry_price: float) -> float:
    """Prix le plus haut depuis l'entrée (pour trailing stop)."""
    try:
        df = okx.get_ohlcv(ticker, days=10)
        if df.empty:
            return entry_price
        return max(float(df["high"].max()), entry_price)
    except Exception:
        return entry_price


def _get_regime_adjusted_thresholds(regime_data: dict) -> dict:
    """
    Ajuste les seuils selon le régime HMM + volatilité GARCH.
    En BEAR ou volatilité extrême : encore plus agressif sur les stops.
    """
    regime    = regime_data.get("regime", "sideways")
    vol_regime = regime_data.get("vol_regime", "normal")
    recent_break = regime_data.get("recent_break", False)

    hard_signal     = HARD_SIGNAL_EXIT
    soft_signal     = STOP_SIGNAL_EXIT
    hard_price      = HARD_PRICE_STOP_PCT
    soft_price      = SOFT_PRICE_STOP_PCT
    partial_profit  = PARTIAL_PROFIT_PCT
    full_profit     = FULL_PROFIT_PCT
    trailing_trigger = TRAILING_STOP_TRIGGER

    if regime == "bear":
        hard_signal     = -1.0
        soft_signal     = -0.5
        hard_price      = -0.03   # -3% en régime bear
        soft_price      = -0.015
        partial_profit  = 0.03    # Prendre profit dès +3%
        full_profit     = 0.05    # Sortir complètement dès +5%
        trailing_trigger = 0.03

    if vol_regime in ("elevated", "extreme"):
        hard_price       = min(hard_price, -0.03)
        soft_price       = min(soft_price, -0.015)
        partial_profit   = min(partial_profit, 0.04)
        trailing_trigger = min(trailing_trigger, 0.03)

    if recent_break:
        soft_signal = min(soft_signal, -0.4)

    return {
        "hard_signal":       hard_signal,
        "soft_signal":       soft_signal,
        "hard_price_pct":    hard_price * 100,
        "soft_price_pct":    soft_price * 100,
        "partial_profit_pct": partial_profit * 100,
        "full_profit_pct":   full_profit * 100,
        "trailing_trigger_pct": trailing_trigger * 100,
    }


# ── Évaluation ────────────────────────────────────────────────────────────────

def evaluate_position(pos: dict, tech: dict, regime_data: dict = None) -> dict:
    """
    Évalue une position et retourne la décision.
    Décisions : HOLD | PARTIAL_SELL | FULL_SELL | TRAILING_STOP
    """
    ticker    = pos["ticker"]
    prix      = pos["prix_actuel"]
    entree    = pos["prix_entree"]
    pnl_pct   = pos.get("pnl_pct") or 0
    valeur    = pos["valeur_usd"]
    hours_held = pos.get("hours_held")

    sig     = tech.get("signal", {})
    score   = sig.get("score", 0)
    verdict = sig.get("verdict", "")

    t = _get_regime_adjusted_thresholds(regime_data or {})
    regime_name = (regime_data or {}).get("regime", "sideways")
    reg_suffix  = f" [{regime_name}]" if regime_data else ""

    decision = "HOLD"
    raison   = ""
    urgence  = False

    # ── P1 : Stop dur en prix (-4%) ──────────────────────────────────────────
    if entree and pnl_pct <= t["hard_price_pct"]:
        decision = "FULL_SELL"
        raison   = f"Stop dur ({pnl_pct:.1f}%){reg_suffix}"
        urgence  = True

    # ── P2 : Stop souple (-2% + score négatif) ───────────────────────────────
    elif entree and pnl_pct <= t["soft_price_pct"] and score < 0:
        decision = "FULL_SELL"
        raison   = f"Stop souple ({pnl_pct:.1f}% + score {score:+.2f}){reg_suffix}"
        urgence  = True

    # ── P3 : Signal très baissier ────────────────────────────────────────────
    elif score <= t["hard_signal"]:
        decision = "FULL_SELL"
        raison   = f"Signal fort baissier (score {score:+.2f}){reg_suffix}"
        urgence  = True

    # ── P4 : Sortie défensive (score négatif + position marginale) ────────────
    elif score <= t["soft_signal"] and pnl_pct < 2:
        decision = "FULL_SELL"
        raison   = f"Signal négatif ({score:+.2f}) + gain insuffisant{reg_suffix}"

    # ── P5 : Time stop (36h sans progresser) ─────────────────────────────────
    elif hours_held and hours_held >= MAX_HOLDING_HOURS and pnl_pct < STAGNANT_PCT_THRESHOLD:
        decision = "FULL_SELL"
        raison   = f"Stagnation {hours_held:.0f}h (P&L {pnl_pct:+.1f}%) — rotation"

    # ── P6 : Prise de profit totale (+8% + momentum faible) ──────────────────
    elif pnl_pct >= t["full_profit_pct"] and score <= 0.5:
        decision = "FULL_SELL"
        raison   = f"Profit sécurisé (+{pnl_pct:.1f}%) — momentum s'essouffle{reg_suffix}"

    # ── P7 : Prise de profit partielle (+5%) ─────────────────────────────────
    elif pnl_pct >= t["partial_profit_pct"] and score > 0:
        decision = "PARTIAL_SELL"
        raison   = f"Prise de profit partielle (+{pnl_pct:.1f}%){reg_suffix}"

    # ── P8 : Trailing stop ───────────────────────────────────────────────────
    elif pnl_pct >= t["trailing_trigger_pct"] and entree:
        plus_haut = _get_highest_price_since_entry(ticker, entree)
        trailing_stop = plus_haut * (1 - TRAILING_STOP_DISTANCE)
        if prix < trailing_stop:
            decision = "FULL_SELL"
            raison   = f"Trailing stop (${prix:.4f} < ${trailing_stop:.4f}){reg_suffix}"

    if decision == "HOLD":
        raison = f"Signal {score:+.2f} — {verdict}{reg_suffix}"

    return {
        "ticker":    ticker,
        "decision":  decision,
        "raison":    raison,
        "urgence":   urgence,
        "score":     score,
        "pnl_pct":   pnl_pct,
        "pnl_usd":   pos.get("pnl_usd"),
        "valeur":    valeur,
        "qty":       pos["qty"],
        "hours_held": hours_held,
        "regime":    regime_name,
    }


# ── Rotation active ────────────────────────────────────────────────────────────

def find_rotation_candidates(positions: list[dict], tech_results_positions: dict,
                              new_signals: dict) -> list[dict]:
    """
    Compare les positions actuelles aux nouveaux signaux.
    Retourne les couples (position_à_vendre, ticker_à_acheter) justifiant une rotation.

    Conditions pour rotater :
    - Écart de score >= ROTATION_SCORE_GAP (1.2 points)
    - Position actuelle : P&L < ROTATION_MAX_PNL (pas de gain déjà important à couper)
    - Nouveau signal : score >= 1.5 (signal fort)
    - Gain attendu après frais > MIN_NET_GAIN_PCT
    """
    rotations = []

    for pos in positions:
        ticker     = pos["ticker"]
        pnl_pct    = pos.get("pnl_pct") or 0
        score_pos  = tech_results_positions.get(ticker, {}).get("signal", {}).get("score", 0)

        # Ne pas toucher une position déjà bien en profit
        if pnl_pct >= ROTATION_MAX_PNL:
            continue

        # Trouver le meilleur signal disponible (pas déjà en position)
        pos_tickers = {p["ticker"] for p in positions}
        for new_ticker, new_score in new_signals.items():
            if new_ticker in pos_tickers:
                continue
            if new_score < 1.5:
                continue

            ecart = new_score - score_pos
            if ecart >= ROTATION_SCORE_GAP:
                # Vérifier rentabilité nette : gain attendu - frais aller-retour
                gain_attendu_pct = new_score * 1.5   # approximation : score 2.0 → ~3% espéré
                net_apres_frais  = gain_attendu_pct - FEE_ROUND_TRIP_PCT * 2  # exit + entry
                if net_apres_frais >= MIN_NET_GAIN_PCT:
                    rotations.append({
                        "sell_ticker":  ticker,
                        "sell_score":   score_pos,
                        "sell_pnl":     pnl_pct,
                        "buy_ticker":   new_ticker,
                        "buy_score":    new_score,
                        "ecart":        ecart,
                        "net_gain_est": net_apres_frais,
                    })
                    break  # Une rotation par position max

    # Trier par écart décroissant (rotation la plus rentable en premier)
    rotations.sort(key=lambda x: x["ecart"], reverse=True)
    return rotations


# ── Exécution ──────────────────────────────────────────────────────────────────

def execute_decision(decision: dict, portfolio_value: float) -> bool:
    """Exécute la décision de gestion sur OKX."""
    ticker = decision["ticker"]
    action = decision["decision"]
    qty    = decision["qty"]

    if action == "FULL_SELL":
        try:
            result = okx.place_order(
                ticker=ticker,
                side="sell",
                quantity=qty * 0.999,
                order_type="market",
            )
            ordre_id = result.get("ordId", "?")
            pnl_str  = f"{decision['pnl_pct']:+.1f}%" if decision["pnl_pct"] is not None else "N/A"
            emoji    = "🟢" if (decision["pnl_pct"] or 0) > 0 else "🔴"
            hours    = f" ({decision['hours_held']:.0f}h)" if decision.get("hours_held") else ""

            alertes.send(
                f"{emoji} *VENTE {ticker}*{hours} (OKX)\n"
                f"Raison : {decision['raison']}\n"
                f"Quantité : `{qty:.4f} {ticker}`\n"
                f"Valeur : `${decision['valeur']:.2f}`\n"
                f"P&L : `{pnl_str}`\n"
                f"ID : `{ordre_id}`"
            )
            logger.info(f"SELL {ticker} — {decision['raison']} | P&L {pnl_str}")
            return True
        except Exception as e:
            logger.error(f"Erreur vente {ticker} : {e}")
            alertes.send(f"❌ Échec vente {ticker} : {str(e)[:100]}")
            return False

    elif action == "PARTIAL_SELL":
        qty_sell = qty * 0.50
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
                f"Quantité : `{qty_sell:.4f} {ticker}`\n"
                f"P&L : `{decision['pnl_pct']:+.1f}%`\n"
                f"ID : `{ordre_id}`"
            )
            logger.info(f"PARTIAL SELL {ticker} — {decision['raison']}")
            return True
        except Exception as e:
            logger.error(f"Erreur vente partielle {ticker} : {e}")
            return False

    return False  # HOLD


# ── Point d'entrée principal ──────────────────────────────────────────────────

def run(portfolio_value: float, ohlcv_data: dict = None,
        new_signal_scores: dict = None) -> dict:
    """
    Gestion complète des positions toutes les 4h.

    new_signal_scores : dict {ticker: score} des meilleurs signaux détectés
                        ce cycle — utilisé pour la rotation active.
    """
    logger.info("Gestion des positions (stratégie rotation rapide)...")

    positions = get_open_positions()
    if not positions:
        logger.info("Aucune position ouverte.")
        return {"positions": [], "actions": []}

    # Données techniques
    tickers = [p["ticker"] for p in positions]
    if ohlcv_data is None:
        ohlcv_data = okx.get_all_ohlcv(tickers, days=60)
    tech_results = ts.run(ohlcv_data)

    # Régime de marché
    regime_results = {}
    for ticker in tickers:
        df_ticker = ohlcv_data.get(ticker)
        if df_ticker is not None and not df_ticker.empty:
            try:
                regime_results[ticker] = rd.analyze(df_ticker)
            except Exception as e:
                logger.warning(f"Régime {ticker} : {e}")
                regime_results[ticker] = {}

    actions      = []
    danger_lines = []
    sold_tickers = set()

    # ── Évaluation individuelle de chaque position ────────────────────────────
    for pos in positions:
        ticker     = pos["ticker"]
        tech       = tech_results.get(ticker, {})
        regime_data = regime_results.get(ticker, {})

        if not tech or "erreur" in tech:
            logger.warning(f"{ticker} : pas de données techniques")
            continue

        decision = evaluate_position(pos, tech, regime_data)
        pnl  = decision["pnl_pct"] or 0
        score = decision["score"]
        held  = f"{decision['hours_held']:.0f}h" if decision.get("hours_held") else "?"

        logger.info(
            f"{ticker} | P&L {pnl:+.1f}% | Score {score:+.2f} | "
            f"Tenu {held} | Régime {decision.get('regime','?')} | "
            f"→ {decision['decision']}"
        )

        if decision["decision"] != "HOLD":
            success = execute_decision(decision, portfolio_value)
            if success:
                actions.append(decision)
                sold_tickers.add(ticker)
        else:
            # Zone de danger sans stop déclenché
            is_danger = (
                pos["prix_entree"] is not None
                and (pnl <= -2.5 or (score <= -0.5 and pnl < 0))
            )
            if is_danger:
                emoji = "🔴" if pnl < -3 else "🟠"
                reg_icon = {"bull": "📈", "bear": "📉", "sideways": "↔️"}.get(
                    regime_results.get(ticker, {}).get("regime", ""), ""
                )
                danger_lines.append(
                    f"{emoji} *{ticker}* `${pos['prix_actuel']:.4f}` "
                    f"P&L `{pnl:+.1f}%` Score `{score:+.2f}` {reg_icon}"
                )

    # ── Rotation active ────────────────────────────────────────────────────────
    if new_signal_scores:
        positions_restantes = [p for p in positions if p["ticker"] not in sold_tickers]
        if positions_restantes:
            rotations = find_rotation_candidates(
                positions_restantes, tech_results, new_signal_scores
            )
            for rot in rotations:
                sell_t = rot["sell_ticker"]
                buy_t  = rot["buy_ticker"]
                logger.info(
                    f"Rotation : vendre {sell_t} (score {rot['sell_score']:+.2f}, "
                    f"P&L {rot['sell_pnl']:+.1f}%) → acheter {buy_t} "
                    f"(score {rot['buy_score']:+.2f}, écart {rot['ecart']:+.2f})"
                )
                # Trouver la position à vendre
                pos_a_vendre = next((p for p in positions_restantes if p["ticker"] == sell_t), None)
                if not pos_a_vendre:
                    continue
                rot_decision = {
                    "ticker":    sell_t,
                    "decision":  "FULL_SELL",
                    "raison":    f"Rotation → {buy_t} (écart score {rot['ecart']:+.2f})",
                    "urgence":   False,
                    "score":     rot["sell_score"],
                    "pnl_pct":   rot["sell_pnl"],
                    "pnl_usd":   pos_a_vendre.get("pnl_usd"),
                    "valeur":    pos_a_vendre["valeur_usd"],
                    "qty":       pos_a_vendre["qty"],
                    "hours_held": pos_a_vendre.get("hours_held"),
                    "regime":    regime_results.get(sell_t, {}).get("regime", "sideways"),
                }
                success = execute_decision(rot_decision, portfolio_value)
                if success:
                    actions.append(rot_decision)
                    sold_tickers.add(sell_t)

    # ── Alertes danger ────────────────────────────────────────────────────────
    if danger_lines:
        msg  = "⚠️ *Positions à surveiller*\n\n" + "\n".join(danger_lines)
        msg += "\n\n_Stop automatique déclenche à -4% — surveille si tu veux intervenir avant._"
        alertes.send(msg)

    return {"positions": positions, "actions": actions}
