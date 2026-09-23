"""Les dimensions de scoring doivent renvoyer de vraies valeurs (pas des zeros muets).

Contexte : le health check du 23/09/2026 a revele que microstructure (12%) et
Coinglass (8%) etaient figees a zero. Une dimension morte ne contribue pas
zero, elle rabote 20% du score total : un signal valant 2.20 etait calcule
a 1.76 et ratait le seuil de 2.0.
"""
import sys
sys.path.insert(0, ".")

import inspect

import coinglass_data as cg
import market_microstructure as mm


def test_microstructure_utilise_le_bon_parametre_okx():
    """OKX rubik attend 'ccy' ; 'instId' renvoie un HTTP 400."""
    src = inspect.getsource(mm)
    for fn in ("long-short-account-ratio", "taker-volume"):
        i = src.index(fn)
        bloc = src[i:i + 320]
        assert '"ccy"' in bloc, f"{fn} : parametre ccy manquant"
        assert '"instId"' not in bloc, f"{fn} : instId provoque un HTTP 400"


def test_microstructure_lit_des_tableaux_pas_des_objets():
    """OKX renvoie [ts, valeur], pas {'longShortRatio': ...}."""
    src = inspect.getsource(mm)
    assert '.get("longShortRatio"' not in src
    assert '.get("buyVol"' not in src


def test_coinglass_ne_depend_plus_des_endpoints_morts():
    """fapi.coinglass.com renvoie 404 et l'API v3 exige une cle payante."""
    src = inspect.getsource(cg)
    assert "fapi.coinglass.com" not in src
    assert "_okx_stat" in src, "Coinglass doit s'appuyer sur les stats OKX gratuites"


def test_coinglass_remonte_sa_disponibilite():
    """analyze() doit distinguer 'neutre' de 'source indisponible'."""
    src = inspect.getsource(cg.analyze)
    assert '"disponible"' in src


def test_collecteurs_renvoient_disponible_false_si_vide():
    """Sans donnees, chaque collecteur signale l'indisponibilite plutot qu'un faux neutre."""
    for fn in (cg.get_long_short_ratio, cg.get_open_interest, cg.get_liquidations_24h):
        src = inspect.getsource(fn)
        assert '"disponible": False' in src, f"{fn.__name__} ne signale pas l'indisponibilite"
