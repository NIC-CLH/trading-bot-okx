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
