"""Take profit etendu et detection de stop intraseance (07/10/2026).

Contexte mesure sur la periode 23/09-07/10 :
- 4 TP sur 9 sortis entre +12.2% et +12.8%, pile au declenchement. Apres
  sortie, IOTA a continue de +18.7%, HUMA de +7.8%, ENJ de +3.8%.
- 4 stops sur 7 ont depasse la limite de -10% : -13.0, -11.2, -10.5, -10.1.
"""
import sys
sys.path.insert(0, ".")

import inspect
from unittest.mock import patch

import alert_scanner as a
import position_manager as pm


def test_cible_etendue_definie():
    assert a.TP_PCT == 12.0
    assert a.TP_EXTENDED_PCT == 20.0
    assert a.TP_EXTENDED_PCT > a.TP_PCT


def test_le_tp_30min_consulte_le_score():
    """Avant, le TP etait fixe a +12% sans regarder le signal : la regle
    d'extension du cycle 4h ne pouvait jamais s'appliquer."""
    src = inspect.getsource(a.emergency_stop_check)
    assert "_score_technique" in src
    assert "STRONG_SCORE_MIN" in src


def test_le_trailing_protege_une_position_en_tp_etendu():
    """Le trailing doit rester atteignable apres un TP etendu : avec un `elif`
    une position laissee courir n'aurait plus eu aucun filet."""
    src = inspect.getsource(a.emergency_stop_check)
    i_tp = src.index("P1 : Take profit")
    i_trail = src.index("P2 : Trailing stop")
    bloc = src[i_tp:i_trail + 400]
    assert "elif peak_pnl >= TRAIL_ACTIVATE" not in bloc, "le trailing est court-circuite"
    assert "if sell_reason is None and peak_pnl >= TRAIL_ACTIVATE" in bloc


def test_score_indisponible_vend_au_tp_de_base():
    """Sans score, on prend le profit a +12% : comportement prudent."""
    with patch.object(a.okx, "get_all_ohlcv", side_effect=Exception("api morte")):
        assert a._score_technique("SOL") is None


def test_plus_bas_recent_lit_la_meche_basse():
    """OKX renvoie [ts, open, high, low, close, ...] : le low est en index 3."""
    bougies = [
        ["1", "10", "11", "8.5", "10"],
        ["2", "10", "12", "9.2", "11"],
    ]
    with patch.object(a.okx, "_get", return_value=bougies):
        assert a._plus_bas_recent("SOL") == 8.5


def test_plus_bas_recent_tolere_une_api_muette():
    with patch.object(a.okx, "_get", return_value=[]):
        assert a._plus_bas_recent("SOL") is None
    with patch.object(a.okx, "_get", side_effect=Exception("timeout")):
        assert a._plus_bas_recent("SOL") is None


def test_stop_declenche_sur_meche_meme_si_le_prix_est_remonte():
    """Coeur du correctif : le stop doit partir si le plus bas l'a touche,
    meme si le prix courant est repasse au-dessus."""
    src = inspect.getsource(a.emergency_stop_check)
    assert "_plus_bas_recent" in src
    assert "touche_bas <= stop_price" in src
