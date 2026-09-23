import sys
sys.path.insert(0, ".")
from scanner import compute_final_score

def test_technique_sert_de_base():
    """Un technique parfait, tout le reste neutre, doit rester un score fort.
    Avant le 23/09/2026 la moyenne ponderee l'ecrasait a 1.10 et le cycle 4h
    ne pouvait jamais atteindre son seuil de 2.0."""
    score = compute_final_score(3.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert score == 3.0, f"Attendu 3.0, obtenu {score}"


def test_dimension_muette_ne_dilue_pas():
    """Une source morte (0.0) ne doit rien retrancher au score technique."""
    avec = compute_final_score(2.2, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert avec == 2.2, f"Une dimension a zero a dilue le score : {avec}"


def test_news_reste_le_plus_gros_ajustement():
    """La hierarchie validee doit tenir : news >> microstructure > oc = cg > macro."""
    from scanner import AJUST_MAX
    assert AJUST_MAX["news"] > AJUST_MAX["ms"] > AJUST_MAX["oc"]
    assert AJUST_MAX["oc"] == AJUST_MAX["cg"] > AJUST_MAX["macro"]


def test_contexte_favorable_promeut_un_signal_moyen():
    """Technique 1.5 + tout favorable doit franchir le seuil d'execution."""
    from scanner import AUTO_EXECUTE_THRESHOLD
    score = compute_final_score(1.5, 2.0, 1.0, 1.0, 1.5, 1.0)
    assert score >= AUTO_EXECUTE_THRESHOLD, f"{score} < {AUTO_EXECUTE_THRESHOLD}"


def test_contexte_defavorable_retrograde_un_bon_signal():
    """Technique 2.2 + tout contre doit repasser sous le seuil."""
    from scanner import AUTO_EXECUTE_THRESHOLD
    score = compute_final_score(2.2, -2.0, -1.0, -1.0, -1.5, -1.0)
    assert score < AUTO_EXECUTE_THRESHOLD, f"{score} >= {AUTO_EXECUTE_THRESHOLD}"


def test_ajustement_total_borne():
    """L'amplitude cumulee des ajustements ne doit pas noyer la technique."""
    from scanner import AJUST_MAX
    assert 0.9 <= sum(AJUST_MAX.values()) <= 1.3


def test_score_borne_a_3():
    assert compute_final_score(3.0, 2.0, 1.0, 1.0, 1.5, 1.0) == 3.0
    assert compute_final_score(-3.0, -2.0, -1.0, -1.0, -1.5, -1.0) == -3.0


def test_les_deux_scanners_partagent_la_bande_validee():
    """Cycle 4h et cycle 30min doivent couvrir la meme bande 2.0-2.8."""
    import scanner, alert_scanner
    assert alert_scanner.SIGNAL_ALERT_THRESHOLD == scanner.AUTO_EXECUTE_THRESHOLD == 2.0
    assert alert_scanner.SCORE_MAX_EXEC == scanner.SCORE_MAX_EXEC == 2.8


def test_option_a_retiree():
    """Option A retirée (backtest 17/07 : EV -1.58% sous MA50) —
    les constantes du mode bear ne doivent plus exister."""
    import scanner
    assert not hasattr(scanner, "BTC_BEAR_MIN_SCORE")
    assert not hasattr(scanner, "BTC_BEAR_SIZE_MULT")
