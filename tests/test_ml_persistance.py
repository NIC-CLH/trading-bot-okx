"""La chaine ML doit survivre aux runs GitHub Actions (07/10/2026).

Avant : stockage en SQLite, or *.db est dans .gitignore donc detruit a chaque
run. Et update_trade_label n'etait appele nulle part. Resultat : 0 echantillon
labellise malgre 115 trades clotures, le modele ne pouvait jamais s'entrainer.
"""
import sys
sys.path.insert(0, ".")

import inspect
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import ml_scorer as ml
import ruflo_memory as rm


@pytest.fixture
def memoire(tmp_path):
    f = tmp_path / "trade_memory.json"
    f.write_text(json.dumps({"outcomes": [], "entries": []}), encoding="utf-8")
    with patch.object(rm, "MEMORY_FILE", Path(f)):
        yield f


def test_plus_aucune_dependance_sqlite():
    src = inspect.getsource(ml)
    assert "sqlite3" not in src
    assert "FEATURES_DB" not in src


def test_signal_ecrit_dans_la_memoire_persistee(memoire):
    ml.save_signal_for_training({"ticker": "SOL", "score": 2.3})
    signaux = json.loads(memoire.read_text(encoding="utf-8"))["ml_signals"]
    assert len(signaux) == 1
    assert signaux[0]["ticker"] == "SOL"
    assert signaux[0]["label"] is None
    assert isinstance(signaux[0]["features"], list)


def test_label_pose_a_la_cloture(memoire):
    ml.save_signal_for_training({"ticker": "SOL", "score": 2.3})
    ml.update_trade_label("SOL", 12.5)
    sig = json.loads(memoire.read_text(encoding="utf-8"))["ml_signals"][0]
    assert sig["label"] == 1
    assert sig["pnl_pct"] == 12.5


def test_label_zero_sur_perte(memoire):
    ml.save_signal_for_training({"ticker": "SOL", "score": 2.3})
    ml.update_trade_label("SOL", -8.0)
    assert json.loads(memoire.read_text(encoding="utf-8"))["ml_signals"][0]["label"] == 0


def test_label_cible_le_signal_le_plus_recent(memoire):
    ml.save_signal_for_training({"ticker": "SOL", "score": 2.0})
    ml.save_signal_for_training({"ticker": "SOL", "score": 2.5})
    ml.update_trade_label("SOL", 5.0)
    signaux = json.loads(memoire.read_text(encoding="utf-8"))["ml_signals"]
    assert signaux[0]["label"] is None
    assert signaux[1]["label"] == 1


def test_jeu_d_entrainement_ne_prend_que_les_labellises(memoire):
    ml.save_signal_for_training({"ticker": "SOL", "score": 2.3})
    ml.save_signal_for_training({"ticker": "ZK", "score": 2.1})
    ml.update_trade_label("SOL", 10.0)
    X, y = ml.get_training_data()
    assert len(X) == 1 and len(y) == 1 and y[0] == 1


def test_jeu_vide_sans_label(memoire):
    ml.save_signal_for_training({"ticker": "SOL", "score": 2.3})
    assert ml.get_training_data() == (None, None)


def test_cloture_pose_le_label_automatiquement():
    """store_trade_outcome doit declencher l'etiquetage."""
    assert "update_trade_label" in inspect.getsource(rm.store_trade_outcome)


def test_le_cycle_30min_enregistre_ses_signaux():
    """Ce chemin a produit 100% des trades et n'alimentait pas le ML."""
    import alert_scanner
    assert "save_signal_for_training" in inspect.getsource(
        alert_scanner.scan_and_execute_signals)


def test_le_modele_entraine_est_commite():
    """ml_model.json doit etre pousse, sinon il est perdu au run suivant."""
    for f in (".github/workflows/trading_bot.yml", ".github/workflows/alert_scan.yml"):
        assert "ml_model.json" in open(f, encoding="utf-8").read(), f
