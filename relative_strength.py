from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_NEUTRAL = {"score": 0.0, "rs_ratio": 1.0, "ticker_ret": 0.0, "btc_ret": 0.0, "verdict": "neutre"}


def get_relative_strength(
    ticker: str,
    ohlcv_data: dict,
    days: int = 14,
) -> dict:
    """
    Retourne un dict avec :
        score      : float [-1.0, +1.0] — force relative normalisée
        rs_ratio   : float — performance ticker / performance BTC sur `days` jours
        ticker_ret : float — return du ticker sur `days` jours (%)
        btc_ret    : float — return de BTC sur `days` jours (%)
        verdict    : str — "fort" | "neutre" | "faible"

    Logique :
    - Si BTC n'est pas dans ohlcv_data → retourne score=0, verdict="neutre"
    - performance = (close[-1] - close[-days]) / close[-days]
    - rs_ratio = ticker_perf / btc_perf (si btc_perf != 0)
    - score normalisé : rs_ratio > 1.5 → +1.0, entre 0.5 et 1.5 → interpolé, < 0.5 → -1.0
    - verdict : score > 0.3 → "fort", score < -0.3 → "faible", sinon "neutre"
    - Si données insuffisantes ou erreur → retourne score=0, verdict="neutre"
    """
    try:
        btc_key = next((k for k in ohlcv_data if k.upper().startswith("BTC")), None)
        if btc_key is None:
            logger.debug("relative_strength(%s): BTC absent des données → neutre", ticker)
            return dict(_NEUTRAL)

        if ticker not in ohlcv_data:
            logger.debug("relative_strength(%s): ticker absent des données → neutre", ticker)
            return dict(_NEUTRAL)

        df_ticker: pd.DataFrame = ohlcv_data[ticker]
        df_btc: pd.DataFrame = ohlcv_data[btc_key]

        if len(df_ticker) < days or len(df_btc) < days:
            logger.debug("relative_strength(%s): données insuffisantes → neutre", ticker)
            return dict(_NEUTRAL)

        close_t = df_ticker["close"].iloc[-days:]
        close_b = df_btc["close"].iloc[-days:]

        ticker_ret = (close_t.iloc[-1] - close_t.iloc[0]) / close_t.iloc[0]
        btc_ret = (close_b.iloc[-1] - close_b.iloc[0]) / close_b.iloc[0]

        if abs(btc_ret) < 1e-6:
            rs_ratio = 1.0
        else:
            rs_ratio = float(ticker_ret / btc_ret)

        # Normalisation du score sur [-1, +1]
        if rs_ratio >= 1.5:
            score = 1.0
        elif rs_ratio <= 0.5:
            score = -1.0
        else:
            # Interpolation linéaire entre 0.5 → -1.0 et 1.5 → +1.0
            score = float(np.interp(rs_ratio, [0.5, 1.5], [-1.0, 1.0]))

        if score > 0.3:
            verdict = "fort"
        elif score < -0.3:
            verdict = "faible"
        else:
            verdict = "neutre"

        result = {
            "score": round(score, 4),
            "rs_ratio": round(rs_ratio, 4),
            "ticker_ret": round(float(ticker_ret) * 100, 4),
            "btc_ret": round(float(btc_ret) * 100, 4),
            "verdict": verdict,
        }
        logger.debug("relative_strength(%s): %s", ticker, result)
        return result

    except Exception as exc:
        logger.debug("relative_strength(%s): erreur inattendue (%s) → neutre", ticker, exc)
        return dict(_NEUTRAL)
