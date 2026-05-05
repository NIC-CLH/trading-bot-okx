"""
Contextual Regime Enricher — enrichit la classification technique avec contexte narratif.

Le module régime existant (HMM/GARCH/ruptures) est purement technique.
Cet agent ajoute la dimension narrative : news macro, sentiment global,
contexte géopolitique/réglementaire que les indicateurs captent avec retard.

Exemple de valeur ajoutée :
  - Indicateurs disent "sideways" mais Fear&Greed=85 + ETF inflows
    + halving dans 30j → agent dit "bull_fragile en accumulation"
  - Indicateurs disent "bull" mais Fed hawkish + DXY en hausse
    → agent dit "bull_fragile, réduire les positions"

Point d'injection :
  - run_scan() dans scanner.py, appelé UNE FOIS par cycle avant la boucle
  - Le multiplicateur retourné s'applique au position_multiplier global
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from llm_client import get_client, is_available

logger = logging.getLogger(__name__)

MODEL      = "claude-haiku-4-5"
MAX_TOKENS = 400

_SYSTEM = """\
Tu es un macro-analyste crypto expert. Tu synthétises les conditions de marché
en combinant indicateurs quantitatifs ET contexte narratif pour classifier
le régime actuel et donner un biais directionnel.

Réponds UNIQUEMENT en JSON :
{
  "regime_narratif": "bull_confirmed|bull_fragile|sideways_accumulation|sideways_distribution|bear_early|bear_confirmed",
  "narratif": "synthèse en français max 200 chars",
  "biais": "acheteur|neutre|vendeur",
  "multiplicateur_adj": 1.0,
  "confiance": 0.0,
  "risques_principaux": ["risque1", "risque2"]
}

multiplicateur_adj : entre 0.6 (très prudent) et 1.25 (très confiant), 1.0 = neutre.
Sois conservateur : en cas de doute, reste proche de 1.0."""

_DEFAULT: dict = {
    "regime_narratif":  "sideways_accumulation",
    "narratif":         "Agent indisponible — régime neutre par défaut",
    "biais":            "neutre",
    "multiplicateur_adj": 1.0,
    "confiance":        0.0,
    "risques_principaux": [],
}


def enrich(
    technical_regime: dict,
    macro_data: dict,
    stablecoin_score: float = 0.0,
) -> dict:
    """
    Enrichit la classification de régime technique avec le contexte narratif.

    technical_regime : dict de regime_detector.analyze() (regime, vol_regime,
                       vol_annualized, position_multiplier)
    macro_data       : dict de macro_context.analyze() (score, verdict,
                       signals, dxy, dvol)
    stablecoin_score : score de stablecoin_dominance.analyze() [-1, +1]

    Retourne toujours un dict valide — jamais bloquant.
    Le multiplicateur_adj est clampé entre 0.6 et 1.25 pour la sécurité.
    """
    if not is_available():
        return dict(_DEFAULT)

    try:
        regime      = technical_regime.get("regime", "sideways")
        vol_regime  = technical_regime.get("vol_regime", "normal")
        vol_ann     = technical_regime.get("vol_annualized", 80)
        tech_mult   = technical_regime.get("position_multiplier", 1.0)

        macro_score   = macro_data.get("score", 0)
        macro_verdict = macro_data.get("verdict", "")
        macro_signals = macro_data.get("signals", [])
        dxy           = macro_data.get("dxy") or 104
        dvol          = macro_data.get("dvol") or 65

        macro_text = (
            "\n".join(f"- {s}" for s in macro_signals[:4])
            if macro_signals else macro_verdict or "Aucun signal macro disponible"
        )

        now = datetime.now(timezone.utc)

        prompt = (
            f"Date : {now.strftime('%d/%m/%Y %H:%M UTC')}\n"
            f"Régime technique : {regime} "
            f"(vol={vol_regime}, vol_ann={vol_ann:.0f}%, mult_tech={tech_mult})\n"
            f"Score macro quantitatif : {macro_score:+.2f}\n"
            f"DXY (dollar index) : {dxy:.1f}\n"
            f"DVOL (vol implicite BTC) : {dvol:.1f}\n"
            f"Dominance stablecoins (score) : {stablecoin_score:+.2f}\n\n"
            f"Signaux macro :\n{macro_text}\n\n"
            f"Synthétise le régime de marché crypto actuel. "
            f"Donne un biais directionnel et un ajustement du multiplicateur de position."
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
            # Sécurité : clamper le multiplicateur
            adj = float(result.get("multiplicateur_adj", 1.0))
            result["multiplicateur_adj"] = round(max(0.6, min(1.25, adj)), 2)

            logger.info(
                f"[RegimeCtx] {result.get('regime_narratif')} | "
                f"biais={result.get('biais')} | "
                f"mult_adj={result['multiplicateur_adj']:.2f} | "
                f"{result.get('narratif', '')[:80]}"
            )
            return {**_DEFAULT, **result}

        return dict(_DEFAULT)

    except Exception as e:
        logger.debug(f"[RegimeCtx] Agent indisponible : {e}")
        return dict(_DEFAULT)
