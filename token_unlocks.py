from __future__ import annotations

from datetime import datetime, timezone
import requests

# Fallback statique — format : {ticker: [{date: "YYYY-MM-DD", amount_pct: float}]}
# À mettre à jour manuellement si un unlock majeur est connu à l'avance.
# Source : tokenunlocks.app, cryptorank.io/unlocks, vestlab.io
KNOWN_UNLOCKS: dict = {}

# API publique — retourne JSON avec champ "unlocks" ou liste directe
# Alternative gratuite : https://vestlab.io/api  (même format)
_API_URLS = [
    "https://api.unlocks.app/v1/events",     # ?symbol=TICKER&limit=10
    "https://vestlab.io/api/unlocks",        # ?token=TICKER
]
_TIMEOUT = 10


def _days_until(date_str: str) -> int | None:
    """Retourne le nombre de jours jusqu'à une date ISO (YYYY-MM-DD), ou None si parsing échoue."""
    try:
        target = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        delta = (target - now).days
        return delta
    except (ValueError, TypeError):
        return None


def _score_from_unlock(days: int, amount_pct: float) -> tuple[float, list[str]]:
    """Calcule score et signals depuis un unlock imminent."""
    signals: list[str] = []
    score = 0.0

    if days <= 3 and amount_pct > 5.0:
        score = -1.0
        signals.append(
            f"Unlock CRITIQUE dans {days}j : {amount_pct:.1f}% du supply (entrée bloquée)"
        )
    elif days <= 7 and amount_pct > 3.0:
        score = -0.5
        signals.append(
            f"Unlock dans {days}j : {amount_pct:.1f}% du supply (risque de dump)"
        )
    else:
        signals.append(
            f"Unlock dans {days}j : {amount_pct:.1f}% du supply (impact limité)"
        )

    return score, signals


def _check_from_list(
    unlocks: list[dict],
) -> tuple[bool, int | None, float | None, float, list[str]]:
    """Analyse une liste d'unlocks et retourne (has_unlock, days_until, amount_pct, score, signals)."""
    upcoming = []
    for entry in unlocks:
        days = _days_until(entry.get("date", ""))
        if days is None:
            continue
        amount = float(entry.get("amount_pct", 0))
        if 0 <= days <= 7:
            upcoming.append((days, amount))

    if not upcoming:
        return False, None, None, 0.0, []

    # Prend l'unlock le plus proche
    upcoming.sort(key=lambda x: x[0])
    days, amount = upcoming[0]
    score, signals = _score_from_unlock(days, amount)
    return True, days, amount, score, signals


def check_unlock(ticker: str) -> dict:
    """Vérifie si un unlock de tokens est prévu dans les 7 prochains jours.
    Retourne : has_unlock, days_until, amount_pct, verdict, score (-1.0→0.0), signals."""
    ticker_up = ticker.upper()

    # Tentative sur plusieurs endpoints publics (le premier qui répond gagne)
    for api_url in _API_URLS:
        try:
            resp = requests.get(
                api_url,
                params={"token": ticker_up, "symbol": ticker_up, "limit": "10"},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 200:
                data = resp.json()
                unlocks_raw = data if isinstance(data, list) else (
                    data.get("unlocks") or data.get("events") or data.get("data") or []
                )
                if not isinstance(unlocks_raw, list):
                    continue
                has_unlock, days_until, amount_pct, score, signals = _check_from_list(
                    unlocks_raw
                )
                if has_unlock:
                    verdict = f"Unlock détecté via API ({ticker_up}) — score {score:+.1f}"
                else:
                    verdict = f"Aucun unlock imminent pour {ticker_up} (API)"
                return {
                    "has_unlock": has_unlock,
                    "days_until": days_until,
                    "amount_pct": amount_pct,
                    "verdict": verdict,
                    "score": score,
                    "signals": signals,
                }
        except Exception:
            continue  # essaie l'URL suivante

    # Fallback : liste statique
    if ticker_up in KNOWN_UNLOCKS:
        has_unlock, days_until, amount_pct, score, signals = _check_from_list(
            KNOWN_UNLOCKS[ticker_up]
        )
        if has_unlock:
            verdict = f"Unlock détecté (fallback statique) pour {ticker_up} — score {score:+.1f}"
        else:
            verdict = f"Aucun unlock imminent pour {ticker_up} (fallback)"
        return {
            "has_unlock": has_unlock,
            "days_until": days_until,
            "amount_pct": amount_pct,
            "verdict": verdict,
            "score": score,
            "signals": signals,
        }

    # Aucune donnée disponible
    return {
        "has_unlock": False,
        "days_until": None,
        "amount_pct": None,
        "verdict": f"Pas de données unlock pour {ticker_up} (API indisponible, non listé)",
        "score": 0.0,
        "signals": [],
    }
