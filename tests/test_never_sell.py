"""Interdiction absolue de vente sur les holdings perso (decision Nico 07/10/2026).

Le verrou est pose dans okx_client.place_order, point de passage obligatoire de
tout ordre. Les protections metier precedentes (execute_decision,
emergency_stop_check) ne couvraient ni la rotation ni les ventes d'urgence.
"""
import sys
sys.path.insert(0, ".")

from unittest.mock import patch

import pytest

import config
import okx_client as okx


def test_xrp_est_protege():
    assert "XRP" in config.NEVER_SELL


def test_vente_refusee_avant_tout_appel_reseau():
    """Aucun ordre ne doit partir chez OKX pour un ticker protege."""
    with patch.object(okx, "_post") as mock_post, \
         patch.object(okx, "_get", return_value=[]):
        with pytest.raises(PermissionError, match="NEVER_SELL"):
            okx.place_order(ticker="XRP", side="sell", quantity=100.0)
    mock_post.assert_not_called()


def test_vente_refusee_quelle_que_soit_la_casse():
    with patch.object(okx, "_post") as mock_post, \
         patch.object(okx, "_get", return_value=[]):
        for t in ("xrp", "Xrp", "XRP"):
            with pytest.raises(PermissionError):
                okx.place_order(ticker=t, side="sell", quantity=100.0)
    mock_post.assert_not_called()


def test_achat_reste_autorise():
    """Seule la vente est interdite : le bot peut renforcer la position."""
    okx._maker_attempts = okx.MAKER_MAX_PER_RUN  # court-circuite le chemin maker
    with patch.object(okx, "_get", return_value=[]), \
         patch.object(okx, "get_ask_price", return_value=1.50), \
         patch.object(okx, "get_instrument_specs",
                      return_value={"lotSz": "0.01", "tickSz": "0.0001", "minSz": "1"}), \
         patch.object(okx, "_post", return_value=[{"ordId": "OK1"}]) as mock_post:
        result = okx.place_order(ticker="XRP", side="buy", usdt_amount=50.0)
    assert result.get("ordId") == "OK1"
    assert mock_post.called
    okx._maker_attempts = 0


def test_ticker_non_protege_peut_etre_vendu():
    with patch.object(okx, "_get", return_value=[]), \
         patch.object(okx, "get_bid_price", return_value=100.0), \
         patch.object(okx, "get_instrument_specs",
                      return_value={"lotSz": "0.001", "tickSz": "0.01", "minSz": "0.01"}), \
         patch.object(okx, "_post", return_value=[{"ordId": "OK2"}]) as mock_post:
        result = okx.place_order(ticker="SOL", side="sell", quantity=1.0)
    assert result.get("ordId") == "OK2"
    assert mock_post.called
