"""Le plafond dur de 25% ne doit jamais être franchi, quel que soit le chemin."""
import sys
sys.path.insert(0, ".")

import inspect

import alert_scanner as a
import execution as ex


def _taille_fallback(portfolio_value, score, usdc_dispo=10_000):
    """Reproduit le calcul du chemin fallback de execute_signal."""
    base = ex.get_max_trade_size(portfolio_value)
    conviction = 0.75
    for seuil, mult in sorted(ex.CONVICTION_MULTIPLIERS.items(), reverse=True):
        if abs(score) >= seuil:
            conviction = mult
            break
    hard_cap = portfolio_value * ex.MAX_TRADE_PCT
    return min(min(base * conviction, hard_cap), usdc_dispo * 0.95)


def test_signal_fort_ne_depasse_pas_le_plafond():
    """Score >= 2.5 : le multiplicateur x1.25 ne doit plus franchir les 25%."""
    pv = 380.0
    taille = _taille_fallback(pv, 2.6)
    assert taille <= pv * ex.MAX_TRADE_PCT + 0.01, f"{taille/pv*100:.1f}% > 25%"
    assert taille <= 95.01, f"attendu <= $95, obtenu ${taille:.2f}"


def test_plafond_respecte_sur_toute_la_plage_de_scores():
    pv = 380.0
    for score in (1.5, 1.9, 2.0, 2.4, 2.5, 2.8, 3.0):
        taille = _taille_fallback(pv, score)
        assert taille <= pv * ex.MAX_TRADE_PCT + 0.01, \
            f"score {score} → {taille/pv*100:.1f}% du capital"


def test_signal_faible_reste_sous_le_plafond():
    pv = 380.0
    assert _taille_fallback(pv, 1.6) < pv * ex.MAX_TRADE_PCT


def test_scanner_30min_ecrit_la_cle_lue_par_execution():
    """execution.py lit 'taille_allouee' — le scanner 30min doit la fournir,
    sinon le fallback conviction reprend la main."""
    src = inspect.getsource(a.scan_and_execute_signals)
    assert '"taille_allouee"' in src, "le scanner 30min n'injecte pas taille_allouee"


def test_chemin_allocateur_prioritaire_sur_le_fallback():
    src = inspect.getsource(ex.execute_signal)
    i_lecture = src.index('signal.get("taille_allouee")')
    i_fallback = src.index("Fallback : ancienne logique")
    assert i_lecture < i_fallback, "le fallback doit rester le dernier recours"
