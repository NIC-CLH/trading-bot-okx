import sys
sys.path.insert(0, ".")


def test_mode_observation_retourne_bool():
    """is_observation_mode() doit toujours retourner un bool, jamais lever."""
    from scanner import is_observation_mode
    result = is_observation_mode()
    assert isinstance(result, bool)


def test_mode_observation_false_si_btc_ok(monkeypatch):
    """Si BTC est au-dessus de la MA50, mode observation = False (même en bear)."""
    import scanner
    # is_btc_uptrend est importée localement (from position_manager import is_btc_uptrend)
    # → patcher la source : position_manager.is_btc_uptrend
    import position_manager
    monkeypatch.setattr(position_manager, "is_btc_uptrend", lambda: True)
    result = scanner.is_observation_mode()
    assert result is False, f"BTC bullish → observation doit être False, obtenu {result}"
