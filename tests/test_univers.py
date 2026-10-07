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


def test_watch_only_exclus_de_l_univers_d_achat():
    """Les holdings perso ne doivent jamais apparaitre comme opportunite d'achat.
    Le 04/10/2026 le scanner 4h a achete $100 de XRP faute de ce filtre."""
    from unittest.mock import patch
    import position_manager as pm
    faux = ["SOL", "XRP", "DYDX"] + [f"TOK{i}" for i in range(50)]
    with patch.object(scanner.okx, "get_available_pairs", return_value=faux), \
         patch("ruflo_memory.get_eea_blacklist", return_value=set()):
        universe = scanner.get_universe()
    for t in pm.WATCH_ONLY_TICKERS:
        assert t not in universe, f"{t} est watch-only mais reste achetable"
    assert "SOL" in universe


def test_les_deux_scanners_excluent_les_watch_only():
    import inspect
    import alert_scanner
    assert "WATCH_ONLY" in inspect.getsource(scanner.get_universe)
    assert "WATCH_ONLY_TICKERS" in inspect.getsource(alert_scanner.scan_and_execute_signals)
