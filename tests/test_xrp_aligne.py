"""Le module XRP applique les règles validées par le backtest (aligné 28/07/2026)."""
import sys
sys.path.insert(0, ".")

import inspect

import alert_scanner as a


def _src():
    return inspect.getsource(a.scan_xrp_binance)


def test_plafond_de_score_applique():
    """Un score au-delà de 2.8 ne doit plus déclencher d'alerte d'achat."""
    src = _src()
    assert "SCORE_MAX_EXEC" in src
    assert "is_buy_signal = False" in src


def test_fondamentaux_verifies():
    """Un signal purement technique ne suffit plus."""
    assert 'ns.analyze("XRP")' in _src()


def test_coherence_prix_signal():
    """Pas d'alerte 'tendance haussière' un jour de forte baisse."""
    src = _src()
    assert "price_move_pct <= -XRP_PRICE_MOVE_PCT" in src


def test_stop_aligne_sur_le_bot_principal():
    """Le stop vient de position_manager (ATR×1.5 borné), plus d'ATR×2 nu."""
    src = _src()
    assert "_pm.get_atr_stop" in src
    assert "atr * 2" not in src, "l'ancien stop ATR×2 subsiste"


def test_message_affiche_la_variation_du_jour():
    """Le message doit montrer le mouvement du jour, pas seulement la tendance."""
    assert "Variation du jour" in _src()


def test_bande_coherente_avec_le_scanner():
    """Seuil bas et plafond identiques au reste du système."""
    import scanner
    assert a.XRP_BUY_THRESHOLD == 2.0
    assert a.SCORE_MAX_EXEC == scanner.SCORE_MAX_EXEC == 2.8
