"""
Backfill des entrées manquantes pour les positions actuellement ouvertes.

Problème : les positions ouvertes avant la mise en place du système de mémoire
(trade_memory.json) n'ont aucun enregistrement d'entrée. Quand elles seront
vendues, store_trade_outcome() ne trouvera pas de contexte (régime, score,
vol_regime) → les outcomes seront incomplets → l'apprentissage cross-ticker
ne fonctionnera pas correctement.

Ce script résout ça une seule fois :
  1. Lit les positions ouvertes via position_manager.get_open_positions()
  2. Skip les tickers déjà enregistrés dans entries[]
  3. Calcule le score technique actuel pour chaque position
  4. Détecte le régime de marché actuel
  5. Injecte des entries "backfill" dans trade_memory.json

Usage : python backfill_open_entries.py
"""

import io
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

import okx_client as okx
import position_manager as pm
import technical_signals as ts
import ruflo_memory as rm


def get_current_regime() -> str:
    """Détecte le régime de marché actuel via BTC 50MA."""
    try:
        df = okx.get_ohlcv("BTC", days=60)
        if df is None or df.empty or len(df) < 50:
            return "unknown"
        ma50  = df["close"].rolling(50).mean().iloc[-1]
        price = df["close"].iloc[-1]
        return "bull" if price > ma50 else "bear"
    except Exception:
        return "unknown"


def get_vol_regime(ticker: str) -> str:
    """
    Évalue le régime de volatilité du ticker.
    Ratio ATR-14 / prix pour estimer si c'est calme, normal ou volatile.
    """
    try:
        df = okx.get_ohlcv(ticker, days=20)
        if df is None or df.empty or len(df) < 15:
            return "normal"

        high  = df["high"].values
        low   = df["low"].values
        close = df["close"].values

        tr_list = []
        for i in range(1, len(close)):
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i]  - close[i - 1]),
            )
            tr_list.append(tr)

        if len(tr_list) < 14:
            return "normal"

        atr   = sum(tr_list[-14:]) / 14
        price = close[-1]
        atr_pct = atr / price * 100  # ATR en % du prix

        if atr_pct < 2.0:
            return "low"
        elif atr_pct > 5.0:
            return "high"
        else:
            return "normal"
    except Exception:
        return "normal"


def run():
    print(f"\n{'='*60}")
    print(f"  BACKFILL — Entrées positions ouvertes")
    print(f"{'='*60}\n")

    # ── Lire l'état actuel de la mémoire ─────────────────────────────────────
    data = rm._load_json()
    already_entered = {e.get("ticker") for e in data.get("entries", [])}
    logger.info(f"Entrées déjà connues : {already_entered or 'aucune'}")

    # ── Positions ouvertes ────────────────────────────────────────────────────
    positions = pm.get_open_positions()
    if not positions:
        print("Aucune position ouverte détectée.")
        return

    tickers = [p["ticker"] for p in positions]
    print(f"Positions ouvertes : {tickers}")

    # ── Régime de marché (commun à toutes les positions) ─────────────────────
    regime = get_current_regime()
    logger.info(f"Régime BTC 50MA : {regime}")

    # ── Scores techniques par batch ───────────────────────────────────────────
    scores_map: dict[str, dict] = {}
    try:
        ohlcv_all = okx.get_all_ohlcv(tickers, days=90)
        tech_all  = ts.run(ohlcv_all)
        for ticker, tech in tech_all.items():
            sig = tech.get("signal", {})
            # ts.run() retourne uniquement le score technique (signal.score).
            # score_news / score_ms / score_macro non calculés → 0.0 (backfill acceptable).
            scores_map[ticker] = {
                "score":       sig.get("score", 0.0),
                "score_tech":  sig.get("score", 0.0),
                "score_news":  0.0,
                "score_ms":    0.0,
                "score_macro": 0.0,
            }
        logger.info(f"Scores calculés : { {t: f\"{s['score']:+.2f}\" for t, s in scores_map.items()} }")
    except Exception as e:
        logger.warning(f"Calcul scores échoué ({e}) — scores à 0 utilisés pour le backfill")

    # ── Injection des entrées manquantes ──────────────────────────────────────
    added   = 0
    skipped = 0

    for pos in positions:
        ticker  = pos["ticker"]
        entree  = pos.get("prix_entree")
        valeur  = pos.get("valeur_usd", 0)

        if ticker in already_entered:
            logger.info(f"{ticker} : entrée déjà présente — skip")
            skipped += 1
            continue

        if not entree:
            logger.warning(f"{ticker} : prix d'entrée inconnu — skip (position orpheline)")
            skipped += 1
            continue

        scores = scores_map.get(ticker, {})
        vol_regime = get_vol_regime(ticker)

        # Format identique à store_trade_entry()
        entry = {
            "type":        "trade_entry",
            "ticker":      ticker,
            "score":       round(scores.get("score", 0.0), 4),
            "score_tech":  round(scores.get("score_tech", 0.0), 4),
            "score_news":  0.0,
            "score_ms":    round(scores.get("score_ms", 0.0), 4),
            "score_macro": round(scores.get("score_macro", 0.0), 4),
            "regime":      regime,
            "vol_regime":  vol_regime,
            "taille_usd":  round(valeur, 2),
            "prix":        entree,
            "btc_uptrend": regime != "bear",
            "backfill":    True,   # marqueur pour distinguer des vraies entrées
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }

        data["entries"].append(entry)
        already_entered.add(ticker)
        added += 1

        pnl_str = f"{pos['pnl_pct']:+.1f}%" if pos.get("pnl_pct") is not None else "N/A"
        logger.info(
            f"Backfill {ticker} : prix_entree={entree:.6f} valeur=${valeur:.2f} "
            f"P&L={pnl_str} score={scores.get('score', 0):+.2f} régime={regime}/{vol_regime}"
        )

    # ── Persister ─────────────────────────────────────────────────────────────
    if added > 0:
        rm._save_json(data)
        print(f"\n✅ {added} entrée(s) backfillée(s) dans trade_memory.json")
        if skipped:
            print(f"   {skipped} ticker(s) skippé(s) (déjà présents ou orphelins)")

        # Recharger ruflo depuis le JSON mis à jour
        try:
            rm.seed_ruflo_from_json()
            print("   Ruflo vector DB mis à jour.")
        except Exception as e:
            logger.warning(f"Ruflo seed ignoré : {e}")
    else:
        print(f"\n⚠️  Aucune nouvelle entrée à backfiller ({skipped} skip(s)).")

    # ── Résumé final ──────────────────────────────────────────────────────────
    data_final = rm._load_json()
    print(f"\n{'─'*60}")
    print(f"  trade_memory.json — état final")
    print(f"{'─'*60}")
    print(f"  Outcomes  : {len(data_final['outcomes'])}")
    print(f"  Entries   : {len(data_final['entries'])}")
    for e in data_final["entries"]:
        tag = " [backfill]" if e.get("backfill") else ""
        print(
            f"  └─ {e['ticker']:10} score={e['score']:+.2f} "
            f"régime={e.get('regime','?')}/{e.get('vol_regime','?')}{tag}"
        )
    print(f"{'─'*60}\n")


if __name__ == "__main__":
    run()
