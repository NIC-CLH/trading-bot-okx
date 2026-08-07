"""Cohérence des univers de scan après l'élargissement du 28/07/2026."""
import sys
sys.path.insert(0, ".")

from unittest.mock import patch

import alert_scanner
import scanner


def test_caps_elargis():
    assert scanner.MAX_UNIVERSE == 80
    assert alert_scanner.ALERT_MAX_UNIVERSE == 65


def test_le_cycle_30min_ne_depasse_pas_le_cycle_4h():
    """Le scanner 30min a moins de temps : son univers doit rester plus petit."""
    assert alert_scanner.ALERT_MAX_UNIVERSE <= scanner.MAX_UNIVERSE


def test_univers_respecte_le_cap():
    faux_pairs = [f"TOK{i}" for i in range(200)]
    with patch.object(scanner.okx, "get_available_pairs", return_value=faux_pairs):
        universe = scanner.get_universe()
    assert len(universe) == scanner.MAX_UNIVERSE


def test_univers_exclut_toujours_stables_et_blacklist():
    """L'élargissement ne doit pas laisser passer les exclusions."""
    faux_pairs = ["SOL", "USDT", "WBTC", "DYDX"] + [f"TOK{i}" for i in range(100)]
    with patch.object(scanner.okx, "get_available_pairs", return_value=faux_pairs), \
         patch("ruflo_memory.get_eea_blacklist", return_value={"DYDX"}):
        universe = scanner.get_universe()
    assert "USDT" not in universe
    assert "WBTC" not in universe
    assert "DYDX" not in universe, "blacklist EEA ignorée après élargissement"
    assert "SOL" in universe
