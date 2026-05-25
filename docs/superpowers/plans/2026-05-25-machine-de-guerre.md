# Machine de Guerre — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transformer le bot en un système qui apprend et s'adapte — EV positif, win rate ≥ 40% sur les 20 prochains trades.

**Architecture:** 8 tâches indépendantes à déployer dans l'ordre. Tasks 1-4 = défensif (arrêter de perdre). Tasks 5-8 = offensif (capturer plus). Chaque tâche est un commit séparé, déployable individuellement.

**Tech Stack:** Python 3.11, OKX API, GitHub Actions, trade_memory.json (persistance cross-runs)

---

## File Map

| Fichier | Modifications |
|---|---|
| `capital_allocator.py` | Task 1 (protection rotation), Task 4 (EV mode → threshold) |
| `scanner.py` | Task 2 (poids news, momentum filter), Task 5 (mode observation), Task 7 (near-misses), Task 8 (pending_signals) |
| `ruflo_memory.py` | Task 3 (sub-scores, exit_quality), Task 4 (get_rolling_ev), Task 6 (reentry_threshold), Task 7 (shadow portfolio) |
| `position_manager.py` | Task 6 (appel set_reentry_threshold après stop) |
| `alert_scanner.py` | Task 8 (entry_scan_scalp) |

---

## Task 1 — Protection rotation + filtre anti-top

**Fichiers :**
- Modifier : `capital_allocator.py` ligne 119

- [ ] **Lire le fichier avant d'éditer**

```bash
# Vérifier la ligne exacte à modifier
grep -n "pnl_pct.*10" capital_allocator.py
```

- [ ] **Écrire le test**

Créer `tests/test_capital_allocator.py` :

```python
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
    """Seuil exact : +3.0% est protégé, +2.9% est candidat."""
    positions = [
        {"ticker": "A", "prix_entree": 1.0, "pnl_pct": 3.0,  "valeur_usd": 100},
        {"ticker": "B", "prix_entree": 1.0, "pnl_pct": 2.9,  "valeur_usd": 100},
    ]
    candidate = ca._find_rotation_candidate("NEW", positions)
    assert candidate["ticker"] == "B"
```

- [ ] **Vérifier que le test échoue**

```bash
cd "E:\Claude code\portfolio" && python -m pytest tests/test_capital_allocator.py -v 2>&1
```
Attendu : FAIL (seuil actuel est 10.0, pas 3.0)

- [ ] **Modifier `capital_allocator.py`**

Trouver la ligne :
```python
and (p.get("pnl_pct") or 0) < 10.0
```
Remplacer par :
```python
and (p.get("pnl_pct") or 0) < 3.0   # Protège les positions ≥ +3% de la rotation
```

- [ ] **Vérifier que le test passe**

```bash
python -m pytest tests/test_capital_allocator.py -v 2>&1
```
Attendu : PASS

- [ ] **Commit**

```bash
git add capital_allocator.py tests/test_capital_allocator.py
git commit -m "feat: protéger positions ≥ +3% de la rotation"
```

---

## Task 2 — Poids score_news ×2 + filtre momentum

**Fichiers :**
- Modifier : `scanner.py` fonctions `compute_final_score()` (ligne ~88) et boucle Phase 1 (~ligne 390)

- [ ] **Écrire les tests**

Créer `tests/test_scanner_scoring.py` :

```python
import sys
sys.path.insert(0, ".")
from scanner import compute_final_score

def test_score_news_poids_double():
    """Avec news fortement positif (+2.0) et tout le reste neutre (0), score > 0.5."""
    score = compute_final_score(
        score_tech=0.0, score_news=2.0, score_ms=0.0,
        score_oc=0.0, score_cg=0.0, score_macro=0.0
    )
    # Nouveau poids news = 0.30 → 2.0 × 0.30 = 0.60
    assert score == 0.60, f"Attendu 0.60, obtenu {score}"

def test_score_tech_poids_35():
    """Tech seul à +1.0 → score = 0.35."""
    score = compute_final_score(
        score_tech=1.0, score_news=0.0, score_ms=0.0,
        score_oc=0.0, score_cg=0.0, score_macro=0.0
    )
    assert score == 0.35, f"Attendu 0.35, obtenu {score}"

def test_poids_somme_a_1():
    """Tous les poids doivent sommer à 1.0."""
    score = compute_final_score(1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    assert abs(score - 1.0) < 0.01, f"Somme des poids ≠ 1.0 : {score}"
```

- [ ] **Vérifier que les tests échouent**

```bash
python -m pytest tests/test_scanner_scoring.py -v 2>&1
```
Attendu : FAIL (poids actuels : tech=0.40, news=0.15)

- [ ] **Modifier `compute_final_score` dans `scanner.py`**

Remplacer :
```python
    score = (
        score_tech  * 0.40
        + score_news  * 0.15
        + score_ms    * 0.15
        + score_oc    * 0.10
        + score_cg    * 0.10
        + score_macro * 0.10
    )
```
Par :
```python
    # Poids révisés — score_news doublé (pattern wins = tokens à narrative forte)
    # tech 0.40→0.35 | news 0.15→0.30 | ms 0.15→0.12 | oc 0.10→0.08 | cg 0.10→0.08 | macro 0.10→0.07
    score = (
        score_tech  * 0.35
        + score_news  * 0.30
        + score_ms    * 0.12
        + score_oc    * 0.08
        + score_cg    * 0.08
        + score_macro * 0.07
    )
```

- [ ] **Ajouter le filtre momentum dans la boucle Phase 1 de `run_scan`**

Après le bloc `# Appliquer le multiplicateur contextuel du Regime Enricher` (ligne ~443), ajouter :

```python
            # ── Filtre momentum : éviter d'acheter ce qui a déjà trop monté ──
            try:
                if df_ticker is not None and len(df_ticker) >= 18:
                    # 18 candles × 4h = 72h de lookback
                    prix_72h_ago = float(df_ticker["close"].iloc[-18])
                    prix_now     = float(df_ticker["close"].iloc[-1])
                    momentum_72h = (prix_now - prix_72h_ago) / prix_72h_ago * 100
                    if momentum_72h > 15.0:
                        score_final = min(score_final, 1.4)
                        logger.info(
                            f"{ticker} : momentum +{momentum_72h:.1f}% sur 72h "
                            f"→ score plafonné à 1.4 (éviter top)"
                        )
            except Exception:
                pass
```

- [ ] **Vérifier que les tests passent**

```bash
python -m pytest tests/test_scanner_scoring.py -v 2>&1
```
Attendu : PASS (3 tests)

- [ ] **Commit**

```bash
git add scanner.py tests/test_scanner_scoring.py
git commit -m "feat: poids score_news x2 + filtre momentum +15% en 72h"
```

---

## Task 3 — Sub-scores bruts + exit_quality dans la mémoire

**Fichiers :**
- Modifier : `ruflo_memory.py` fonctions `store_trade_entry()` (ligne ~207) et `store_trade_outcome()` (ligne ~243)

- [ ] **Écrire les tests**

Créer `tests/test_ruflo_memory.py` :

```python
import sys, json, tempfile, os
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
    assert entry["score_tech_raw"] == 0.72
    assert "score_news_raw" in entry
    assert "score_ms_raw" in entry

def test_store_outcome_inclut_exit_quality(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    # Simuler un peak existant
    data = {"outcomes": [], "entries": [], "peaks": {"TEST": 8.0}}
    (tmp_path / "mem.json").write_text(json.dumps(data))

    rm.store_trade_outcome({
        "ticker": "TEST", "pnl_pct": 5.0,
        "days_held": 2.0, "raison": "TP", "valeur": 200,
    })

    result = json.loads((tmp_path / "mem.json").read_text())
    outcome = result["outcomes"][-1]
    assert "exit_quality" in outcome
    # Sorti à 5%, peak était 8% → exit_quality = 5/8*100 = 62.5
    assert abs(outcome["exit_quality"] - 62.5) < 0.1
```

- [ ] **Vérifier que les tests échouent**

```bash
python -m pytest tests/test_ruflo_memory.py -v 2>&1
```

- [ ] **Modifier `store_trade_entry` dans `ruflo_memory.py`**

Dans le dict `entry = { ... }`, après `"btc_uptrend": True,`, ajouter :

```python
        # Sub-scores bruts (non pondérés) — pour repondération future
        "score_tech_raw":     payload.get("score_tech", 0),
        "score_news_raw":     payload.get("score_news", 0),
        "score_ms_raw":       payload.get("score_ms", 0),
        "score_oc_raw":       payload.get("score_oc", 0),
        "score_cg_raw":       payload.get("score_cg", 0),
        "score_macro_raw":    payload.get("score_macro", 0),
        "score_tech_adj_raw": payload.get("score_tech_adj", payload.get("score_tech", 0)),
        "score_rs_raw":       payload.get("score_rs", 0),
        "score_social_raw":   payload.get("score_social", 0),
```

- [ ] **Modifier `store_trade_outcome` dans `ruflo_memory.py`**

Après le calcul de `pnl` et `ticker`, ajouter avant le dict `outcome` :

```python
    # Exit quality : % du pic capturé
    try:
        peak_pnl = get_peak_pnl(ticker)
        if peak_pnl > 0 and pnl > 0:
            exit_quality = round(pnl / peak_pnl * 100, 1)
        else:
            exit_quality = None
    except Exception:
        exit_quality = None
```

Puis dans le dict `outcome = { ... }`, ajouter :
```python
        "exit_quality": exit_quality,
```

- [ ] **Vérifier que les tests passent**

```bash
python -m pytest tests/test_ruflo_memory.py -v 2>&1
```

- [ ] **Commit**

```bash
git add ruflo_memory.py tests/test_ruflo_memory.py
git commit -m "feat: stocker sub-scores bruts + exit_quality pour apprentissage futur"
```

---

## Task 4 — EV médiane rolling + threshold dynamique

**Fichiers :**
- Modifier : `ruflo_memory.py` (ajouter `get_rolling_ev`)
- Modifier : `capital_allocator.py` (utiliser l'EV pour ajuster le threshold)

- [ ] **Écrire les tests**

Ajouter dans `tests/test_ruflo_memory.py` :

```python
def test_get_rolling_ev_positif(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    outcomes = [
        {"ticker": f"T{i}", "pnl_pct": 12.0, "outcome": "win"}  for i in range(8)
    ] + [
        {"ticker": f"L{i}", "pnl_pct": -7.0, "outcome": "loss"} for i in range(7)
    ]
    (tmp_path / "mem.json").write_text(json.dumps({"outcomes": outcomes, "entries": []}))

    result = rm.get_rolling_ev(n_trades=15)
    assert result["ev"] is not None
    # 8 wins à +12%, 7 losses à -7%
    # WR=0.533, med_win=12, med_loss=7 → EV = 0.533*12 - 0.467*7 = 6.4 - 3.27 = 3.13
    assert result["ev"] > 0, f"EV devrait être positif : {result['ev']}"
    assert result["mode"] == "aggressive"

def test_get_rolling_ev_negatif(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    outcomes = [
        {"ticker": f"T{i}", "pnl_pct": 12.0, "outcome": "win"}  for i in range(4)
    ] + [
        {"ticker": f"L{i}", "pnl_pct": -8.0, "outcome": "loss"} for i in range(11)
    ]
    (tmp_path / "mem.json").write_text(json.dumps({"outcomes": outcomes, "entries": []}))

    result = rm.get_rolling_ev(n_trades=15)
    assert result["ev"] < 0
    assert result["mode"] == "conservative"
```

- [ ] **Vérifier que les tests échouent**

```bash
python -m pytest tests/test_ruflo_memory.py::test_get_rolling_ev_positif tests/test_ruflo_memory.py::test_get_rolling_ev_negatif -v 2>&1
```

- [ ] **Ajouter `get_rolling_ev` dans `ruflo_memory.py`**

Après la fonction `clear_peak_pnl`, avant `# ── API publique`, ajouter :

```python
# ── EV Rolling (signal d'agressivité) ────────────────────────────────────────

# Seuils EV pour les modes (en % par trade)
EV_AGGRESSIVE_THRESHOLD =  2.0   # EV > 2% sur 5 cycles → agressif
EV_CONSERVATIVE_THRESHOLD = 0.0  # EV < 0% sur 3 cycles → conservateur

# Hysteresis : nombre de cycles consécutifs pour changer de mode
HYSTERESIS_TO_CONSERVATIVE = 3
HYSTERESIS_TO_NORMAL        = 3
HYSTERESIS_TO_AGGRESSIVE    = 5


def get_rolling_ev(n_trades: int = 15) -> dict:
    """
    Calcule l'EV médiane sur les n derniers trades réels.
    Utilise la médiane (robuste aux outliers type CHZ -22%).

    Returns dict avec :
        ev     : float (% par trade) ou None si < 5 trades
        mode   : "aggressive" | "normal" | "conservative"
        wr     : float win rate [0-1]
        nb     : int nombre de trades utilisés
    """
    data     = _load_json()
    outcomes = data.get("outcomes", [])[-n_trades:]
    nb       = len(outcomes)

    if nb < 5:
        return {"ev": None, "mode": "normal", "nb": nb, "wr": None}

    wins   = sorted([o["pnl_pct"] for o in outcomes if o.get("pnl_pct", 0) > 0])
    losses = sorted([abs(o["pnl_pct"]) for o in outcomes if o.get("pnl_pct", 0) <= 0])

    wr  = len(wins) / nb
    lr  = 1.0 - wr

    med_win  = wins[len(wins) // 2]   if wins   else 0.0
    med_loss = losses[len(losses) // 2] if losses else 0.0

    ev = round(wr * med_win - lr * med_loss, 2)

    # Mode avec hysteresis (stocké dans trade_memory pour persister entre runs)
    mode = _compute_ev_mode(ev, data)

    return {
        "ev":       ev,
        "mode":     mode,
        "wr":       round(wr, 3),
        "med_win":  round(med_win, 2),
        "med_loss": round(med_loss, 2),
        "nb":       nb,
    }


def _compute_ev_mode(ev: float, data: dict) -> str:
    """
    Applique l'hysteresis pour éviter les flip-flops de mode.
    Lit et met à jour ev_history dans trade_memory.json.
    """
    ev_history = data.setdefault("ev_history", [])
    ev_history.append(round(ev, 2))
    # Garder les 10 dernières valeurs
    data["ev_history"] = ev_history[-10:]

    recent = data["ev_history"]

    # Mode conservateur : EV < 0 sur les 3 derniers calculs
    if len(recent) >= HYSTERESIS_TO_CONSERVATIVE:
        if all(v < EV_CONSERVATIVE_THRESHOLD for v in recent[-HYSTERESIS_TO_CONSERVATIVE:]):
            return "conservative"

    # Mode agressif : EV > 2% sur les 5 derniers calculs
    if len(recent) >= HYSTERESIS_TO_AGGRESSIVE:
        if all(v > EV_AGGRESSIVE_THRESHOLD for v in recent[-HYSTERESIS_TO_AGGRESSIVE:]):
            return "aggressive"

    return "normal"
```

- [ ] **Utiliser l'EV dans `capital_allocator.py`**

Après `ROTATION_USDC_RATIO = 0.60` (ligne ~33), ajouter :

```python
# Ajustements threshold selon EV rolling
EV_MODE_THRESHOLD_DELTA = {
    "conservative": +0.15,   # seuil plus élevé → moins d'entrées
    "normal":        0.00,
    "aggressive":   -0.10,   # seuil plus bas → plus d'entrées
}
EV_MODE_SIZE_MULT = {
    "conservative": 0.80,
    "normal":       1.00,
    "aggressive":   1.10,
}
```

Dans la fonction `calculate_allocation`, après `score_abs = abs(score)`, ajouter :

```python
    # ── Ajustement EV rolling ────────────────────────────────────────────────
    try:
        import ruflo_memory as rm_ev
        ev_data     = rm_ev.get_rolling_ev()
        ev_mode     = ev_data.get("mode", "normal")
        ev_size_mult = EV_MODE_SIZE_MULT.get(ev_mode, 1.0)
        if ev_mode != "normal":
            logger.info(
                f"[Allocateur] Mode EV={ev_mode} "
                f"(EV={ev_data.get('ev')}%/trade, WR={ev_data.get('wr', 0):.0%}) "
                f"→ taille ×{ev_size_mult}"
            )
    except Exception:
        ev_size_mult = 1.0
```

Et juste avant `HARD_CAP_PCT`, modifier :
```python
    target_usdt = portfolio_value * base_pct * mem_mult * ev_size_mult
```

- [ ] **Vérifier que les tests passent**

```bash
python -m pytest tests/test_ruflo_memory.py -v 2>&1
```

- [ ] **Commit**

```bash
git add ruflo_memory.py capital_allocator.py tests/test_ruflo_memory.py
git commit -m "feat: EV médiane rolling avec hysteresis — threshold et taille adaptatifs"
```

---

## Task 5 — Mode observation (double condition BTC + régime)

**Fichiers :**
- Modifier : `scanner.py` fonction `run_scan` (au début, après le filtre BTC MA50 existant)

- [ ] **Écrire le test**

Créer `tests/test_mode_observation.py` :

```python
import sys
sys.path.insert(0, ".")

def test_mode_observation_retourne_bool():
    """is_observation_mode() doit toujours retourner un bool, jamais lever."""
    from scanner import is_observation_mode
    result = is_observation_mode()
    assert isinstance(result, bool)

def test_mode_observation_false_si_btc_ok(monkeypatch):
    """Si BTC est au-dessus de la MA50, mode observation = False."""
    import scanner
    monkeypatch.setattr("position_manager.is_btc_uptrend", lambda: True)
    assert scanner.is_observation_mode() == False
```

- [ ] **Vérifier que le test échoue**

```bash
python -m pytest tests/test_mode_observation.py -v 2>&1
```

- [ ] **Ajouter `is_observation_mode` dans `scanner.py`**

Après la fonction `is_cooldown_active` (ligne ~85), ajouter :

```python
def is_observation_mode() -> bool:
    """
    Mode observation : aucune nouvelle entrée.
    Conditions : BTC sous MA50 daily ET régime HMM = 'bear' simultanément.
    Une seule condition → pas d'observation (juste threshold +0.2 via EV).
    Les exits (trailing stop, P2.5, TP) continuent normalement.
    """
    try:
        from position_manager import is_btc_uptrend
        if is_btc_uptrend():
            return False  # BTC bullish → jamais en observation

        # BTC sous MA50 : vérifier le régime HMM sur BTC
        import regime_detector as rd_local
        import okx_client as okx_local
        btc_ohlcv = okx_local.get_ohlcv("BTC", days=90)
        if btc_ohlcv is None or len(btc_ohlcv) < 20:
            return False  # Données insuffisantes → conservateur mais pas bloquant
        regime_btc = rd_local.analyze(btc_ohlcv)
        return regime_btc.get("regime", "sideways") == "bear"
    except Exception as e:
        logger.debug(f"[ObservationMode] check échoué : {e}")
        return False
```

- [ ] **Utiliser `is_observation_mode` dans `run_scan`**

Dans `run_scan`, après le bloc `if not is_btc_uptrend()` existant (ligne ~212), ajouter JUSTE AVANT `universe = get_universe()` :

```python
    # ── Mode observation (double condition) : BTC sous MA50 ET HMM bear ───────
    if is_observation_mode():
        logger.info(
            "🔭 Mode observation actif — BTC sous MA50 ET régime bear. "
            "Aucune nouvelle entrée. Exits et position_manager continuent normalement."
        )
        return []
```

- [ ] **Vérifier que les tests passent**

```bash
python -m pytest tests/test_mode_observation.py -v 2>&1
```

- [ ] **Commit**

```bash
git add scanner.py tests/test_mode_observation.py
git commit -m "feat: mode observation — blocage entrées si BTC bear + HMM bear"
```

---

## Task 6 — Re-entry graduated threshold (par ticker)

**Fichiers :**
- Modifier : `ruflo_memory.py` (ajouter set/get reentry_threshold)
- Modifier : `position_manager.py` (appeler set_reentry_threshold après un stop)

- [ ] **Écrire les tests**

Ajouter dans `tests/test_ruflo_memory.py` :

```python
def test_reentry_threshold_petite_perte(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    rm.set_reentry_threshold("KAIA", loss_pct=-4.0)
    threshold = rm.get_reentry_threshold("KAIA")
    assert threshold == 1.7, f"Perte -4% → seuil 1.7, obtenu {threshold}"

def test_reentry_threshold_grande_perte(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    rm.set_reentry_threshold("CHZ", loss_pct=-12.0)
    assert rm.get_reentry_threshold("CHZ") == 2.2

def test_reentry_threshold_expire(tmp_path, monkeypatch):
    import ruflo_memory as rm
    from datetime import datetime, timezone
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    # Stocker avec timestamp déjà expiré
    import json, time
    data = {"outcomes": [], "entries": [], "reentry_thresholds": {
        "BTC": {"threshold": 2.0, "expires": time.time() - 1, "loss_pct": -8.0}
    }}
    (tmp_path / "mem.json").write_text(json.dumps(data))

    result = rm.get_reentry_threshold("BTC")
    assert result is None, "Threshold expiré doit retourner None"
```

- [ ] **Vérifier que les tests échouent**

```bash
python -m pytest tests/test_ruflo_memory.py::test_reentry_threshold_petite_perte tests/test_ruflo_memory.py::test_reentry_threshold_grande_perte tests/test_ruflo_memory.py::test_reentry_threshold_expire -v 2>&1
```

- [ ] **Ajouter `set_reentry_threshold` et `get_reentry_threshold` dans `ruflo_memory.py`**

Après la fonction `_compute_ev_mode`, avant `# ── API publique`, ajouter :

```python
# ── Re-entry graduated threshold ─────────────────────────────────────────────

_REENTRY_TABLE = [
    (-10.0, 2.2),  # perte > 10% → seuil 2.2
    (-5.0,  1.9),  # perte 5-10% → seuil 1.9
    (0.0,   1.7),  # perte < 5%  → seuil 1.7
]
REENTRY_DURATION_SECONDS = 4 * 3600  # 4h


def set_reentry_threshold(ticker: str, loss_pct: float):
    """
    Enregistre un seuil d'entrée temporaire après un stop loss.
    loss_pct doit être négatif (ex : -7.5 pour -7.5%).
    """
    threshold = 1.7  # défaut
    for min_loss, thr in _REENTRY_TABLE:
        if loss_pct <= min_loss:
            threshold = thr
            break

    data = _load_json()
    data.setdefault("reentry_thresholds", {})[ticker] = {
        "threshold": threshold,
        "expires":   time.time() + REENTRY_DURATION_SECONDS,
        "loss_pct":  round(loss_pct, 2),
    }
    _save_json(data)
    logger.info(
        f"[ReEntry] {ticker} : perte {loss_pct:+.1f}% → "
        f"threshold temporaire {threshold} pendant 4h"
    )


def get_reentry_threshold(ticker: str) -> float | None:
    """
    Retourne le seuil d'entrée temporaire si actif, None sinon.
    Nettoie automatiquement les entrées expirées.
    """
    data = _load_json()
    entry = data.get("reentry_thresholds", {}).get(ticker)
    if not entry:
        return None
    if time.time() > entry["expires"]:
        del data["reentry_thresholds"][ticker]
        _save_json(data)
        return None
    return entry["threshold"]
```

- [ ] **Appeler `set_reentry_threshold` dans `position_manager.py` après un stop**

Chercher dans `position_manager.py` les endroits où `decision == "FULL_SELL"` est déclenché par un stop ATR (P1). Chercher la chaîne `"Stop ATR"` ou `"P1"` :

```bash
grep -n "Stop ATR\|raison.*P1\|ATR.*stop" position_manager.py | head -20
```

Après l'appel à `execute_decision` (ou dans `evaluate_position` avant le return), si la raison contient "Stop ATR", ajouter :

```python
                # Re-entry graduated : relever le seuil pour ce ticker pendant 4h
                if "Stop ATR" in raison or "Stop Loss" in raison:
                    try:
                        import ruflo_memory as rm
                        rm.set_reentry_threshold(ticker, pnl_pct or 0.0)
                    except Exception:
                        pass
```

- [ ] **Vérifier où injecter dans position_manager.py**

```bash
grep -n "FULL_SELL\|execute_decision\|raison" position_manager.py | head -30
```

- [ ] **Vérifier que les tests passent**

```bash
python -m pytest tests/test_ruflo_memory.py -v 2>&1
```

- [ ] **Utiliser le threshold dans `scanner.py`**

Dans la boucle `for ticker, tech in candidates.items()`, au début du bloc try (après le filtre `is_cooldown_active`), ajouter :

```python
            # ── Re-entry threshold : vérifier si ce ticker vient d'être stoppé ─
            try:
                import ruflo_memory as rm_reentry
                reentry_thr = rm_reentry.get_reentry_threshold(ticker)
                if reentry_thr is not None:
                    effective_threshold = reentry_thr
                    logger.info(
                        f"{ticker} : threshold temporaire {reentry_thr} actif "
                        f"(stop récent) — seuil relevé"
                    )
                else:
                    effective_threshold = AUTO_EXECUTE_THRESHOLD
            except Exception:
                effective_threshold = AUTO_EXECUTE_THRESHOLD
```

Et dans le filtre score à la fin de Phase 1, remplacer :
```python
            if abs(score_final) < SIGNAL_THRESHOLD:
```
Par :
```python
            if abs(score_final) < SIGNAL_THRESHOLD:
```
*(inchangé — le threshold relevé est utilisé en Phase 3 pour l'exécution, pas pour l'alerte)*

Et dans Phase 3, remplacer `payload["score"] >= AUTO_EXECUTE_THRESHOLD` par :
```python
            try:
                reentry_thr_exec = rm.get_reentry_threshold(payload["ticker"])
                exec_threshold = reentry_thr_exec if reentry_thr_exec else AUTO_EXECUTE_THRESHOLD
            except Exception:
                exec_threshold = AUTO_EXECUTE_THRESHOLD

            if payload["score"] >= exec_threshold and payload.get("trade_autorise"):
```

- [ ] **Commit**

```bash
git add ruflo_memory.py position_manager.py scanner.py tests/test_ruflo_memory.py
git commit -m "feat: re-entry graduated — seuil temporaire après stop loss"
```

---

## Task 7 — Shadow portfolio near-misses

**Fichiers :**
- Modifier : `ruflo_memory.py` (add_near_miss, check_near_miss_outcomes)
- Modifier : `scanner.py` (logger les near-misses en fin de Phase 1)

- [ ] **Écrire les tests**

Ajouter dans `tests/test_ruflo_memory.py` :

```python
def test_add_near_miss_cap_15(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    # Ajouter 20 near-misses → doit rester plafonné à 15
    for i in range(20):
        rm.add_near_miss(f"T{i}", score=1.3, prix=1.0, trade_type="swing")

    import json
    data = json.loads((tmp_path / "mem.json").read_text())
    assert len(data["shadow_portfolio"]) <= 15

def test_add_near_miss_deduplique(tmp_path, monkeypatch):
    import ruflo_memory as rm
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")
    (tmp_path / "mem.json").write_text('{"outcomes":[],"entries":[]}')

    rm.add_near_miss("KAIA", score=1.3, prix=0.05, trade_type="swing")
    rm.add_near_miss("KAIA", score=1.4, prix=0.052, trade_type="swing")  # update

    import json
    data = json.loads((tmp_path / "mem.json").read_text())
    kaias = [s for s in data["shadow_portfolio"] if s["ticker"] == "KAIA"]
    assert len(kaias) == 1, "KAIA doit être dédupliqué"
    assert kaias[0]["score"] == 1.4  # version la plus récente
```

- [ ] **Vérifier que les tests échouent**

```bash
python -m pytest tests/test_ruflo_memory.py::test_add_near_miss_cap_15 tests/test_ruflo_memory.py::test_add_near_miss_deduplique -v 2>&1
```

- [ ] **Ajouter les fonctions shadow portfolio dans `ruflo_memory.py`**

Avant `# ── API publique`, ajouter :

```python
# ── Shadow Portfolio ──────────────────────────────────────────────────────────

SHADOW_MAX        = 15       # Max near-misses actifs
SHADOW_GOOD_PCT   = 8.0      # +8% = opportunité manquée significative
SHADOW_MEASURE_SCALP_H  = 4  # Mesurer les scalps après 4h
SHADOW_MEASURE_SWING_H  = 48 # Mesurer les swings après 48h


def add_near_miss(ticker: str, score: float, prix: float, trade_type: str):
    """
    Enregistre un token scanné mais non acheté (score entre 1.0 et le seuil).
    trade_type : 'scalp' (score ≥ 2.0) ou 'swing' (score < 2.0)
    Plafond : 15 near-misses actifs. Déduplique par ticker.
    """
    data   = _load_json()
    shadow = data.get("shadow_portfolio", [])

    # Dédupliquer — garder la version la plus récente
    shadow = [s for s in shadow if s.get("ticker") != ticker]

    shadow.append({
        "ticker":    ticker,
        "score":     round(score, 2),
        "prix_ref":  prix,
        "type":      trade_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "outcome":   None,
        "pnl_pct":   None,
    })

    # Garder les 15 plus récents
    data["shadow_portfolio"] = shadow[-SHADOW_MAX:]
    _save_json(data)


def check_near_miss_outcomes() -> dict:
    """
    Vérifie les résultats des near-misses arrivés à maturité.
    Appelé à chaque cycle 4h depuis run_once.py.
    Retourne {"missed": int, "correct": int} pour les logs.
    """
    data   = _load_json()
    shadow = data.get("shadow_portfolio", [])
    now    = time.time()

    missed  = 0
    correct = 0
    updated = []

    for nm in shadow:
        if nm.get("outcome") is not None:
            updated.append(nm)
            continue

        try:
            ts    = datetime.fromisoformat(nm["timestamp"]).timestamp()
            age_h = (now - ts) / 3600
            measure_h = (SHADOW_MEASURE_SCALP_H
                         if nm.get("type") == "scalp"
                         else SHADOW_MEASURE_SWING_H)

            if age_h < measure_h:
                updated.append(nm)
                continue

            # Mesurer le résultat
            import okx_client as okx_shadow
            prix_actuel = okx_shadow.get_price_usdc(nm["ticker"])
            if prix_actuel and nm.get("prix_ref", 0) > 0:
                pnl_pct = (prix_actuel - nm["prix_ref"]) / nm["prix_ref"] * 100
                was_missed = pnl_pct > SHADOW_GOOD_PCT
                nm["outcome"] = "missed" if was_missed else "correct"
                nm["pnl_pct"] = round(pnl_pct, 2)
                if was_missed:
                    missed += 1
                    logger.info(
                        f"[Shadow] {nm['ticker']} : opportunité manquée "
                        f"+{pnl_pct:.1f}% (score refus={nm['score']:.2f})"
                    )
                else:
                    correct += 1
            else:
                nm["outcome"] = "error"

        except Exception as e:
            logger.debug(f"[Shadow] check {nm.get('ticker')} : {e}")
            nm["outcome"] = "error"

        updated.append(nm)

    data["shadow_portfolio"] = updated
    _save_json(data)

    if missed + correct > 0:
        logger.info(f"[Shadow] {missed} opportunités manquées / {correct} refus corrects")

    return {"missed": missed, "correct": correct}
```

- [ ] **Logger les near-misses dans `scanner.py`**

Dans la boucle Phase 1, juste avant `continue` pour les tokens avec score < SIGNAL_THRESHOLD, ajouter :

```python
            if abs(score_final) < SIGNAL_THRESHOLD:
                # Logger comme near-miss si score suffisamment proche du seuil
                if abs(score_final) >= 1.0:
                    try:
                        import ruflo_memory as rm_shadow
                        nm_type = "scalp" if score_final >= 2.0 else "swing"
                        rm_shadow.add_near_miss(
                            ticker     = ticker,
                            score      = score_final,
                            prix       = tech.get("prix_actuel", 0),
                            trade_type = nm_type,
                        )
                    except Exception:
                        pass
                logger.info(f"{ticker} : score {score_final:+.2f} < {SIGNAL_THRESHOLD} — ignoré")
                continue
```

- [ ] **Appeler `check_near_miss_outcomes` dans `run_once.py`**

```bash
grep -n "seed_ruflo\|run_scan\|def run" run_once.py | head -20
```

Ajouter après `seed_ruflo_from_json()` (ou en début de cycle) :

```python
    # Shadow portfolio : vérifier les near-misses arrivés à maturité
    try:
        import ruflo_memory as rm
        rm.check_near_miss_outcomes()
    except Exception as e:
        logger.debug(f"Shadow portfolio check : {e}")
```

- [ ] **Vérifier que les tests passent**

```bash
python -m pytest tests/test_ruflo_memory.py -v 2>&1
```

- [ ] **Commit**

```bash
git add ruflo_memory.py scanner.py run_once.py tests/test_ruflo_memory.py
git commit -m "feat: shadow portfolio — tracker les near-misses pour calibrage futur"
```

---

## Task 8 — Dual entry path (scalp 30min via pending_signals)

**Fichiers :**
- Modifier : `scanner.py` (stocker pending_signals en fin de Phase 1)
- Modifier : `alert_scanner.py` (ajouter entry_scan_scalp)
- Modifier : `ruflo_memory.py` (get/set pending_signals)

- [ ] **Écrire les tests**

Créer `tests/test_dual_entry.py` :

```python
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
    # Seulement XYZ (score ≥ 2.0) doit être stocké pour les scalps
    scalp_tickers = [p["ticker"] for p in pending]
    assert "XYZ" in scalp_tickers

def test_pending_signals_expire(tmp_path, monkeypatch):
    import ruflo_memory as rm, time
    monkeypatch.setattr(rm, "MEMORY_FILE", tmp_path / "mem.json")

    data = {"outcomes": [], "entries": [], "pending_signals": [
        {"ticker": "OLD", "score": 2.5, "prix_ref": 1.0,
         "timestamp": "2020-01-01T00:00:00+00:00", "ttl": 14400}
    ]}
    (tmp_path / "mem.json").write_text(json.dumps(data))

    actifs = rm.get_active_pending_signals()
    assert len(actifs) == 0, "Signal expiré ne doit pas être retourné"
```

- [ ] **Vérifier que les tests échouent**

```bash
python -m pytest tests/test_dual_entry.py -v 2>&1
```

- [ ] **Ajouter `store_pending_signals` et `get_active_pending_signals` dans `ruflo_memory.py`**

Avant `# ── API publique`, ajouter :

```python
# ── Pending Signals (dual entry path) ────────────────────────────────────────

PENDING_SIGNAL_MIN_SCORE = 2.0   # Seuil minimum pour entrée rapide 30min
PENDING_SIGNAL_TTL       = 14400  # 4h en secondes


def store_pending_signals(actionable: list[dict]):
    """
    Stocke les signaux forts (score ≥ 2.0) pour l'entrée rapide 30min.
    Appelé depuis scanner.py en fin de Phase 1.
    """
    pending = []
    for p in actionable:
        if p.get("score", 0) >= PENDING_SIGNAL_MIN_SCORE:
            pending.append({
                "ticker":    p["ticker"],
                "score":     p["score"],
                "prix_ref":  p.get("prix", 0),
                "vol_regime": p.get("vol_regime", "normal"),
                "regime":    p.get("regime", "sideways"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ttl":       PENDING_SIGNAL_TTL,
                "stop_atr":  p.get("stop"),
            })

    data = _load_json()
    data["pending_signals"] = pending
    _save_json(data)

    if pending:
        logger.info(
            f"[Pending] {len(pending)} signal(s) stocké(s) pour entrée 30min : "
            f"{[p['ticker'] for p in pending]}"
        )


def get_active_pending_signals() -> list[dict]:
    """
    Retourne les signaux encore valides (dans le TTL).
    Nettoie les signaux expirés.
    """
    data    = _load_json()
    pending = data.get("pending_signals", [])
    now     = time.time()
    actifs  = []

    for p in pending:
        try:
            ts  = datetime.fromisoformat(p["timestamp"]).timestamp()
            age = now - ts
            if age <= p.get("ttl", PENDING_SIGNAL_TTL):
                actifs.append(p)
        except Exception:
            continue

    if len(actifs) != len(pending):
        data["pending_signals"] = actifs
        _save_json(data)

    return actifs
```

- [ ] **Appeler `store_pending_signals` dans `scanner.py` en fin de Phase 1**

Après `actionable.sort(...)` (ligne ~516), ajouter :

```python
    # Stocker les signaux forts pour l'entrée rapide 30min (dual entry path)
    try:
        import ruflo_memory as rm_pending
        rm_pending.store_pending_signals(actionable)
    except Exception:
        pass
```

- [ ] **Ajouter `entry_scan_scalp` dans `alert_scanner.py`**

Trouver la fin du fichier `alert_scanner.py`. Ajouter après la dernière fonction :

```python
# ─────────────────────────────────────────────────────────────────────────────
# Dual entry path — entrées scalp depuis pending_signals (30min)
# ─────────────────────────────────────────────────────────────────────────────

SCALP_PRICE_TOLERANCE = 0.02   # ±2% du prix de référence
SCALP_TP_PCT          = 5.0    # Take profit scalp
SCALP_STOP_PCT        = -3.0   # Stop loss scalp fixe
SCALP_TIME_STOP_H     = 4      # Exit si pas de mouvement après 4h


def entry_scan_scalp() -> list[str]:
    """
    Vérifie les pending_signals toutes les 30min.
    Entre sur les signaux qui :
    1. Sont encore dans le TTL
    2. Ont un prix dans ±2% du prix de référence
    3. Ne sont pas déjà en position
    4. Ont un re-entry threshold respecté

    Retourne la liste des tickers entrés.
    """
    entered = []
    try:
        import ruflo_memory as rm
        import okx_client as okx
        import position_manager as pm
        import execution
        import capital_allocator as ca

        pending = rm.get_active_pending_signals()
        if not pending:
            return []

        open_positions = pm.get_open_positions()
        open_tickers   = {p["ticker"] for p in open_positions}

        try:
            balances = okx.get_balances()
            usdc_available = float(balances.get("USDC", 0))
            portfolio_value = usdc_available + sum(
                p.get("valeur_usd", 0) for p in open_positions
            )
        except Exception:
            return []

        for signal in pending:
            ticker   = signal["ticker"]
            prix_ref = signal.get("prix_ref", 0)
            score    = signal.get("score", 0)

            # Skip si déjà en position
            if ticker in open_tickers:
                continue

            # Re-entry threshold
            try:
                reentry_thr = rm.get_reentry_threshold(ticker)
                if reentry_thr and score < reentry_thr:
                    logger.info(
                        f"[Scalp] {ticker} : bloqué re-entry "
                        f"(threshold={reentry_thr}, score={score:.2f})"
                    )
                    continue
            except Exception:
                pass

            # Vérifier le prix actuel
            try:
                prix_actuel = okx.get_price_usdc(ticker)
                if not prix_actuel or prix_ref <= 0:
                    continue
                drift = abs(prix_actuel - prix_ref) / prix_ref
                if drift > SCALP_PRICE_TOLERANCE:
                    logger.info(
                        f"[Scalp] {ticker} : prix dérivé {drift:.1%} "
                        f"(ref={prix_ref:.5f}, actuel={prix_actuel:.5f}) — skip"
                    )
                    continue
            except Exception:
                continue

            # Calculer la taille
            try:
                alloc = ca.calculate_allocation(
                    ticker          = ticker,
                    score           = score,
                    portfolio_value = portfolio_value,
                    usdc_available  = usdc_available,
                    open_positions  = open_positions,
                )
                taille = alloc.get("taille_allouee", 0)
                if taille < 20:
                    continue
            except Exception:
                continue

            # Construire le payload scalp
            stop_price = round(prix_actuel * (1 + SCALP_STOP_PCT / 100), 6)
            target_price = round(prix_actuel * (1 + SCALP_TP_PCT / 100), 6)

            payload = {
                "ticker":          ticker,
                "score":           score,
                "prix":            prix_actuel,
                "stop":            stop_price,
                "target":          target_price,
                "taille_allouee":  taille,
                "trade_autorise":  True,
                "regime":          signal.get("regime", "sideways"),
                "vol_regime":      signal.get("vol_regime", "normal"),
                "trade_type":      "scalp",   # Taggé pour exits différenciés
                "source":          "scalp_30min",
            }

            logger.info(
                f"[Scalp] {ticker} : entrée rapide "
                f"score={score:.2f} prix={prix_actuel:.5f} "
                f"taille=${taille:.0f} stop={SCALP_STOP_PCT}% TP={SCALP_TP_PCT}%"
            )

            try:
                execution.execute_signal(payload, portfolio_value)
                if payload.get("ordre_execute"):
                    rm.store_trade_entry(payload)
                    entered.append(ticker)
            except Exception as e:
                logger.error(f"[Scalp] {ticker} entrée échouée : {e}")

    except Exception as e:
        logger.error(f"entry_scan_scalp erreur globale : {e}")

    return entered
```

- [ ] **Appeler `entry_scan_scalp` dans la fonction principale de `alert_scanner.py`**

Chercher la fonction principale qui est appelée par le workflow GitHub Actions (probablement `run()` ou `main()`):

```bash
grep -n "def run\|def main\|emergency_stop_check" alert_scanner.py | tail -20
```

Ajouter l'appel à `entry_scan_scalp()` après `emergency_stop_check()` :

```python
    # Dual entry path : entrées scalp sur signaux haute conviction
    try:
        scalp_entered = entry_scan_scalp()
        if scalp_entered:
            logger.info(f"[Scalp] Entrées 30min : {scalp_entered}")
    except Exception as e:
        logger.error(f"entry_scan_scalp : {e}")
```

- [ ] **Vérifier que les tests passent**

```bash
python -m pytest tests/test_dual_entry.py -v 2>&1
```

- [ ] **Run complet des tests**

```bash
python -m pytest tests/ -v 2>&1
```
Attendu : tous les tests PASS

- [ ] **Commit**

```bash
git add ruflo_memory.py scanner.py alert_scanner.py tests/test_dual_entry.py
git commit -m "feat: dual entry path — entrées scalp haute conviction en 30min"
```

---

## Vérification finale

- [ ] **Pousser vers GitHub**

```bash
git push origin main
```

- [ ] **Vérifier le prochain cycle GitHub Actions**

Surveiller les logs du run suivant sur https://github.com/NIC-CLH/trading-bot/actions :
- Task 1-3 : pas d'erreurs nouvelles dans les logs
- Task 4 : voir apparaître `[Allocateur] Mode EV=...` dans les logs
- Task 5 : voir `🔭 Mode observation` si BTC en bear, sinon silencieux
- Task 6 : voir `[ReEntry]` après un prochain stop loss
- Task 7 : voir `[Shadow]` après 48h
- Task 8 : voir `[Scalp]` si signal ≥ 2.0 dans le prochain cycle 4h

- [ ] **Mettre à jour le second cerveau**

```
C:\Users\DARCKHEL\iCloudDrive\Second Cerveau de Nico\02 - Projets\Bot Crypto — Portfolio Manager.md
```
