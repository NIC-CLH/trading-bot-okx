import sys, json, os
sys.path.insert(0, ".")


def test_store_entry_inclut_subscores(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    payload = {
        "ticker": "TEST", "score": 1.8, "regime": "bull",
        "vol_regime": "normal", "prix": 1.0,
        "score_tech": 0.72, "score_news": 0.45, "score_ms": 0.31,
        "score_oc": 0.15, "score_cg": 0.42, "score_macro": 0.28,
        "score_tech_adj": 0.68, "score_rs": 0.10, "score_social": 0.05,
    }
    rm.store_trade_entry(payload)

    data = json.loads((tmp_path / "mem.json").read_text())
    entry = data["entries"][-1]
    assert "score_tech_raw" in entry, "score_tech_raw manquant"
    assert abs(entry["score_tech_raw"] - 0.72) < 0.001
    assert "score_news_raw" in entry
    assert "score_ms_raw" in entry


def test_store_outcome_inclut_exit_quality(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    # Simuler un peak existant (8%)
    data = {"outcomes": [], "entries": [], "peaks": {"TEST": 8.0}}
    (tmp_path / "mem.json").write_text(json.dumps(data))

    rm.store_trade_outcome({
        "ticker": "TEST", "pnl_pct": 5.0,
        "days_held": 2.0, "raison": "TP", "valeur": 200,
    })

    result = json.loads((tmp_path / "mem.json").read_text())
    outcome = result["outcomes"][-1]
    assert "exit_quality" in outcome, "exit_quality manquant"
    # Sorti à 5%, peak 8% → exit_quality = 5/8*100 = 62.5
    assert abs(outcome["exit_quality"] - 62.5) < 0.1, f"exit_quality attendu 62.5, obtenu {outcome['exit_quality']}"


def test_get_rolling_ev_positif(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    outcomes = (
        [{"ticker": f"T{i}", "pnl_pct": 12.0, "outcome": "win"}  for i in range(8)] +
        [{"ticker": f"L{i}", "pnl_pct": -7.0,  "outcome": "loss"} for i in range(7)]
    )
    import json
    (tmp_path / "mem.json").write_text(json.dumps({"outcomes": outcomes, "entries": []}))

    result = rm.get_rolling_ev(n_trades=15)
    assert result["ev"] is not None
    # WR=8/15=0.533, med_win=12, med_loss=7 → EV = 0.533×12 - 0.467×7 = 6.4 - 3.27 = 3.13
    assert result["ev"] > 0, f"EV devrait être positif : {result['ev']}"

def test_get_rolling_ev_negatif(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    outcomes = (
        [{"ticker": f"T{i}", "pnl_pct": 12.0, "outcome": "win"}  for i in range(4)] +
        [{"ticker": f"L{i}", "pnl_pct": -8.0,  "outcome": "loss"} for i in range(11)]
    )
    import json
    (tmp_path / "mem.json").write_text(json.dumps({"outcomes": outcomes, "entries": []}))

    result = rm.get_rolling_ev(n_trades=15)
    assert result["ev"] < 0, f"EV devrait être négatif : {result['ev']}"

def test_get_rolling_ev_trop_peu_de_trades(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    import json
    (tmp_path / "mem.json").write_text(json.dumps({"outcomes": [
        {"ticker": "X", "pnl_pct": 5.0}
    ], "entries": []}))

    result = rm.get_rolling_ev(n_trades=15)
    assert result["ev"] is None
    assert result["mode"] == "normal"


def test_reentry_threshold_petite_perte(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    import json
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    rm.set_reentry_threshold("KAIA", loss_pct=-4.0)
    threshold = rm.get_reentry_threshold("KAIA")
    assert threshold == 1.7, f"Perte -4% → seuil 1.7, obtenu {threshold}"

def test_reentry_threshold_grande_perte(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    import json
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    rm.set_reentry_threshold("CHZ", loss_pct=-12.0)
    assert rm.get_reentry_threshold("CHZ") == 2.2

def test_reentry_threshold_expire(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    import json, time
    data = {"outcomes": [], "entries": [], "reentry_thresholds": {
        "BTC": {"threshold": 2.0, "expires": time.time() - 1, "loss_pct": -8.0}
    }}
    (tmp_path / "mem.json").write_text(json.dumps(data))

    result = rm.get_reentry_threshold("BTC")
    assert result is None, f"Threshold expiré doit retourner None, obtenu {result}"


def test_add_near_miss_cap_15(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    import json
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    for i in range(20):
        rm.add_near_miss(f"T{i}", score=1.3, prix=1.0, trade_type="swing")

    data = json.loads((tmp_path / "mem.json").read_text())
    assert len(data["shadow_portfolio"]) <= 15, \
        f"Shadow portfolio doit être plafonné à 15, obtenu {len(data['shadow_portfolio'])}"

def test_add_near_miss_deduplique(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    import json
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    rm.add_near_miss("KAIA", score=1.3, prix=0.05, trade_type="swing")
    rm.add_near_miss("KAIA", score=1.4, prix=0.052, trade_type="swing")

    data = json.loads((tmp_path / "mem.json").read_text())
    kaias = [s for s in data["shadow_portfolio"] if s["ticker"] == "KAIA"]
    assert len(kaias) == 1, "KAIA doit être dédupliqué"
    assert kaias[0]["score"] == 1.4, "Version la plus récente doit être gardée"


def test_get_entry_stop_retourne_stop_stocke(tmp_path, monkeypatch):
    """get_entry_stop doit retourner le stop stocké à l'entrée (pas de drift ATR)."""
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    payload = {
        "ticker": "JTO", "score": 2.1, "regime": "bull",
        "vol_regime": "normal", "prix": 3.50, "stop": 3.255,  # -7% stocké
    }
    rm.store_trade_entry(payload)

    result = rm.get_entry_stop("JTO")
    assert result is not None, "Le stop doit être retourné"
    assert abs(result - 3.255) < 0.001, f"Stop attendu 3.255, obtenu {result}"


def test_get_entry_stop_retourne_none_si_absent(tmp_path, monkeypatch):
    """get_entry_stop retourne None si le ticker n'a pas d'entrée — fallback vers ATR live."""
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    result = rm.get_entry_stop("INCONNU")
    assert result is None, "Doit retourner None si pas d'entrée connue"
