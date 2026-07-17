"""
Mémoire d'apprentissage du bot — ruflo (vector DB) + JSON (persistance cross-runs).

Architecture deux couches :
  1. trade_memory.json  — fichier JSON commité dans le repo, persiste entre les runs GitHub Actions
  2. ruflo vector DB    — recherche sémantique HNSW, seedé depuis le JSON au démarrage

Points d'injection :
  - run_once.py        → seed_ruflo_from_json() au démarrage de chaque cycle
  - scanner.py         → get_ticker_memory() avant achat + store_trade_entry() après achat
  - position_manager.py→ store_trade_outcome() après chaque vente

Influence sur les décisions :
  - Win rate < 30% (>= 5 trades) → taille position réduite de 50%
  - Win rate >= 65% (>= 5 trades) → log "signal historiquement fort"
  - Jamais bloquant — toujours graceful fallback si ruflo indisponible
"""

import json
import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Chemins ───────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
MEMORY_FILE = BASE_DIR / "trade_memory.json"

# Détection dynamique de npx (Windows retourne npx.CMD, Linux retourne npx)
# shutil.which garantit que Python trouve le même exécutable que le shell.
_NPX = shutil.which("npx") or "npx"
RUFLO_CMD   = [_NPX, "--yes", "@claude-flow/cli@latest"]
RUFLO_TIMEOUT = 8   # 8s max par appel CLI — évite de bloquer le cycle 4h sur GitHub Actions
MAX_OUTCOMES = 500   # garder les N derniers outcomes en mémoire JSON
MAX_ENTRIES  = 200   # garder les N dernières entrées en mémoire JSON


# ── Couche ruflo CLI ──────────────────────────────────────────────────────────

def _ruflo(args: list[str]) -> tuple[bool, str]:
    """Appelle ruflo CLI. Silencieux en cas d'échec."""
    try:
        result = subprocess.run(
            RUFLO_CMD + args,
            capture_output=True, text=True,
            timeout=RUFLO_TIMEOUT,
            cwd=str(BASE_DIR),
        )
        return result.returncode == 0, result.stdout + result.stderr
    except Exception as e:
        logger.debug(f"[Memory] ruflo CLI indisponible : {e}")
        return False, str(e)


def _ruflo_store(key: str, value: dict) -> bool:
    """Stocke une entrée dans ruflo avec embeddings vectoriels."""
    ok, _ = _ruflo([
        "memory", "store",
        "-k", key,
        "--value", json.dumps(value, ensure_ascii=False),
    ])
    return ok


def _ruflo_get(key: str) -> dict | None:
    """Récupère une entrée complète depuis ruflo par sa clé."""
    ok, output = _ruflo(["memory", "get", "-k", key, "--format", "json"])
    if not ok:
        return None
    try:
        idx = output.find("{")
        if idx < 0:
            return None
        data = json.loads(output[idx:])
        content_str = data.get("content", "{}")
        return json.loads(content_str)
    except Exception as e:
        logger.debug(f"[Memory] ruflo get parse error ({key}) : {e}")
        return None


def _ruflo_search(query: str, limit: int = 20) -> list[dict]:
    """Recherche sémantique dans ruflo. Retourne liste de {key, score, content}.
    Note : le champ 'preview' du CLI est tronqué — on fetch chaque clé séparément.
    """
    ok, output = _ruflo([
        "memory", "search",
        "-q", query,
        "--limit", str(limit),
        "--format", "json",
    ])
    if not ok:
        return []
    try:
        idx = output.find("{")
        if idx < 0:
            return []
        data = json.loads(output[idx:])
        results = []
        for r in data.get("results", []):
            key   = r.get("key", "")
            score = r.get("score", 0)
            # Preview tronqué par le CLI → fetch complet par clé
            content = _ruflo_get(key)
            if content is None:
                continue
            results.append({
                "key":     key,
                "score":   score,
                "content": content,
            })
        return results
    except Exception as e:
        logger.debug(f"[Memory] ruflo search parse error : {e}")
        return []


# ── Couche JSON (persistance GitHub Actions) ──────────────────────────────────

def _load_json() -> dict:
    """Charge le fichier mémoire JSON. Retourne structure vide si absent."""
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"outcomes": [], "entries": []}


def _save_json(data: dict):
    """Sauvegarde le JSON en gardant uniquement les N derniers enregistrements."""
    try:
        data["outcomes"] = data["outcomes"][-MAX_OUTCOMES:]
        data["entries"]  = data["entries"][-MAX_ENTRIES:]
        MEMORY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.error(f"[Memory] Erreur écriture JSON : {e}")


# ── Peaks (trailing stop) ─────────────────────────────────────────────────────

def get_peak_pnl(ticker: str) -> float:
    """Retourne le pic de P&L historique d'une position ouverte (en %)."""
    return _load_json().get("peaks", {}).get(ticker, 0.0)


def update_peak_pnl(ticker: str, pnl_pct: float) -> bool:
    """Met à jour le peak si pnl_pct dépasse le précédent. Retourne True si mis à jour."""
    data = _load_json()
    peaks = data.setdefault("peaks", {})
    if pnl_pct > peaks.get(ticker, 0.0):
        peaks[ticker] = round(pnl_pct, 2)
        _save_json(data)
        return True
    return False


def clear_peak_pnl(ticker: str):
    """Supprime le peak d'une position après fermeture."""
    data = _load_json()
    if ticker in data.get("peaks", {}):
        del data["peaks"][ticker]
        _save_json(data)


# ── EV Rolling (signal d'agressivité) ────────────────────────────────────────

EV_AGGRESSIVE_THRESHOLD   =  2.0   # EV > 2% sur 5 cycles → agressif
EV_CONSERVATIVE_THRESHOLD =  0.0   # EV < 0% sur 3 cycles → conservateur
HYSTERESIS_TO_CONSERVATIVE = 3
HYSTERESIS_TO_AGGRESSIVE   = 5


def get_rolling_ev(n_trades: int = 15) -> dict:
    """
    Calcule l'EV médiane sur les n derniers trades réels.
    Utilise la médiane (robuste aux outliers type CHZ -22%).

    Returns dict avec :
        ev     : float (% par trade) ou None si < 5 trades
        mode   : "aggressive" | "normal" | "conservative"
        wr     : float win rate [0-1] ou None
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

    med_win  = wins[len(wins) // 2]     if wins   else 0.0
    med_loss = losses[len(losses) // 2] if losses else 0.0

    ev = round(wr * med_win - lr * med_loss, 2)

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
    data["ev_history"] = ev_history[-10:]
    _save_json(data)

    recent = data["ev_history"]

    if len(recent) >= HYSTERESIS_TO_CONSERVATIVE:
        if all(v < EV_CONSERVATIVE_THRESHOLD for v in recent[-HYSTERESIS_TO_CONSERVATIVE:]):
            return "conservative"

    if len(recent) >= HYSTERESIS_TO_AGGRESSIVE:
        if all(v > EV_AGGRESSIVE_THRESHOLD for v in recent[-HYSTERESIS_TO_AGGRESSIVE:]):
            return "aggressive"

    return "normal"


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


# ── Shadow Portfolio ──────────────────────────────────────────────────────────

SHADOW_MAX              = 15   # Max near-misses actifs
SHADOW_GOOD_PCT         = 8.0  # +8% = opportunité manquée significative
SHADOW_MEASURE_SCALP_H  = 4    # Mesurer les scalps après 4h
SHADOW_MEASURE_SWING_H  = 48   # Mesurer les swings après 48h


def add_near_miss(ticker: str, score: float, prix: float, trade_type: str):
    """
    Enregistre un token scanné mais non acheté (score entre 1.0 et le seuil).
    trade_type : 'scalp' (score >= 2.0) ou 'swing' (score < 2.0)
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
                pnl_pct     = (prix_actuel - nm["prix_ref"]) / nm["prix_ref"] * 100
                was_missed  = pnl_pct > SHADOW_GOOD_PCT
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


# ── API publique ──────────────────────────────────────────────────────────────

def seed_ruflo_from_json():
    """
    Charge l'historique JSON dans ruflo au démarrage de chaque cycle.
    Rend la recherche vectorielle disponible même sur un runner GitHub Actions frais.
    Appelé depuis run_once.py au tout début du cycle.
    """
    data = _load_json()
    total = len(data["outcomes"]) + len(data["entries"])
    if total == 0:
        logger.info("[Memory] Aucun historique à charger — démarrage à zéro")
        return

    _ruflo(["memory", "init"])  # no-op si déjà init

    loaded = 0
    # Charger les 100 derniers outcomes
    for i, outcome in enumerate(data["outcomes"][-100:]):
        key = f"outcome:{outcome.get('ticker', 'UNK')}:{i}"
        if _ruflo_store(key, outcome):
            loaded += 1

    # Charger les 50 dernières entrées
    for i, entry in enumerate(data["entries"][-50:]):
        key = f"entry:{entry.get('ticker', 'UNK')}:{i}"
        _ruflo_store(key, entry)

    logger.info(
        f"[Memory] Ruflo seedé depuis JSON : "
        f"{loaded} outcomes + {len(data['entries'][-50:])} entries chargés"
    )


def _f(val, default=0.0):
    """Coerce en float natif — les np.float64 de pandas font planter json.dump."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def store_trade_entry(payload: dict):
    """
    Enregistre le contexte d'entrée après un achat confirmé.
    Appelé depuis scanner.py quand payload['ordre_execute'] == True.

    Toutes les valeurs numériques sont coercées en float natif : un np.float64
    dans le payload faisait échouer la sérialisation JSON en silence, et
    l'auto-registration créait ensuite une entrée minimale sans sub-scores.
    """
    entry = {
        "type":        "trade_entry",
        "ticker":      payload["ticker"],
        "score":       _f(payload.get("score", 0)),
        "score_tech":  _f(payload.get("score_tech", 0)),
        "score_news":  _f(payload.get("score_news", 0)),
        "score_ms":    _f(payload.get("score_ms", 0)),
        "score_macro": _f(payload.get("score_macro", 0)),
        "regime":      payload.get("regime", "unknown"),
        "vol_regime":  payload.get("vol_regime", "normal"),
        "taille_usd":  round(_f(payload.get("taille_allouee", payload.get("taille_usd", 0))), 2),
        "prix":        _f(payload.get("prix", 0)),
        "stop":        _f(payload["stop"]) if payload.get("stop") is not None else None,
        "btc_uptrend": True,  # filtre 50MA passé — toujours True ici
        "late_entry":  bool(payload.get("late_entry", False)),  # True = auto-registration (sub-scores absents)
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        # Sub-scores bruts (non pondérés) — pour repondération future
        "score_tech_raw":     _f(payload.get("score_tech", 0)),
        "score_news_raw":     _f(payload.get("score_news", 0)),
        "score_ms_raw":       _f(payload.get("score_ms", 0)),
        "score_oc_raw":       _f(payload.get("score_oc", 0)),
        "score_cg_raw":       _f(payload.get("score_cg", 0)),
        "score_macro_raw":    _f(payload.get("score_macro", 0)),
        "score_tech_adj_raw": _f(payload.get("score_tech_adj", payload.get("score_tech", 0))),
        "score_rs_raw":       _f(payload.get("score_rs", 0)),
        "score_social_raw":   _f(payload.get("score_social", 0)),
    }

    # Persistance JSON
    data = _load_json()
    data["entries"].append(entry)
    _save_json(data)

    # Ruflo vector DB
    key = f"entry:{payload['ticker']}:{int(time.time() * 1000)}"
    _ruflo_store(key, entry)

    logger.info(
        f"[Memory] Entrée enregistrée : {payload['ticker']} "
        f"score={payload.get('score', 0):+.2f} régime={entry['regime']}"
    )


# ── Anti-churn : blacklist temporaire après stops répétés ─────────────────────
# Backtest 17/07/2026 : HYPE = 18 trades sur 69 (26%) — le bot re-rentrait en
# boucle sur le même token après chaque stop, payant les frais à chaque cycle.
CHURN_MAX_STOPS   = 2   # nombre de stops déclencheur
CHURN_WINDOW_DAYS = 7   # fenêtre d'observation ET durée de la blacklist


def is_churn_blacklisted(ticker: str) -> bool:
    """
    True si le ticker a pris >= CHURN_MAX_STOPS stops dans les
    CHURN_WINDOW_DAYS derniers jours → entrée interdite.
    """
    try:
        data = _load_json()
        cutoff = datetime.now(timezone.utc).timestamp() - CHURN_WINDOW_DAYS * 86400
        stops = 0
        for o in data.get("outcomes", []):
            if o.get("ticker") != ticker:
                continue
            reason = (o.get("exit_reason") or "").lower()
            if "stop" not in reason or "trailing" in reason or "time" in reason:
                continue
            try:
                ts = datetime.fromisoformat(o.get("timestamp", "")).timestamp()
            except ValueError:
                continue
            if ts >= cutoff:
                stops += 1
        if stops >= CHURN_MAX_STOPS:
            logger.info(f"[Anti-churn] {ticker} : {stops} stops en {CHURN_WINDOW_DAYS}j — blacklisté")
            return True
    except Exception as e:
        logger.debug(f"is_churn_blacklisted({ticker}) : {e}")
    return False


# ── Snapshot équity : historique de la valeur du portfolio ────────────────────

def record_equity(value: float, cap: int = 3000):
    """Enregistre la valeur du portfolio (appelé une fois par cycle 4h)."""
    try:
        data = _load_json()
        history = data.setdefault("equity_history", [])
        history.append({
            "date": datetime.now(timezone.utc).isoformat()[:16],
            "equity": round(float(value), 2),
        })
        if len(history) > cap:
            data["equity_history"] = history[-cap:]
        _save_json(data)
    except Exception as e:
        logger.debug(f"record_equity : {e}")


# ── Health check : détection des dimensions de scoring mortes ─────────────────
HEALTH_WARN_INTERVAL_DAYS = 7


def health_check_dimensions(payloads: list[dict]) -> list[str]:
    """
    Repère les dimensions de scoring restées à zéro sur TOUT le scan.
    Retourne la liste des dimensions mortes si un warning est dû
    (max 1 fois par HEALTH_WARN_INTERVAL_DAYS), sinon [].

    Leçon du 17/07/2026 : le score news est resté mort 2 mois sans alerte.
    """
    if not payloads:
        return []
    dims = ["score_news", "score_ms", "score_oc", "score_cg", "score_macro"]
    dead = [
        d for d in dims
        if all(abs(_f(p.get(d, 0))) < 1e-9 for p in payloads)
    ]
    if not dead:
        return []
    try:
        data = _load_json()
        last = data.get("health_last_warn", 0)
        now = time.time()
        if now - last < HEALTH_WARN_INTERVAL_DAYS * 86400:
            return []  # déjà alerté récemment
        data["health_last_warn"] = now
        _save_json(data)
    except Exception:
        pass
    return dead


def get_entry_stop(ticker: str) -> float | None:
    """
    Retourne le prix du stop ATR stocké au moment de l'entrée pour ce ticker.

    Utilité : évite le drift de stop dans emergency_stop_check.
    Sans cette fonction, le stop est recalculé à partir de l'ATR live → peut
    passer de -7% à -10% si la volatilité augmente après l'entrée.

    Retourne None si aucune entrée trouvée (fallback vers get_atr_stop live).
    """
    try:
        data = _load_json()
        matching = [
            e for e in data["entries"]
            if e.get("ticker") == ticker and e.get("stop") is not None
        ]
        if matching:
            return float(matching[-1]["stop"])
    except Exception as e:
        logger.debug(f"get_entry_stop({ticker}) : {e}")
    return None


def store_trade_outcome(decision: dict):
    """
    Enregistre le résultat d'un trade après une vente.
    Appelé depuis position_manager.execute_decision() après FULL_SELL réussi.

    Enrichit l'outcome avec le contexte d'entrée (régime, score, volatilité)
    récupéré depuis les entries JSON — indispensable pour le pattern matching cross-ticker.
    """
    # Ne pas enregistrer si P&L inconnu (position orpheline, vente sans historique d'entrée).
    # Stocker pnl=None comme "loss" biaiserait le win_rate des tickers concernés.
    if decision.get("pnl_pct") is None:
        logger.debug(
            f"[Memory] Outcome ignoré : {decision.get('ticker')} — P&L inconnu (orphelin)"
        )
        return

    # Note : pnl_pct est garanti non-None ici grâce au guard ci-dessus.
    # On évite `or 0` qui transformerait pnl=0.0 en 0 (int) et marquerait
    # un trade flat comme "loss".
    pnl    = decision.get("pnl_pct", 0)
    ticker = decision["ticker"]

    # Exit quality : % du pic capturé
    try:
        peak_pnl = get_peak_pnl(ticker)
        if peak_pnl and peak_pnl > 0 and pnl > 0:
            exit_quality = round(pnl / peak_pnl * 100, 1)
        else:
            exit_quality = None
    except Exception:
        exit_quality = None

    outcome = {
        "type":        "trade_outcome",
        "ticker":      ticker,
        "pnl_pct":     round(pnl, 2),
        "days_held":   round(decision.get("days_held") or 0, 1),
        "exit_reason": decision.get("raison", ""),
        "valeur_usd":  round(decision.get("valeur", 0), 2),
        "outcome":     "win" if pnl > 0 else "loss",
        "exit_quality": exit_quality,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }

    # Enrichir avec le contexte d'entrée (régime, score, vol) pour le pattern matching
    data = _load_json()
    matching = [e for e in data["entries"] if e.get("ticker") == ticker]
    if matching:
        last = matching[-1]
        outcome["regime"]      = last.get("regime", "unknown")
        outcome["vol_regime"]  = last.get("vol_regime", "normal")
        outcome["score_entry"] = last.get("score", 0)

    data["outcomes"].append(outcome)
    _save_json(data)

    # Ruflo vector DB
    key = f"outcome:{decision['ticker']}:{int(time.time() * 1000)}"
    _ruflo_store(key, outcome)

    label = "WIN  🟢" if outcome["outcome"] == "win" else "LOSS 🔴"
    logger.info(
        f"[Memory] Outcome enregistré : {decision['ticker']} "
        f"{label} {pnl:+.1f}% en {outcome['days_held']}j"
    )


def get_pattern_memory(
    score: float,
    regime: str,
    vol_regime: str,
    min_samples: int = 5,
) -> dict:
    """
    Recherche cross-ticker : trouve des trades passés dans des conditions
    de marché similaires (régime, niveau de score, volatilité).

    Utilisé quand le ticker n'a pas encore assez d'historique propre.
    Les ajustements sont plus conservateurs que le ticker-specific (±15%).

    Requiert que les outcomes soient enrichis avec regime/vol_regime
    (fait par store_trade_outcome depuis la session courante).
    """
    score_bucket = "strong" if abs(score) >= 2.0 else "moderate"
    query = (
        f"trade outcome {regime} regime {vol_regime} volatility "
        f"{score_bucket} signal crypto win loss"
    )

    ruflo_results = _ruflo_search(query, limit=40)

    outcomes = []
    seen = set()
    for r in ruflo_results:
        c = r["content"]
        if c.get("type") != "trade_outcome":
            continue
        # Filtrer par régime si l'outcome l'a stocké
        if c.get("regime") and c.get("regime") != "unknown" and c.get("regime") != regime:
            continue
        # Dédupliquer
        dedup = (c.get("ticker"), c.get("pnl_pct"), c.get("timestamp", "")[:13])
        if dedup in seen:
            continue
        seen.add(dedup)
        outcomes.append(c)

    nb = len(outcomes)
    if nb < min_samples:
        return {
            "win_rate":   None,
            "avg_pnl":    None,
            "nb_trades":  nb,
            "confidence": "insufficient",
        }

    wins    = sum(1 for o in outcomes if o.get("outcome") == "win")
    avg_pnl = sum(o.get("pnl_pct", 0) for o in outcomes) / nb
    win_rate = wins / nb

    return {
        "win_rate":   round(win_rate, 3),
        "avg_pnl":    round(avg_pnl, 2),
        "nb_trades":  nb,
        "confidence": "medium" if nb >= 10 else "low",
    }


def get_ticker_memory(ticker: str, min_samples: int = 3) -> dict:
    """
    Retourne les statistiques historiques pour un ticker.

    Sources (ordre de priorité) :
      1. JSON local — toujours disponible
      2. ruflo search — enrichit avec recherche sémantique

    Returns dict avec :
      win_rate   : float [0-1] ou None si < min_samples
      avg_pnl    : float (%) ou None
      nb_trades  : int
      avg_days   : float ou None
      confidence : "high" | "medium" | "low" | "insufficient"
      suggestion : str lisible pour les logs
    """
    # ── Source 1 : JSON ────────────────────────────────────────────────────────
    data     = _load_json()
    outcomes = [o for o in data["outcomes"] if o.get("ticker") == ticker]

    # ── Source 2 : ruflo search (déduplication par pnl + date) ───────────────
    ruflo_results = _ruflo_search(f"{ticker} trade outcome win loss crypto", limit=20)
    seen_keys = set()
    for r in ruflo_results:
        c = r["content"]
        if c.get("type") != "trade_outcome" or c.get("ticker") != ticker:
            continue
        dedup_key = (c.get("pnl_pct"), c.get("timestamp", "")[:13])
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        # Vérifier si déjà dans la liste JSON
        already = any(
            o.get("pnl_pct") == c.get("pnl_pct")
            and o.get("timestamp", "")[:13] == c.get("timestamp", "")[:13]
            for o in outcomes
        )
        if not already:
            outcomes.append(c)

    nb = len(outcomes)
    if nb < min_samples:
        return {
            "win_rate":   None,
            "avg_pnl":    None,
            "nb_trades":  nb,
            "avg_days":   None,
            "confidence": "insufficient",
            "suggestion": f"Données insuffisantes : {nb}/{min_samples} trades",
        }

    wins     = sum(1 for o in outcomes if o.get("outcome") == "win")
    avg_pnl  = sum(o.get("pnl_pct", 0) for o in outcomes) / nb
    avg_days = sum(o.get("days_held", 0) for o in outcomes) / nb
    win_rate = wins / nb

    confidence = "high" if nb >= 20 else "medium" if nb >= 10 else "low"

    if win_rate >= 0.65 and avg_pnl > 3:
        suggestion = (
            f"Signal historiquement fort sur {ticker} : "
            f"{win_rate:.0%} win rate, P&L moyen {avg_pnl:+.1f}% ({nb} trades)"
        )
    elif win_rate <= 0.30 and avg_pnl < -2:
        suggestion = (
            f"Historique faible sur {ticker} : "
            f"{win_rate:.0%} win rate, P&L moyen {avg_pnl:+.1f}% ({nb} trades) — taille réduite"
        )
    else:
        suggestion = (
            f"{ticker} neutre : {win_rate:.0%} win rate, "
            f"P&L moyen {avg_pnl:+.1f}% sur {nb} trades ({avg_days:.1f}j moy.)"
        )

    return {
        "win_rate":   round(win_rate, 3),
        "avg_pnl":    round(avg_pnl, 2),
        "nb_trades":  nb,
        "avg_days":   round(avg_days, 1),
        "confidence": confidence,
        "suggestion": suggestion,
    }


# ── Pending Signals (dual entry path) ────────────────────────────────────────

PENDING_SIGNAL_MIN_SCORE = 2.0    # Seuil minimum pour entrée rapide 30min
PENDING_SIGNAL_TTL       = 14400  # 4h en secondes


def store_pending_signals(actionable: list) -> None:
    """
    Stocke les signaux forts (score >= 2.0) pour l'entrée rapide 30min.
    Appelé depuis scanner.py en fin de Phase 1.
    Remplace entièrement pending_signals (nouveau cycle = nouveaux signaux).
    """
    pending = []
    for p in actionable:
        if p.get("score", 0) >= PENDING_SIGNAL_MIN_SCORE:
            pending.append({
                "ticker":     p["ticker"],
                "score":      p["score"],
                "prix_ref":   p.get("prix", 0),
                "vol_regime": p.get("vol_regime", "normal"),
                "regime":     p.get("regime", "sideways"),
                "timestamp":  datetime.now(timezone.utc).isoformat(),
                "ttl":        PENDING_SIGNAL_TTL,
                "stop_atr":   p.get("stop"),
            })

    data = _load_json()
    data["pending_signals"] = pending
    _save_json(data)

    if pending:
        logger.info(
            f"[Pending] {len(pending)} signal(s) stocké(s) pour entrée 30min : "
            f"{[p['ticker'] for p in pending]}"
        )


def get_active_pending_signals() -> list:
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
