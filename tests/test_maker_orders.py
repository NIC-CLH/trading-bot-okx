"""Tests de la logique maker-first (mocks — aucun appel réseau réel)."""
import sys
sys.path.insert(0, ".")

from unittest.mock import patch

import okx_client as okx


def _reset_counter():
    okx._maker_attempts = 0


def test_maker_rempli_retourne_sans_fallback():
    """Ordre post_only rempli → résultat maker, aucun ordre agressif placé."""
    _reset_counter()
    with patch.object(okx, "_post", return_value=[{"ordId": "M1"}]) as mock_post, \
         patch.object(okx, "get_order_state", return_value={"state": "filled"}), \
         patch.object(okx, "time") as mock_time:
        mock_time.time.side_effect = [0, 5]   # deadline pas atteinte
        mock_time.sleep = lambda s: None
        result = okx._try_maker_buy("SOL", "SOL-USDC", 100.0, 2.0)
    assert result is not None
    assert result["maker"] is True
    assert result["ordId"] == "M1"
    assert mock_post.call_count == 1
    body = mock_post.call_args[0][1]
    assert body["ordType"] == "post_only"
    assert body["px"] == "100.0"


def test_maker_non_rempli_retourne_none():
    """Timeout sans remplissage → annulation + None (le caller passe agressif)."""
    _reset_counter()
    with patch.object(okx, "_post", return_value=[{"ordId": "M2"}]), \
         patch.object(okx, "get_order_state", return_value={"state": "live", "accFillSz": "0"}), \
         patch.object(okx, "cancel_order") as mock_cancel, \
         patch.object(okx, "time") as mock_time:
        # première boucle sous la deadline, ensuite deadline dépassée
        mock_time.time.side_effect = [0, 200, 200, 200]
        mock_time.sleep = lambda s: None
        result = okx._try_maker_buy("SOL", "SOL-USDC", 100.0, 2.0)
    assert result is None
    mock_cancel.assert_called_once()


def test_maker_partiel_garde_le_rempli():
    """Rempli partiellement au timeout → on garde la part remplie, pas de fallback."""
    _reset_counter()
    with patch.object(okx, "_post", return_value=[{"ordId": "M3"}]), \
         patch.object(okx, "get_order_state", return_value={"state": "partially_filled", "accFillSz": "1.5"}), \
         patch.object(okx, "cancel_order"), \
         patch.object(okx, "time") as mock_time:
        mock_time.time.side_effect = [0, 200, 200, 200]
        mock_time.sleep = lambda s: None
        result = okx._try_maker_buy("SOL", "SOL-USDC", 100.0, 2.0)
    assert result is not None
    assert result["partial"] is True
    assert result["qty_estimate"] == 1.5


def test_post_only_annule_par_okx_fallback_immediat():
    """post_only annulé par OKX (croiserait le carnet) → None sans attendre le timeout."""
    _reset_counter()
    with patch.object(okx, "_post", return_value=[{"ordId": "M4"}]), \
         patch.object(okx, "get_order_state", return_value={"state": "canceled"}), \
         patch.object(okx, "time") as mock_time:
        mock_time.time.side_effect = [0, 5]
        mock_time.sleep = lambda s: None
        result = okx._try_maker_buy("SOL", "SOL-USDC", 100.0, 2.0)
    assert result is None


def test_cap_maker_par_cycle():
    """Au-delà de MAKER_MAX_PER_RUN tentatives, plus aucune tentative maker."""
    _reset_counter()
    okx._maker_attempts = okx.MAKER_MAX_PER_RUN  # cap atteint
    with patch.object(okx, "_try_maker_buy") as mock_maker, \
         patch.object(okx, "_get", return_value=[]), \
         patch.object(okx, "get_ask_price", return_value=None), \
         patch.object(okx, "get_bid_price", return_value=100.0), \
         patch.object(okx, "_post", return_value=[{"ordId": "T1"}]):
        okx.place_order("SOL", "buy", usdt_amount=200.0)
    mock_maker.assert_not_called()
    _reset_counter()
