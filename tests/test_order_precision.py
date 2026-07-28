"""Alignement des ordres sur les pas OKX (lotSz / tickSz)."""
import sys
sys.path.insert(0, ".")

from decimal import Decimal

import okx_client as okx


def test_align_arrondit_vers_le_bas():
    """L'alignement ne doit jamais produire une valeur supérieure (risque de solde insuffisant)."""
    assert okx._align_to_step(0.10102383, "0.000001") == 0.101023
    assert okx._align_to_step(1.05785582, "0.0001") == 1.0578
    assert okx._align_to_step(50.82141906, "0.001") == 50.821


def test_align_resultat_multiple_exact_du_pas():
    for value, step in [(0.78429492, "0.00001"), (8.907393, "0.0001"), (566.94, "0.1")]:
        aligned = okx._align_to_step(value, step)
        assert Decimal(str(aligned)) % Decimal(step) == 0, f"{aligned} non multiple de {step}"


def test_align_tolere_step_absent_ou_invalide():
    """Specs indisponibles → on renvoie la valeur d'origine plutôt que de planter."""
    assert okx._align_to_step(1.2345, None) == 1.2345
    assert okx._align_to_step(1.2345, "") == 1.2345
    assert okx._align_to_step(1.2345, "abc") == 1.2345
    assert okx._align_to_step(1.2345, "0") == 1.2345


def test_align_valeur_deja_conforme_inchangee():
    assert okx._align_to_step(0.5, "0.1") == 0.5
    assert okx._align_to_step(100.0, "0.01") == 100.0
