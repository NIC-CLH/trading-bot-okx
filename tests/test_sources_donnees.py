"""Sources de donnees secondaires : reparees ou declarees mortes (07/10/2026)."""
import sys
sys.path.insert(0, ".")

import inspect
from unittest.mock import patch

import social_radar as sr
import token_unlocks as tu


def test_social_radar_resout_les_ids_dynamiquement():
    """La table codee en dur ne couvrait que 30% de l'univers scanne."""
    src = inspect.getsource(sr)
    assert "_charger_table_ids" in src
    assert "market_cap_desc" in src, "le classement par cap tranche les collisions"


def test_social_radar_utilise_les_champs_encore_fournis():
    """CoinGecko a supprime community_data : on bascule sur sentiment + watchlist."""
    src = inspect.getsource(sr)
    assert "sentiment_votes_up_percentage" in src
    assert "watchlist_portfolio_users" in src


def test_social_radar_garde_un_repli_sur_les_anciens_champs():
    """Si CoinGecko les reactive, l'ancien calcul reprend."""
    src = inspect.getsource(sr._compute_score)
    assert "community_score" in src
    assert "reddit_accounts_active_48h" in src


def test_seuils_watchlist_calibres_sur_les_altcoins():
    """DYDX plafonne a 60k : des seuils a 500k/100k renvoyaient 0 partout."""
    src = inspect.getsource(sr._compute_score)
    assert "200_000" in src and "40_000" in src


def test_id_statique_prioritaire_sur_le_dynamique():
    with patch.object(sr, "_charger_table_ids", return_value={"SOL": "faux-id"}):
        assert sr._get_coingecko_id("SOL") == sr._TICKER_TO_ID["SOL"]


def test_id_dynamique_en_repli():
    with patch.object(sr, "_charger_table_ids", return_value={"ZZZ": "zzz-coin"}):
        assert sr._get_coingecko_id("ZZZ") == "zzz-coin"


def test_token_unlocks_signale_son_absence_de_source():
    """Aucune source active : le filtre ne doit pas passer pour une protection."""
    tu._warned = False
    with patch.object(tu.logger, "warning") as mock_warn:
        tu.check_unlock("SOL")
        tu.check_unlock("ZK")
    assert mock_warn.call_count == 1, "le warning doit sortir une seule fois"
    tu._warned = False


def test_token_unlocks_ne_bloque_rien():
    """Sans donnees, il doit rendre un verdict neutre et non bloquant."""
    r = tu.check_unlock("SOL")
    assert r["has_unlock"] is False
    assert r["score"] == 0.0
