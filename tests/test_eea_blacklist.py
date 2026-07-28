"""Tests de la blacklist EEA persistante (isolée du vrai trade_memory.json)."""
import sys
sys.path.insert(0, ".")

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import ruflo_memory as rm


@pytest.fixture
def memoire_isolee(tmp_path):
    """Redirige MEMORY_FILE vers un fichier temporaire."""
    fake = tmp_path / "trade_memory.json"
    fake.write_text(json.dumps({"outcomes": [], "entries": []}), encoding="utf-8")
    with patch.object(rm, "MEMORY_FILE", Path(fake)):
        yield fake


def test_blacklist_persiste_et_relit(memoire_isolee):
    rm.blacklist_eea("BNB", "All operations failed (code 1)")
    assert rm.get_eea_blacklist() == {"BNB"}
    data = json.loads(memoire_isolee.read_text(encoding="utf-8"))
    assert data["eea_blacklist"]["BNB"]["reason"].startswith("All operations failed")
    assert "since" in data["eea_blacklist"]["BNB"]


def test_blacklist_normalise_la_casse(memoire_isolee):
    rm.blacklist_eea("avax", "test")
    assert "AVAX" in rm.get_eea_blacklist()


def test_blacklist_idempotente(memoire_isolee):
    """Re-blacklister ne duplique pas et ne réécrit pas la date d'origine."""
    rm.blacklist_eea("GRAM", "premier refus")
    first = json.loads(memoire_isolee.read_text(encoding="utf-8"))["eea_blacklist"]["GRAM"]
    rm.blacklist_eea("GRAM", "second refus")
    second = json.loads(memoire_isolee.read_text(encoding="utf-8"))["eea_blacklist"]["GRAM"]
    assert second == first
    assert len(rm.get_eea_blacklist()) == 1


def test_blacklist_vide_par_defaut(memoire_isolee):
    assert rm.get_eea_blacklist() == set()


def test_univers_scanner_exclut_la_blacklist(memoire_isolee):
    """get_universe() doit retirer les tickers blacklistés EEA."""
    import scanner
    rm.blacklist_eea("BNB", "refus")
    with patch.object(scanner.okx, "get_available_pairs",
                      return_value=["SOL", "BNB", "DYDX"]):
        universe = scanner.get_universe()
    assert "BNB" not in universe
    assert {"SOL", "DYDX"} <= set(universe)
