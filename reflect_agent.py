"""
Reflect Agent — analyse chaque trade clôturé et extrait les leçons.

Basé sur arXiv 2510.08068 (Adaptive Multi-Agent BTC) :
un agent réflexif qui critique les décisions passées en langage naturel
et stocke les patterns d'erreur en mémoire vectorielle (ruflo).

Points d'injection :
  - position_manager.py → analyze_trade() après store_trade_outcome()

Influence sur les décisions futures :
  - Les réflexions sont stockées dans ruflo et consultables via get_ticker_memory()
  - Elles enrichissent le contexte des prochaines analyses du même ticker
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from llm_client import get_client, is_available

logger = logging.getLogger(__name__)

MODEL      = "claude-haiku-4-5"
MAX_TOKENS = 400

_SYSTEM = """\
Tu es un analyste de trading crypto expert. Tu analyses des trades clôturés \
pour en extraire des leçons concrètes et améliorer les décisions futures.

Réponds UNIQUEMENT en JSON valide avec cette structure :
{
  "lecon_principale": "string court max 100 chars",
  "signal_fiable": "quel signal était le plus prédictif",
  "signal_trompeur": "quel signal était faux ou trompeur",
  "regime_optimal": "quel régime convient le mieux à ce ticker",
  "recommandation": "hold_longer|exit_earlier|reduce_size|increase_size|avoid_regime",
  "confiance": 0.0
}"""


def analyze_trade(decision: dict, entry_context: dict | None = None) -> dict:
    """
    Analyse un trade clôturé et extrait les leçons.

    decision      : dict de position_manager (ticker, pnl_pct, days_held,
                    exit_reason/raison, valeur_usd)
    entry_context : dict ruflo_memory (score, regime, vol_regime à l'entrée)

    Retourne le dict de réflexion, ou {} si l'agent est indisponible.
    Jamais bloquant — toutes les exceptions sont absorbées.
    """
    if not is_available():
        return {}

    try:
        ticker     = decision.get("ticker", "UNK")
        pnl        = decision.get("pnl_pct", 0)
        days       = decision.get("days_held", 0) or 0
        exit_r     = decision.get("exit_reason", decision.get("raison", "inconnu"))
        outcome    = "WIN" if pnl > 0 else "LOSS"

        score_e  = entry_context.get("score", "?")      if entry_context else "?"
        regime_e = entry_context.get("regime", "?")     if entry_context else "?"
        vol_e    = entry_context.get("vol_regime", "?") if entry_context else "?"

        prompt = (
            f"Trade {outcome} sur {ticker} :\n"
            f"- P&L : {pnl:+.1f}% en {days:.1f} jours\n"
            f"- Raison de sortie : {exit_r}\n"
            f"- Score composite à l'entrée : {score_e}\n"
            f"- Régime à l'entrée : {regime_e}\n"
            f"- Volatilité à l'entrée : {vol_e}\n\n"
            f"Analyse ce trade et extrais les leçons pour les prochaines décisions."
        )

        client = get_client()
        msg    = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )

        text = msg.content[0].text.strip()
        idx  = text.find("{")
        end  = text.rfind("}") + 1
        reflection = json.loads(text[idx:end]) if idx >= 0 and end > idx else {
            "lecon_principale": text[:100],
            "confiance": 0.5,
        }

        reflection.update({
            "ticker":    ticker,
            "pnl_pct":  pnl,
            "type":     "trade_reflection",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        logger.info(
            f"[Reflect] {ticker} {outcome} ({pnl:+.1f}%) : "
            f"{reflection.get('lecon_principale', '')}"
        )

        # ── Persistance ruflo + JSON ──────────────────────────────────────────
        try:
            import ruflo_memory as rm
            key = f"reflect:{ticker}:{int(time.time() * 1000)}"
            rm._ruflo_store(key, reflection)

            data = rm._load_json()
            data.setdefault("reflections", [])
            data["reflections"] = data["reflections"][-100:]
            data["reflections"].append(reflection)
            rm._save_json(data)
        except Exception as _e:
            logger.debug(f"[Reflect] ruflo store ignoré : {_e}")

        return reflection

    except Exception as e:
        logger.debug(f"[Reflect] Agent indisponible : {e}")
        return {}
