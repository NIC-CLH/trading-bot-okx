import sys, json
sys.path.insert(0, ".")


def test_pending_signals_stockes(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    signals = [
        {"ticker": "XYZ", "score": 2.3, "prix": 1.5, "vol_regime": "high"},
        {"ticker": "ABC", "score": 1.8, "prix": 0.5, "vol_regime": "normal"},
    ]
    rm.store_pending_signals(signals)

    data = json.loads((tmp_path / "mem.json").read_text())
    pending = data.get("pending_signals", [])
    scalp_tickers = [p["ticker"] for p in pending]
    # XYZ score=2.3 doit être stocké (>=2.0), ABC score=1.8 non
    assert "XYZ" in scalp_tickers, f"XYZ (score 2.3) devrait être dans pending_signals"


def test_pending_signals_expire(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    data = {"outcomes": [], "entries": [], "pending_signals": [
        {"ticker": "OLD", "score": 2.5, "prix_ref": 1.0,
         "timestamp": "2020-01-01T00:00:00+00:00", "ttl": 14400}
    ]}
    (tmp_path / "mem.json").write_text(json.dumps(data))

    actifs = rm.get_active_pending_signals()
    assert len(actifs) == 0, f"Signal expiré ne doit pas être retourné"
