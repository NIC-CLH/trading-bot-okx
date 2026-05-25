"""
Capital Allocator — cerveau de l'allocation des positions.

Principe : la qualité du signal détermine le budget alloué.
Signal faible (1.5) → 12% du portfolio
Signal fort (2.0)   → 17% du portfolio
Signal exceptionnel (2.5+) → 22% du portfolio

La mémoire historique (ruflo) ajuste ce budget :
  - Win rate >= 70% sur >= 3 trades → +25% (ticker prouvé)
  - Win rate < 30% sur >= 3 trades  → -50% (ticker à risque)

Rotation : si USDC < 60% du budget cible ET score >= 2.0,
           cherche la position la plus faible à liquider.
"""
from __future__ import annotations  # compatibilité Python 3.9 (dict | None)

import logging
import time

logger = logging.getLogger(__name__)

# ── Tiers d'allocation par score ──────────────────────────────────────────────
# score_abs → % du portfolio TOTAL alloué à ce trade
SCORE_TIERS = [
    (2.5, 0.22),   # score >= 2.5 : 22% (signal exceptionnel)
    (2.0, 0.17),   # score >= 2.0 : 17% (signal fort)
    (1.5, 0.12),   # score >= 1.5 : 12% (signal valide)
]

# Seuil score minimum pour déclencher une rotation
ROTATION_SCORE_MIN = 2.0        # était 2.5 dans scanner.py — abaissé
ROTATION_USDC_RATIO = 0.60      # rotation si USDC < 60% du budget cible

# Ajustements selon EV rolling
EV_MODE_SIZE_MULT = {
    "conservative": 0.80,
    "normal":       1.00,
    "aggressive":   1.10,
}

# Seuil de données mémoire minimales pour ajuster le sizing
MIN_MEMORY_TRADES = 3


def _get_base_pct(score_abs: float) -> float:
    """Retourne le pourcentage de portfolio à allouer selon le score."""
    for threshold, pct in SCORE_TIERS:
        if score_abs >= threshold:
            return pct
    return 0.12  # fallback


def _get_memory_multiplier(
    ticker: str,
    context: dict | None = None,
) -> tuple[float, str]:
    """
    Ajuste le sizing selon la mémoire historique (deux niveaux) :

    1. Ticker-specific (>= MIN_MEMORY_TRADES=3 trades) — ajustement fort : +25% / -50%
    2. Pattern cross-ticker (>= 5 trades similaires) — ajustement conservateur : +15% / -25%

    context : {"score": float, "regime": str, "vol_regime": str}
    """
    try:
        import ruflo_memory as rm

        # ── Niveau 1 : historique exact du ticker ────────────────────────────
        mem = rm.get_ticker_memory(ticker, min_samples=MIN_MEMORY_TRADES)
        nb  = mem.get("nb_trades", 0)
        wr  = mem.get("win_rate")

        if nb >= MIN_MEMORY_TRADES and wr is not None:
            if wr >= 0.70:
                return 1.25, f"ticker: {wr:.0%} win rate ({nb} trades) → +25%"
            elif wr < 0.30:
                return 0.50, f"ticker: {wr:.0%} win rate ({nb} trades) → -50%"
            else:
                return 1.0, f"ticker: {wr:.0%} win rate ({nb} trades) → neutre"

        # ── Niveau 2 : pattern cross-ticker (fallback) ───────────────────────
        if context:
            pattern = rm.get_pattern_memory(
                score      = context.get("score", 0),
                regime     = context.get("regime", "unknown"),
                vol_regime = context.get("vol_regime", "normal"),
                min_samples= 5,
            )
            p_wr = pattern.get("win_rate")
            p_nb = pattern.get("nb_trades", 0)

            if p_wr is not None:
                if p_wr >= 0.70:
                    return 1.15, f"pattern: {p_wr:.0%} win rate ({p_nb} trades similaires) → +15%"
                elif p_wr < 0.30:
                    return 0.75, f"pattern: {p_wr:.0%} win rate ({p_nb} trades similaires) → -25%"
                else:
                    return 1.0, f"pattern: {p_wr:.0%} win rate ({p_nb} trades similaires) → neutre"

        return 1.0, f"mémoire insuffisante ({nb} trades ticker, pas assez de patterns)"

    except Exception as e:
        logger.debug(f"ruflo_memory indisponible : {e}")
        return 1.0, "ruflo indisponible"


def _find_rotation_candidate(
    incoming_ticker: str,
    open_positions: list[dict],
) -> dict | None:
    """
    Trouve la position la plus faible à liquider pour financer un signal fort.

    Critères :
    - Prix d'entrée connu (on ne vend pas à l'aveugle)
    - P&L < +3% (on protège les positions en profit significatif ≥ +3%)
    - Valeur >= $10 (la vente doit vraiment libérer du capital utile)
    - Pas le même actif que le signal entrant

    La position avec le P&L le plus bas est choisie (stagnante ou en perte).
    """
    candidates = [
        p for p in open_positions
        if p.get("prix_entree") is not None
        and (p.get("pnl_pct") or 0) < 3.0   # Protège les positions ≥ +3% de la rotation
        and p.get("valeur_usd", 0) >= 10.0
        and p.get("ticker") != incoming_ticker
    ]

    if not candidates:
        return None

    # Priorité : P&L le plus faible (pertes > stagnation > gains faibles)
    return min(candidates, key=lambda p: p.get("pnl_pct") or 0)


def calculate_allocation(
    ticker: str,
    score: float,
    portfolio_value: float,
    usdc_available: float,
    open_positions: list[dict] | None = None,
    context: dict | None = None,
) -> dict:
    """
    Calcule la taille optimale d'une position selon le score, le portfolio et la mémoire.

    Paramètres :
        ticker          : ex. "BTC"
        score           : score composite final (signé, ex. +2.3)
        portfolio_value : valeur totale du portefeuille en USD
        usdc_available  : USDC réellement disponibles sur OKX
        open_positions  : liste de positions ouvertes (pour la rotation)

    Retourne :
        taille_allouee  : taille en USD à utiliser pour le trade
        rotation_needed : True si une rotation est nécessaire
        rotation_candidate : dict de la position à vendre (ou None)
        base_pct        : % du portfolio (avant ajustements)
        mem_multiplier  : multiplicateur mémoire appliqué
        reasoning       : string explicatif pour les logs
    """
    if open_positions is None:
        open_positions = []

    score_abs = abs(score)
    base_pct  = _get_base_pct(score_abs)

    # ── Multiplicateur mémoire (ruflo) ───────────────────────────────────────
    mem_mult, mem_msg = _get_memory_multiplier(ticker, context)

    # ── Ajustement EV rolling ────────────────────────────────────────────────
    try:
        from ruflo_memory import get_rolling_ev
        ev_data      = get_rolling_ev()
        ev_mode      = ev_data.get("mode", "normal")
        ev_size_mult = EV_MODE_SIZE_MULT.get(ev_mode, 1.0)
        if ev_mode != "normal":
            logger.info(
                f"[Allocateur] Mode EV={ev_mode} "
                f"(EV={ev_data.get('ev')}%/trade, WR={ev_data.get('wr', 0) or 0:.0%}) "
                f"→ taille ×{ev_size_mult}"
            )
    except Exception:
        ev_size_mult = 1.0

    # ── Taille cible (portfolio total × % × mémoire × EV) ───────────────────
    target_usdt = portfolio_value * base_pct * mem_mult * ev_size_mult

    # Plafond absolu : jamais plus de 25% du portfolio par trade ─────────────
    # Empêche le multiplicateur mémoire (×1.25) de pousser le tier 22% à 27.5%.
    # 25% = marge raisonnable au-dessus du tier max (22%) pour récompenser
    # les tickers prouvés sans risquer une concentration excessive.
    HARD_CAP_PCT = 0.25
    target_usdt = min(target_usdt, portfolio_value * HARD_CAP_PCT)

    # Plafond USDC : ne jamais engager plus que ce qu'on a réellement ────────
    capped_usdt = min(target_usdt, usdc_available * 0.95)

    # ── Rotation : faut-il libérer du capital ? ──────────────────────────────
    rotation_needed    = False
    rotation_candidate = None

    if score_abs >= ROTATION_SCORE_MIN:
        shortfall_ratio = usdc_available / target_usdt if target_usdt > 0 else 1.0
        if shortfall_ratio < ROTATION_USDC_RATIO:
            # USDC < 60% du budget cible → chercher une position à liquider
            rotation_candidate = _find_rotation_candidate(ticker, open_positions)
            if rotation_candidate:
                rotation_needed = True
                logger.info(
                    f"[Allocateur] {ticker} : budget cible ${target_usdt:.0f} "
                    f"mais USDC ${usdc_available:.0f} ({shortfall_ratio:.0%} < 60%) "
                    f"→ rotation candidate : {rotation_candidate['ticker']} "
                    f"(P&L {rotation_candidate.get('pnl_pct', 0):+.1f}%, ${rotation_candidate['valeur_usd']:.0f})"
                )

    reasoning = (
        f"score={score:+.2f} → tier={base_pct:.0%} "
        f"× mémoire={mem_mult:.2f} ({mem_msg}) "
        f"→ cible=${target_usdt:.0f} "
        f"→ après cap USDC=${capped_usdt:.0f}"
        f"{' [ROTATION]' if rotation_needed else ''}"
    )

    logger.info(f"[Allocateur] {ticker} : {reasoning}")

    return {
        "taille_allouee":       round(capped_usdt, 2),
        "target_usdt":          round(target_usdt, 2),   # budget cible avant cap USDC
        "rotation_needed":      rotation_needed,
        "rotation_candidate":   rotation_candidate,
        "base_pct":             base_pct,
        "mem_multiplier":       mem_mult,
        "reasoning":            reasoning,
    }


def execute_rotation(rotation_candidate: dict, incoming_signal: dict) -> bool:
    """
    Vend la position candidate pour libérer du capital.
    Envoie une alerte Telegram et attend 3s pour le settlement OKX.
    """
    import okx_client as okx
    import alertes

    ticker_out = rotation_candidate.get("ticker")
    qty        = rotation_candidate.get("qty", 0)
    if not ticker_out or qty <= 0:
        logger.error(f"execute_rotation : rotation_candidate invalide {rotation_candidate}")
        return False
    pnl_str = f"{rotation_candidate.get('pnl_pct', 0):+.1f}%"
    valeur  = rotation_candidate.get("valeur_usd", 0)

    # ── Seul l'ordre OKX peut faire échouer la rotation ─────────────────────
    # alertes.send et ruflo sont non-bloquants : leur échec ne doit pas
    # masquer une vente OKX réussie (ce qui causerait un USDC mal recalculé).
    try:
        result = okx.place_order(
            ticker=ticker_out,
            side="sell",
            quantity=qty,  # 100% — plus de buffer qui laisse du dust
            order_type="market",
        )
    except Exception as e:
        logger.error(f"Rotation {ticker_out} échouée (OKX) : {e}")
        return False

    ordre_id = result.get("ordId", "?")

    # Notification Telegram (non-bloquante)
    try:
        alertes.send(
            f"🔄 *ROTATION*\n"
            f"Signal entrant : *{incoming_signal['ticker']}* "
            f"score `{incoming_signal['score']:+.2f}`\n"
            f"Position libérée : *{ticker_out}* "
            f"`${valeur:.2f}` ({pnl_str})\n"
            f"ID : `{ordre_id}`"
        )
    except Exception:
        pass

    logger.info(
        f"Rotation : {ticker_out} vendu (ordre {ordre_id}) → capital libéré "
        f"pour {incoming_signal['ticker']}"
    )

    # Mémoire ruflo (non-bloquante)
    try:
        import ruflo_memory as rm
        rm.store_trade_outcome({
            "ticker":    ticker_out,
            "pnl_pct":   rotation_candidate.get("pnl_pct"),
            "days_held": rotation_candidate.get("days_held"),
            "raison":    f"Rotation — capital libéré pour {incoming_signal['ticker']}",
            "valeur":    valeur,
        })
    except Exception:
        pass

    time.sleep(3)  # OKX settlement
    return True
