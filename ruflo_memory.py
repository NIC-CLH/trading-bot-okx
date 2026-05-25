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


def store_trade_entry(payload: dict):
    """
    Enregistre le contexte d'entrée après un achat confirmé.
    Appelé depuis scanner.py quand payload['ordre_execute'] == True.
    """
    entry = {
        "type":        "trade_entry",
        "ticker":      payload["ticker"],
        "score":       payload.get("score", 0),
        "score_tech":  payload.get("score_tech", 0),
        "score_news":  payload.get("score_news", 0),
        "score_ms":    payload.get("score_ms", 0),
        "score_macro": payload.get("score_macro", 0),
        "regime":      payload.get("regime", "unknown"),
        "vol_regime":  payload.get("vol_regime", "normal"),
        "taille_usd":  round(payload.get("taille_allouee", payload.get("taille_usd", 0)), 2),
        "prix":        payload.get("prix", 0),
        "btc_uptrend": True,  # filtre 50MA passé — toujours True ici
        "timestamp":   datetime.now(timezone.utc).isoformat(),
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
