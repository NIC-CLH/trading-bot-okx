from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_COINGECKO_URL = "https://api.coingecko.com/api/v3/global"
_TIMEOUT = 10
_STATE_FILE = Path(__file__).parent / "trade_memory.json"

# Snapshot précédent en mémoire (cache session)
_prev_usdt_d: float | None = None


def _load_prev_usdt_d() -> float | None:
    """Lit la dernière valeur USDT.D persistée dans trade_memory.json."""
    try:
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            return data.get("_sd_prev_usdt_d")
    except Exception:
        pass
    return None


def _save_prev_usdt_d(value: float) -> None:
    """Persiste la dernière valeur USDT.D dans trade_memory.json."""
    try:
        data: dict = {}
        if _STATE_FILE.exists():
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        data["_sd_prev_usdt_d"] = value
        _STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.debug("_save_prev_usdt_d : %s", exc)


def _fetch_coingecko() -> float | None:
    """Retourne la dominance USDT en % depuis CoinGecko, ou None si échec."""
    try:
        resp = requests.get(_COINGECKO_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return float(data["data"]["market_cap_percentage"]["usdt"])
    except Exception as exc:
        logger.warning("CoinGecko indisponible : %s", exc)
        return None


def _compute_score(usdt_d: float, change_24h: float) -> tuple[float, list[str]]:
    """Calcule le score [-1, +1] et les signaux associés."""
    signals: list[str] = []
    score: float

    if usdt_d > 6.0 and change_24h > 0.3:
        score = -1.0
        signals.append(f"USDT.D très élevée ({usdt_d:.2f}%) + hausse rapide (+{change_24h:.2f}pp/24h)")
        signals.append("Fuite massive vers les stablecoins — extrême prudence")
    elif usdt_d > 5.5:
        score = -0.5
        signals.append(f"USDT.D élevée ({usdt_d:.2f}%) — pression baissière sur les alts")
    elif usdt_d < 3.5:
        score = 1.0
        signals.append(f"USDT.D très faible ({usdt_d:.2f}%) — capital massivement dans les alts")
        signals.append("Environnement très favorable aux positions longues")
    elif usdt_d < 4.0:
        score = 0.5
        signals.append(f"USDT.D basse ({usdt_d:.2f}%) — contexte bullish")
    else:
        score = 0.0
        signals.append(f"USDT.D neutre ({usdt_d:.2f}%) — pas de signal macro fort")

    if change_24h > 0.3 and score > -1.0:
        signals.append(f"Attention : USDT.D monte de {change_24h:.2f}pp en 24h")
    elif change_24h < -0.3:
        signals.append(f"USDT.D en baisse de {abs(change_24h):.2f}pp en 24h — tendance positive")

    return score, signals


def analyze() -> dict:
    """
    Mesure la dominance des stablecoins (USDT.D) comme filtre macro.

    Retourne :
        score      : float [-1.0, +1.0]
        usdt_d     : float — % dominance actuelle
        change_24h : float — variation 24h en points de %
        verdict    : str
        signals    : list[str]
    """
    global _prev_usdt_d

    usdt_d = _fetch_coingecko()

    if usdt_d is None:
        result = {
            "score": 0.0,
            "usdt_d": 0.0,
            "change_24h": 0.0,
            "verdict": "indisponible",
            "signals": ["API indisponible — filtre macro désactivé"],
        }
        logger.info("stablecoin_dominance | %s", result)
        return result

    # Utilise la valeur en mémoire d'abord, sinon celle persistée sur disque
    prev = _prev_usdt_d if _prev_usdt_d is not None else _load_prev_usdt_d()
    change_24h = round(usdt_d - prev, 4) if prev is not None else 0.0
    _prev_usdt_d = usdt_d
    _save_prev_usdt_d(usdt_d)

    score, signals = _compute_score(usdt_d, change_24h)

    if score >= 0.5:
        verdict = "bullish"
    elif score <= -0.5:
        verdict = "bearish"
    else:
        verdict = "neutre"

    result = {
        "score": score,
        "usdt_d": round(usdt_d, 4),
        "change_24h": change_24h,
        "verdict": verdict,
        "signals": signals,
    }
    logger.info("stablecoin_dominance | %s", result)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
    import json
    print(json.dumps(analyze(), indent=2, ensure_ascii=False))
