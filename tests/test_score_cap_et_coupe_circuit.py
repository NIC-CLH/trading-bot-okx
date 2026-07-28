"""Plafond de score (zone surchauffe) et coupe-circuit capital."""
import sys
sys.path.insert(0, ".")

import ruflo_memory as rm
import scanner
import alert_scanner


def test_plafond_score_identique_sur_les_deux_chemins():
    """Le scanner 4h et le scanner 30min doivent refuser au même seuil."""
    assert scanner.SCORE_MAX_EXEC == alert_scanner.SCORE_MAX_EXEC == 2.8


def test_bande_executable_correspond_au_backtest():
    """Bande retenue = [AUTO_EXECUTE_THRESHOLD, 2.8] — validée sur les 2 moitiés."""
    assert scanner.AUTO_EXECUTE_THRESHOLD <= 2.0
    assert scanner.SCORE_MAX_EXEC == 2.8
    # Un score de 3.0 (signal "parfait") doit être hors bande
    assert 3.0 > scanner.SCORE_MAX_EXEC


def test_coupe_circuit_bloque_sous_le_plancher():
    assert rm.is_capital_floor_breached(150.0) is True
    assert rm.is_capital_floor_breached(199.99) is True


def test_coupe_circuit_laisse_passer_au_dessus():
    assert rm.is_capital_floor_breached(200.0) is False
    assert rm.is_capital_floor_breached(336.63) is False


def test_coupe_circuit_ne_bloque_pas_sur_donnee_absente():
    """Une lecture de balance ratée ne doit pas geler le bot par erreur."""
    assert rm.is_capital_floor_breached(None) is False
    assert rm.is_capital_floor_breached(0) is False
