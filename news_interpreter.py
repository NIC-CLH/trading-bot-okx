"""
News/Event Interpreter — interprétation causale des news pour un ticker.

Différent du sentiment pur (score numérique) : produit une classification
d'impact causal avec horizon temporel estimé. Peut bloquer un signal si
un événement fondamental majeur contredit le score technique.

Cas typiques détectés :
  - Hack / exploit du protocole → bloquer immédiatement
  - Enquête SEC / régulateur → bloquer ou réduire
  - Listing exchange majeur → renforcer le signal haussier
  - Partenariat stratégique → renforcer
  - Fondateurs qui vendent → signal négatif

Point d'injection :
  - scanner.py Phase 1, après le calcul des scores, avant la décision finale
"""
from __future__ import annotations

import json
import logging

from llm_client import get_client, is_available

logger = logging.getLogger(__name__)

MODEL      = "claude-haiku-4-5"
MAX_TOKENS = 300

_SYSTEM = """\
Tu es un analyste crypto spécialisé dans l'interprétation d'événements fondamentaux.
Tu identifies si une news récente valide ou invalide un signal technique d'achat.

Réponds UNIQUEMENT en JSON :
{
  "impact": "bullish|bearish|neutral|invalid",
  "raison": "string max 150 chars — raison causale principale",
  "horizon_h": 24,
  "confiance": 0.0,
  "bloquer": false
}

Règles :
- "invalid" uniquement si un event MAJEUR contredit le signal (hack, exploit, enquête, rug)
- "bloquer": true uniquement si impact="invalid" ET confiance > 0.70
- Sois conservateur : doute = "neutral", pas "invalid"
- horizon_h = durée estimée de l'impact en heures (6, 12, 24, 48, 72, 168)"""

_DEFAULT = {
    "impact":    "neutral",
    "raison":    "agent indisponible",
    "horizon_h": 24,
    "confiance": 0.0,
    "bloquer":   False,
}


def interpret(ticker: str, news_data: dict, score_context: dict) -> dict:
    """
    Interprète l'impact causal d'une news sur un signal ticker.

    ticker        : ex. "AAVE"
    news_data     : dict de news_sentiment.analyze() (score_global, verdict,
                    news_signals, fear_greed)
    score_context : {"score": float, "score_tech": float, "regime": str}

    Retourne toujours un dict valide — jamais d'exception levée.
    """
    if not is_available():
        return dict(_DEFAULT)

    try:
        news_signals  = news_data.get("news_signals", [])
        verdict_news  = news_data.get("verdict", "")
        fear_greed    = news_data.get("fear_greed", {})
        score_news    = news_data.get("score_global", 0)
        score_final   = score_context.get("score", 0)
        regime        = score_context.get("regime", "sideways")

        # Si aucune news disponible, neutre par défaut — pas d'appel LLM
        if not news_signals and not verdict_news:
            return {**_DEFAULT, "raison": "aucune news disponible"}

        news_text = (
            "\n".join(f"- {s}" for s in news_signals[:5])
            if news_signals else verdict_news
        )
        fg_val = fear_greed.get("value", "?") if isinstance(fear_greed, dict) else "?"

        prompt = (
            f"Ticker : {ticker}\n"
            f"Score composite du signal : {score_final:+.2f}\n"
            f"Score news quantitatif : {score_news:+.2f}\n"
            f"Régime de marché : {regime}\n"
            f"Fear & Greed index : {fg_val}/100\n\n"
            f"News récentes :\n{news_text}\n\n"
            f"Y a-t-il un événement fondamental qui valide ou invalide "
            f"ce signal d'achat ?"
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
        if idx >= 0 and end > idx:
            result = json.loads(text[idx:end])
            # Sécurité : bloquer seulement si vraiment confiant
            if result.get("impact") != "invalid":
                result["bloquer"] = False

            logger.info(
                f"[NewsInterp] {ticker} : {result.get('impact')} "
                f"(conf={result.get('confiance', 0):.2f}, "
                f"bloquer={result.get('bloquer', False)}) — "
                f"{result.get('raison', '')[:60]}"
            )
            return {**_DEFAULT, **result}

        return dict(_DEFAULT)

    except Exception as e:
        logger.debug(f"[NewsInterp] {ticker} agent indisponible : {e}")
        return dict(_DEFAULT)
