import sys
sys.path.insert(0, ".")
import capital_allocator as ca


def test_rotation_protege_position_profitable():
    """Une position à +3.5% ne doit PAS être candidate à la rotation."""
    positions = [
        {"ticker": "ASTER", "prix_entree": 0.7, "pnl_pct": 3.5,  "valeur_usd": 190},
        {"ticker": "ZEC",   "prix_entree": 660, "pnl_pct": -0.5, "valeur_usd": 188},
    ]
    candidate = ca._find_rotation_candidate("NEW", positions)
    assert candidate is not None
    assert candidate["ticker"] == "ZEC", f"Doit choisir ZEC, pas ASTER (protégé à +3.5%)"


def test_rotation_protege_a_plus3_exactement():
    """Seuil exact : +3.0% est protégé (exclu), +2.9% est candidat.
    Ici seul B est disponible — si A était candidat aussi, le test vérifierait
    uniquement que B est choisi, sans garantir qu'A est exclu.
    On vérifie donc aussi que A seul → None (protégé)."""
    # Cas 1 : les deux présents — B doit être choisi (A protégé à ≥3%)
    positions = [
        {"ticker": "A", "prix_entree": 1.0, "pnl_pct": 3.0,  "valeur_usd": 100},
        {"ticker": "B", "prix_entree": 1.0, "pnl_pct": 2.9,  "valeur_usd": 100},
    ]
    candidate = ca._find_rotation_candidate("NEW", positions)
    assert candidate["ticker"] == "B"

    # Cas 2 : seul A présent à exactement +3.0% → doit être protégé (None)
    positions_only_a = [
        {"ticker": "A", "prix_entree": 1.0, "pnl_pct": 3.0, "valeur_usd": 100},
    ]
    candidate_a = ca._find_rotation_candidate("NEW", positions_only_a)
    assert candidate_a is None, "Position à exactement +3.0% doit être protégée (≥ +3%)"

    # Cas 3 : seul C présent à +2.9% → candidat légitime
    positions_only_c = [
        {"ticker": "C", "prix_entree": 1.0, "pnl_pct": 2.9, "valeur_usd": 100},
    ]
    candidate_c = ca._find_rotation_candidate("NEW", positions_only_c)
    assert candidate_c is not None
    assert candidate_c["ticker"] == "C"
