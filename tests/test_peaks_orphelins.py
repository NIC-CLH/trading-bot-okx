"""Purge des peaks de positions fermees (07/10/2026).

Un peak orphelin fait calculer au trailing stop un plancher herite : la
nouvelle position demarre a 0% et se retrouve deja sous le plancher, donc
vendue des le premier cycle. Constate avec 8 orphelins dont MON +10.9%.
"""
import sys
sys.path.insert(0, ".")

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import ruflo_memory as rm


@pytest.fixture
def memoire(tmp_path):
    f = tmp_path / "trade_memory.json"
    f.write_text(json.dumps({
        "outcomes": [], "entries": [],
        "peaks": {"SOL": 4.2, "MON": 10.9, "EIGEN": 8.0},
    }), encoding="utf-8")
    with patch.object(rm, "MEMORY_FILE", Path(f)):
        yield f


def test_purge_supprime_les_fermees_et_garde_les_ouvertes(memoire):
    orph = rm.purge_peaks_orphelins({"SOL"})
    assert set(orph) == {"MON", "EIGEN"}
    peaks = json.loads(memoire.read_text(encoding="utf-8"))["peaks"]
    assert peaks == {"SOL": 4.2}


def test_purge_insensible_a_la_casse(memoire):
    rm.purge_peaks_orphelins({"sol"})
    assert "SOL" in json.loads(memoire.read_text(encoding="utf-8"))["peaks"]


def test_purge_sans_orphelin_ne_touche_rien(memoire):
    assert rm.purge_peaks_orphelins({"SOL", "MON", "EIGEN"}) == []


def test_purge_vide_tout_si_aucune_position(memoire):
    rm.purge_peaks_orphelins(set())
    assert json.loads(memoire.read_text(encoding="utf-8"))["peaks"] == {}


def test_un_rachat_repart_sans_pic_herite(memoire):
    """Coeur du probleme : apres purge, get_peak_pnl doit repartir de zero."""
    rm.purge_peaks_orphelins({"SOL"})
    assert rm.get_peak_pnl("MON") == 0.0


def test_purge_branchee_sur_le_cycle_30min():
    import inspect
    import alert_scanner
    src = inspect.getsource(alert_scanner.emergency_stop_check)
    assert "purge_peaks_orphelins" in src
