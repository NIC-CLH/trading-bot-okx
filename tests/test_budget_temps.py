"""Garde-fou temps : le maker ne doit jamais mettre le cycle en dépassement."""
import sys
sys.path.insert(0, ".")

import time
from unittest.mock import patch

import okx_client as okx


def test_budget_restant_decroit():
    restant = okx._budget_restant()
    assert restant <= okx.PROCESS_BUDGET_SEC
    assert restant > 0


def test_maker_ignore_si_budget_epuise():
    """Budget consommé → aucun ordre post_only, fallback agressif direct."""
    okx._maker_attempts = 0
    with patch.object(okx, "_budget_restant", return_value=10.0), \
         patch.object(okx, "_try_maker_buy") as mock_maker, \
         patch.object(okx, "_get", return_value=[]), \
         patch.object(okx, "get_ask_price", return_value=100.0), \
         patch.object(okx, "get_bid_price", return_value=99.0), \
         patch.object(okx, "get_instrument_specs",
                      return_value={"lotSz": "0.001", "tickSz": "0.01", "minSz": "0.01"}), \
         patch.object(okx, "_post", return_value=[{"ordId": "T1"}]):
        okx.place_order("SOL", "buy", usdt_amount=57.0)
    mock_maker.assert_not_called()
    okx._maker_attempts = 0


def test_maker_tente_si_budget_suffisant():
    okx._maker_attempts = 0
    with patch.object(okx, "_budget_restant", return_value=600.0), \
         patch.object(okx, "_try_maker_buy", return_value={"ordId": "M1", "maker": True}) as mock_maker, \
         patch.object(okx, "_get", return_value=[]), \
         patch.object(okx, "get_bid_price", return_value=99.0), \
         patch.object(okx, "get_instrument_specs",
                      return_value={"lotSz": "0.001", "tickSz": "0.01", "minSz": "0.01"}):
        result = okx.place_order("SOL", "buy", usdt_amount=57.0)
    mock_maker.assert_called_once()
    assert result["maker"] is True
    okx._maker_attempts = 0


def test_attente_maker_bornee_par_le_budget_du_cycle():
    """2 tentatives x attente doivent tenir dans le budget le plus serré (30min)."""
    budget_30min = 660          # OKX_PROCESS_BUDGET du workflow alert_scan
    attente_30min = 45          # OKX_MAKER_WAIT du workflow alert_scan
    cout_max = okx.MAKER_MAX_PER_RUN * attente_30min
    assert cout_max < budget_30min * 0.25, "L'attente maker mange trop du budget"
