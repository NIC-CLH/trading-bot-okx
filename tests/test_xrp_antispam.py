"""Anti-spam XRP : seuil relatif à la volatilité + cooldown persistant."""
import sys
sys.path.insert(0, ".")

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import alert_scanner as a
import ruflo_memory as rm


@pytest.fixture
def memoire_isolee(tmp_path):
    fake = tmp_path / "trade_memory.json"
    fake.write_text(json.dumps({"outcomes": [], "entries": []}), encoding="utf-8")
    with patch.object(rm, "MEMORY_FILE", Path(fake)):
        yield fake


def test_cooldown_bloque_la_seconde_alerte(memoire_isolee):
    assert rm.is_alert_cooldown_active("xrp_spike", 8) is False
    rm.mark_alert_sent("xrp_spike")
    assert rm.is_alert_cooldown_active("xrp_spike", 8) is True


def test_cooldown_independant_par_type(memoire_isolee):
    """Une alerte d'achat ne doit pas museler une alerte de mouvement."""
    rm.mark_alert_sent("xrp_buy")
    assert rm.is_alert_cooldown_active("xrp_buy", 8) is True
    assert rm.is_alert_cooldown_active("xrp_spike", 8) is False


def test_cooldown_expire(memoire_isolee):
    rm.mark_alert_sent("xrp_spike")
    assert rm.is_alert_cooldown_active("xrp_spike", 0.0) is False


def test_seuil_spike_suit_la_volatilite():
    """Avec un ATR de 5.8%/j, le seuil doit dépasser largement les 4% fixes."""
    atr = 5.8
    seuil = max(a.XRP_PRICE_MOVE_PCT, atr * a.XRP_SPIKE_ATR_MULT)
    assert seuil == pytest.approx(8.7), f"seuil {seuil} — devrait suivre l'ATR"
    assert seuil > a.XRP_PRICE_MOVE_PCT


def test_seuil_plancher_sur_actif_calme():
    """Sur un ATR faible, le plancher fixe de 4% s'applique."""
    seuil = max(a.XRP_PRICE_MOVE_PCT, 1.0 * a.XRP_SPIKE_ATR_MULT)
    assert seuil == a.XRP_PRICE_MOVE_PCT


def test_module_utilise_bien_le_seuil_adaptatif():
    src = inspect.getsource(a.scan_xrp_binance)
    assert "seuil_spike" in src and "XRP_SPIKE_ATR_MULT" in src
    assert "is_alert_cooldown_active" in src
    assert "mark_alert_sent" in src
