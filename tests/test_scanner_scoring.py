import sys
sys.path.insert(0, ".")
from scanner import compute_final_score

def test_score_news_poids_double():
    """Avec news fortement positif (+2.0) et tout le reste neutre (0), score = 0.60."""
    score = compute_final_score(
        score_tech=0.0, score_news=2.0, score_ms=0.0,
        score_oc=0.0, score_cg=0.0, score_macro=0.0
    )
    # Nouveau poids news = 0.30 → 2.0 × 0.30 = 0.60
    assert abs(score - 0.60) < 0.001, f"Attendu 0.60, obtenu {score}"

def test_score_tech_poids_35():
    """Tech seul à +1.0 → score = 0.35."""
    score = compute_final_score(
        score_tech=1.0, score_news=0.0, score_ms=0.0,
        score_oc=0.0, score_cg=0.0, score_macro=0.0
    )
    assert abs(score - 0.35) < 0.001, f"Attendu 0.35, obtenu {score}"

def test_poids_somme_a_1():
    """Tous les poids doivent sommer à 1.0."""
    score = compute_final_score(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    assert abs(score - 1.0) < 0.01, f"Somme des poids ≠ 1.0 : {score}"
