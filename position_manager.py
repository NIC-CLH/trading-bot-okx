"""
Gestionnaire de positions — stratégie exits fixes.

Philosophie : une fois en position, le bot est SOURD aux signaux.
On ne sort QUE sur trois événements prédéfinis à l'entrée :
  1. Stop fixe    : -7% du prix d'entrée (absorbe le bruit crypto normal)
  2. Objectif fixe: +12% du prix d'entrée (sortie complète)
  3. Temps max    : 7 jours CONFIRMÉS — si ni stop ni objectif → on sort

Ce qui est SUPPRIMÉ :
  - Sorties basées sur le score technique (whipsawing)
  - Rotations actives (vendre X pour acheter Y)
  - Trailing stop complexe
  - Vente partielle à +7% (causait un loop : sold 50% every 4h cycle until dust)
  - Vente automatique si prix d'entrée inconnu (trop dangereux)

Seul le stop dur en prix et le temps décident de la sortie.
"""
from __future__ import annotations  # compatibilité Python 3.9 (float | None)

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
STOP_LOSS_PCT        = -0.07   # -7%  → conservé pour compatibilité (non utilisé par P1)
ATR_STOP_MULTIPLIER  =  1.5    # stop = prix_entrée - ATR_14 * 1.5
ATR_STOP_MIN_PCT     =  0.04   # plafond  : jamais plus de -4%  (stop serré)
ATR_STOP_MAX_PCT     =  0.10   # plancher : jamais moins de -10% (stop large)
FULL_PROFIT_PCT      =  0.12   # +12% → sortie complète (base)
STRONG_PROFIT_PCT    =  0.20   # +20% → sortie si signal encore fort au moment du target
STRONG_SCORE_MIN     =  2.0    # score minimum pour étendre le target à +20%
MAX_HOLDING_DAYS     =  7      # 7 jours max en position, quelle que soit la situation
MAX_POSITIONS        =  4
MIN_POSITION_VALUE   =  5.0    # ignorer les poussières < $5 (restes de vieux trades)
ORPHAN_AUTO_SELL_MAX = 20.0   # position orpheline (entrée inconnue) auto-vendue si < $20


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

        # Filtrer les poussières — trop petit pour être géré ou vendu
        valeur_usd = qty * prix_actuel
        if valeur_usd < MIN_POSITION_VALUE:
            logger.debug(f"{ticker} ignoré (poussière ${valeur_usd:.2f} < ${MIN_POSITION_VALUE})")
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
            "valeur_usd":  valeur_usd,
            "pnl_pct":     pnl_pct,
            "pnl_usd":     pnl_usd,
        })

    return positions


def _get_entry_info(ticker: str) -> dict:
    """
    Prix d'entrée + timestamp depuis les fills OKX (90j max), fallback SQLite, fallback accAvgPx.

    Sanity check appliqué à CHAQUE source : si le prix donne P&L < -50%,
    il est rejeté comme aberrant (vieux fills hors contexte ou accAvgPx corrompu).
    Si toutes les sources échouent → {"price": None, "time": None}.
    """
    # ── Fills OKX — uniquement les 90 derniers jours ────────────────────────────
    try:
        cutoff_ms = (time_module.time() - 90 * 86400) * 1000  # 90j en ms
        data = okx._get("/api/v5/trade/fills", {
            "instId": f"{ticker.upper()}-USDC",
            "limit": "20",
        })
        if data:
            # Filtrer les fills récents ET les achats seulement
            buys = [
                f for f in data
                if f.get("side") == "buy"
                and int(f.get("ts", 0)) >= cutoff_ms
            ]
            if buys:
                total_qty  = sum(float(f["fillSz"]) for f in buys)
                total_cost = sum(float(f["fillSz"]) * float(f["fillPx"]) for f in buys)
                price      = total_cost / total_qty if total_qty > 0 else None
                timestamps = [int(f.get("ts", 0)) for f in buys if f.get("ts")]
                entry_ts   = max(timestamps) / 1000 if timestamps else None

                # Sanity check : P&L < -50% → fill aberrant, on rejette
                if price:
                    prix_actuel = okx.get_price_usdc(ticker)
                    if prix_actuel:
                        pnl_check = (prix_actuel - price) / price
                        if pnl_check < -0.50:
                            logger.warning(
                                f"{ticker} : fill price ${price:.4f} → P&L {pnl_check:.0%} aberrant "
                                f"— rejeté, fallback accAvgPx"
                            )
                            price = None

                if price:
                    return {"price": price, "time": entry_ts}
    except Exception:
        pass

    # ── SQLite (trades enregistrés par le système) ───────────────────────────────
    try:
        conn   = sqlite3.connect(config.DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT AVG(prix), MAX(timestamp) FROM (
                SELECT prix, timestamp FROM trades
                WHERE ticker = ? AND side = 'buy' AND statut != 'annulé'
                ORDER BY timestamp DESC LIMIT 5
            )
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

    # ── accAvgPx OKX — dernier recours, avec sanity check ───────────────────────
    # Utile pour les positions achetées avant le système ou dont les fills
    # sont trop anciens. Mais OKX peut garder un accAvgPx de vieux trades →
    # même sanity check -50% appliqué.
    try:
        avg_px = okx.get_avg_entry_price(ticker)
        if avg_px:
            prix_actuel = okx.get_price_usdc(ticker)
            if prix_actuel:
                pnl_check = (prix_actuel - avg_px) / avg_px
                if pnl_check < -0.50:
                    logger.warning(
                        f"{ticker} : accAvgPx ${avg_px:.6f} aberrant ({pnl_check:.0%}) "
                        f"— position orpheline, prix d'entrée définitivement inconnu"
                    )
                    return {"price": None, "time": None}
            logger.info(f"{ticker} : prix entrée via accAvgPx OKX = {avg_px:.6f}")
            return {"price": avg_px, "time": None}
    except Exception:
        pass

    return {"price": None, "time": None}


# Alias compatibilité
def _get_entry_price(ticker: str) -> float | None:
    return _get_entry_info(ticker).get("price")


# ── Stop ATR dynamique ───────────────────────────────────────────────────────

def get_atr_stop(ticker: str, prix_entree: float) -> float:
    """
    Calcule le prix de stop basé sur l'ATR 14 jours.

    Formule :
      - True Range  = max(high-low, |high-prev_close|, |low-prev_close|)
      - ATR_14      = moyenne des 14 derniers True Ranges
      - stop_atr    = prix_entrée - ATR_14 * ATR_STOP_MULTIPLIER
      - Plancher    : stop >= prix_entrée * (1 - ATR_STOP_MAX_PCT)  (max -10%)
      - Plafond     : stop >= prix_entrée * (1 - ATR_STOP_MIN_PCT)  (min -4%)

    En cas d'erreur → fallback sur prix_entrée * (1 - 0.07).
    """
    try:
        df = okx.get_ohlcv(ticker, days=20)
        if df is None or df.empty or len(df) < 15:
            raise ValueError(f"OHLCV insuffisant pour {ticker} ({0 if df is None else len(df)} bougies)")

        high  = df["high"].values
        low   = df["low"].values
        close = df["close"].values

        # True Range sur chaque bougie (hors la première, qui n'a pas de prev_close)
        tr_list = []
        for i in range(1, len(close)):
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i]  - close[i - 1]),
            )
            tr_list.append(tr)

        if len(tr_list) < 14:
            raise ValueError(f"Pas assez de True Ranges ({len(tr_list)}) pour ATR-14")

        atr = sum(tr_list[-14:]) / 14

        stop_atr = prix_entree - atr * ATR_STOP_MULTIPLIER

        # Plancher : stop ne peut pas être inférieur à -ATR_STOP_MAX_PCT
        stop = max(stop_atr, prix_entree * (1 - ATR_STOP_MAX_PCT))

        # Plafond : stop ne peut pas être supérieur à -ATR_STOP_MIN_PCT
        stop = min(stop, prix_entree * (1 - ATR_STOP_MIN_PCT))

        stop_pct = (stop - prix_entree) / prix_entree * 100
        logger.debug(
            f"{ticker} | ATR-14={atr:.4f} | stop_atr={stop_atr:.4f} "
            f"| stop final={stop:.4f} ({stop_pct:.1f}%)"
        )
        return stop

    except Exception as e:
        fallback = prix_entree * (1 - 0.07)
        logger.warning(f"{ticker} | get_atr_stop erreur ({e}) — fallback stop -7% = {fallback:.4f}")
        return fallback


# ── Évaluation ────────────────────────────────────────────────────────────────

def evaluate_position(pos: dict, score: float | None = None) -> dict:
    """
    Évalue une position avec exits fixes + extension dynamique sur signal fort.

    Décisions : HOLD | FULL_SELL

    Règles d'exit (par priorité) :
      P1 — Stop ATR dynamique : sortie immédiate si pnl <= stop ATR-based (entre -4% et -10%)
      P2 — Time stop 7j       : sortie si durée CONFIRMÉE >= 7 jours
      P3 — Objectif dynamique :
             score >= STRONG_SCORE_MIN (2.0) → target étendu à +20%
             score <  STRONG_SCORE_MIN       → target fixe +12% (comportement historique)

    Le paramètre `score` est calculé dans run() en batch sur toutes les positions.
    Si absent (None), P3 retombe sur le comportement +12% fixe.

    NOTE : si le prix d'entrée est inconnu, on NE vend PAS automatiquement.
    Le time stop P2 ne se déclenche que si le timestamp est CONNU (days_held != None).
    """
    ticker    = pos["ticker"]
    prix      = pos["prix_actuel"]
    entree    = pos["prix_entree"]
    pnl_pct   = pos.get("pnl_pct") or 0
    days_held = pos.get("days_held")
    valeur    = pos["valeur_usd"]

    # ── Filtre dust — position < $1 : non-vendable sur OKX, on ignore ─────────
    DUST_THRESHOLD_USD = 1.0
    if valeur is not None and valeur < DUST_THRESHOLD_USD:
        logger.debug(
            f"{ticker} | dust ignoré (valeur=${valeur:.3f} < ${DUST_THRESHOLD_USD}) "
            f"— convertir manuellement via OKX app > Small Assets Convert"
        )
        return {
            "ticker":   ticker,
            "decision": "DUST",
            "raison":   f"Poussière ${valeur:.3f} — convertir via app OKX",
            "urgence":  False,
            "pnl_pct":  pnl_pct,
            "pnl_usd":  pos.get("pnl_usd"),
            "valeur":   valeur,
            "qty":      pos["qty"],
            "days_held": days_held,
        }

    decision = "HOLD"
    raison   = ""
    urgence  = False

    # ── Prix d'entrée inconnu : gestion des positions orphelines ─────────────
    # Une position orpheline = prix d'entrée introuvable dans tous les systèmes.
    # Règle :
    #   - Valeur < ORPHAN_AUTO_SELL_MAX ($20) → vente automatique (nettoyage)
    #   - Valeur >= $20 → alerte Telegram + HOLD (l'utilisateur décide)
    if not entree:
        if valeur < ORPHAN_AUTO_SELL_MAX:
            logger.warning(
                f"{ticker} | Orphelin ${valeur:.2f} — vente automatique (nettoyage)"
            )
            return {
                "ticker":   ticker,
                "decision": "FULL_SELL",
                "raison":   f"Position orpheline (entrée inconnue) — nettoyage automatique",
                "urgence":  False,
                "pnl_pct":  None,
                "pnl_usd":  None,
                "valeur":   valeur,
                "qty":      pos["qty"],
                "days_held": days_held,
            }
        else:
            logger.warning(
                f"{ticker} | Orphelin ${valeur:.2f} — HOLD (> $20, action manuelle requise)"
            )
            # Alerte non-bloquante : si Telegram plante, on continue l'évaluation
            try:
                alertes.send(
                    f"⚠️ *Position orpheline : {ticker}*\n"
                    f"Valeur : `${valeur:.2f}` | Entrée inconnue\n"
                    f"_Le bot ne peut pas évaluer le P&L — vérifie et vends manuellement si nécessaire._"
                )
            except Exception:
                pass
        # On continue avec pnl_pct=0 : le stop ne se déclenchera pas.

    # ── P1 : Stop ATR dynamique (seulement si on connaît le prix d'entrée) ──────
    # NOTE : utilise `if` seul (pas elif) pour que P2/P3 puissent s'évaluer
    # indépendamment. Chaque règle vérifie `decision == "HOLD"` avant d'agir.
    if entree:
        stop_price = get_atr_stop(pos["ticker"], entree)
        stop_pct   = (stop_price - entree) / entree * 100
        if pnl_pct <= stop_pct:
            decision = "FULL_SELL"
            raison   = f"Stop ATR déclenché ({pnl_pct:.1f}% < {stop_pct:.1f}%)"
            urgence  = True

    # ── P2 : Time stop — 7 jours CONFIRMÉS uniquement ────────────────────────
    # Conditions : P1 non déclenché + entrée connue + timestamp connu.
    if (decision == "HOLD"
            and entree
            and days_held is not None
            and days_held >= MAX_HOLDING_DAYS
            and pnl_pct < FULL_PROFIT_PCT * 100):
        decision = "FULL_SELL"
        raison   = f"Time stop ({days_held:.0f}j) — P&L {pnl_pct:+.1f}% — on libère le capital"

    # ── P3 : Objectif atteint → sortie fixe ou étendue selon le signal ──────────
    if decision == "HOLD" and entree and pnl_pct >= FULL_PROFIT_PCT * 100:
        if score is not None and score >= STRONG_SCORE_MIN:
            # Signal encore fort → laisser courir jusqu'au target étendu +20%
            if pnl_pct < STRONG_PROFIT_PCT * 100:
                raison = (
                    f"Target étendu — signal fort ({score:+.2f} >= {STRONG_SCORE_MIN}) "
                    f"| {pnl_pct:+.1f}% / +{STRONG_PROFIT_PCT*100:.0f}% cible"
                )
                # decision reste HOLD
            else:
                decision = "FULL_SELL"
                raison   = (
                    f"Target étendu +{STRONG_PROFIT_PCT*100:.0f}% atteint "
                    f"({pnl_pct:+.1f}%) — signal {score:+.2f} 🚀"
                )
        else:
            # Signal faible ou inconnu → sortie classique à +12%
            score_str = f" (score {score:+.2f})" if score is not None else ""
            decision  = "FULL_SELL"
            raison    = f"Objectif +12% atteint ({pnl_pct:+.1f}%){score_str} 🎯"

    if decision == "HOLD" and not raison:
        days_str   = f"{days_held:.1f}j" if days_held else "?"
        entree_str = f"entree=${entree:.4f} " if entree else "entree=? "
        raison     = f"En cours ({entree_str}{pnl_pct:+.1f}% | {days_str} / 7j)"

    return {
        "ticker":    ticker,
        "decision":  decision,   # HOLD | FULL_SELL
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

    if action == "DUST":
        return False  # Rien à faire — dust ignoré

    if action == "FULL_SELL":
        # ── Seul l'ordre OKX peut faire échouer la vente ────────────────────
        # alertes.send et ruflo sont non-bloquants : leur échec ne doit pas
        # masquer un ordre OKX réussi ni empêcher store_trade_outcome d'être appelé.

        # Récupérer la balance ACTUELLE (fraîche depuis OKX) pour éviter
        # de vendre une quantité obsolète. En cas d'erreur → fallback sur qty du snapshot.
        try:
            fresh_balances = okx.get_balances()
            fresh_qty = fresh_balances.get(ticker.upper(), qty)
            if fresh_qty > 0:
                qty = fresh_qty
        except Exception:
            pass  # On garde la qty du snapshot si l'appel OKX échoue

        try:
            result = okx.place_order(
                ticker=ticker, side="sell",
                quantity=qty,  # 100% — pas de buffer qui laisse du dust
                order_type="market",
            )
        except Exception as e:
            logger.error(f"Erreur vente {ticker} : {e}")
            try:
                alertes.send(f"❌ Échec vente {ticker} : {str(e)[:120]}")
            except Exception:
                pass
            return False

        ordre_id = result.get("ordId", "?")
        pnl_str  = f"{decision['pnl_pct']:+.1f}%" if decision["pnl_pct"] is not None else "N/A"
        emoji    = "🟢" if (decision["pnl_pct"] or 0) >= 0 else "🔴"
        days_str = f"{decision['days_held']:.1f}j" if decision.get("days_held") else ""
        logger.info(f"SELL {ticker} — {decision['raison']} | P&L {pnl_str} | ordre {ordre_id}")

        # Notification Telegram (non-bloquante)
        try:
            alertes.send(
                f"{emoji} *VENTE {ticker}*{f' ({days_str})' if days_str else ''}\n"
                f"Raison : {decision['raison']}\n"
                f"Valeur : `${decision['valeur']:.2f}`\n"
                f"P&L : `{pnl_str}`\n"
                f"ID : `{ordre_id}`"
            )
        except Exception:
            pass

        # Mémoriser le résultat du trade pour l'apprentissage (non-bloquant)
        try:
            import ruflo_memory as rm
            rm.store_trade_outcome(decision)
        except Exception as _e:
            logger.debug(f"ruflo_memory store_outcome ignoré : {_e}")

        # Reflect Agent LLM — analyse le trade clôturé et extrait les leçons
        try:
            import reflect_agent
            import ruflo_memory as _rm
            _json_data = _rm._load_json()
            entries = [e for e in _json_data.get("entries", [])
                       if e.get("ticker") == decision.get("ticker")]
            entry_ctx = entries[-1] if entries else None
            reflect_agent.analyze_trade(decision, entry_ctx)
        except Exception as _e:
            logger.debug(f"reflect_agent ignoré : {_e}")

        return True

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

def sweep_dust() -> list[str]:
    """
    Liquide automatiquement toutes les positions < MIN_POSITION_VALUE ($3).
    Ce sont des résidus créés par d'anciens bugs (partial sell loop, trades minuscules).
    On les vend au marché sans condition — récupérer les cents et nettoyer le portefeuille.
    """
    try:
        balances = okx.get_balances()
    except Exception as e:
        logger.error(f"sweep_dust : erreur balances : {e}")
        return []

    stablecoins = {"USDC", "USDT", "BUSD", "DAI"}
    swept = []

    for ticker, qty in balances.items():
        if ticker in stablecoins or qty <= 0:
            continue

        prix = okx.get_price_usdc(ticker)
        if not prix:
            continue

        valeur = qty * prix
        if valeur >= MIN_POSITION_VALUE:
            continue  # pas une poussière

        # Poussière confirmée — vente marché
        try:
            result   = okx.place_order(ticker=ticker, side="sell",
                                        quantity=qty, order_type="market")
            ordre_id = result.get("ordId", "?")
            logger.info(f"Dust sweep : {ticker} ${valeur:.2f} vendu (ordre {ordre_id})")
            swept.append(ticker)
        except Exception as e:
            logger.warning(f"Dust sweep {ticker} échoué : {e}")

    if swept:
        try:
            alertes.send(
                f"🧹 *Nettoyage poussières* : {', '.join(swept)}\n"
                f"_Résidus < ${MIN_POSITION_VALUE} liquidés automatiquement._"
            )
        except Exception:
            pass

    return swept


def run(portfolio_value: float, **kwargs) -> dict:
    """
    Gestion complète des positions toutes les 4h.
    Sorties basées sur stops/objectifs/temps + extension dynamique sur signal fort.
    """
    logger.info("Gestion des positions (exits : ATR stop / +12% base / +20% si signal fort / 7j)...")

    # Nettoyage des poussières en premier (résidus < $3)
    sweep_dust()

    positions = get_open_positions()
    if not positions:
        logger.info("Aucune position ouverte.")
        return {"positions": [], "actions": [], "btc_uptrend": is_btc_uptrend()}

    # ── Calcul des scores techniques pour toutes les positions ────────────────
    # Permet à evaluate_position() d'étendre le target si signal encore fort.
    scores_map: dict[str, float] = {}
    try:
        tickers_open = [p["ticker"] for p in positions]
        ohlcv_pos    = okx.get_all_ohlcv(tickers_open, days=90)
        tech_pos     = ts.run(ohlcv_pos)
        for ticker, tech in tech_pos.items():
            scores_map[ticker] = tech.get("signal", {}).get("score", 0.0)
        logger.info(
            f"Scores positions : "
            f"{ {t: f'{s:+.2f}' for t, s in scores_map.items()} }"
        )
    except Exception as e:
        logger.warning(f"Calcul scores positions échoué ({e}) — exits fixes appliqués")

    actions      = []
    danger_lines = []

    for pos in positions:
        score     = scores_map.get(pos["ticker"])
        decision  = evaluate_position(pos, score=score)
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
            if pnl <= -5.0 and pos.get("prix_entree"):
                emoji = "🔴" if pnl < -6 else "🟠"
                danger_lines.append(
                    f"{emoji} *{pos['ticker']}* `${pos['prix_actuel']:.4f}` "
                    f"P&L `{pnl:+.1f}%` — stop à -7%"
                )

    if danger_lines:
        try:
            alertes.send(
                "⚠️ *Positions proches du stop*\n\n"
                + "\n".join(danger_lines)
                + "\n\n_Stop automatique à -7% — surveille si tu veux couper avant._"
            )
        except Exception:
            pass

    return {"positions": positions, "actions": actions}
