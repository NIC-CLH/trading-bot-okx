from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


def is_correlated(
    ticker: str,
    open_positions: list[dict],
    ohlcv_data: dict,
    threshold: float = 0.85,
    min_days: int = 20,
) -> tuple[bool, str]:
    """
    Retourne (True, raison) si le ticker est trop corrélé à une position existante.
    Retourne (False, "") si OK pour trader.
    """
    try:
        if ticker not in ohlcv_data:
            return False, ""

        df_ticker = ohlcv_data[ticker]
        if df_ticker is None or len(df_ticker) < min_days:
            return False, ""

        returns_ticker = df_ticker["close"].pct_change().dropna().iloc[-30:]

        if len(returns_ticker) < min_days:
            return False, ""

        for position in open_positions:
            pos_ticker = position.get("ticker")
            if not pos_ticker or pos_ticker == ticker:
                continue
            if pos_ticker not in ohlcv_data:
                continue

            df_pos = ohlcv_data[pos_ticker]
            if df_pos is None or len(df_pos) < min_days:
                continue

            returns_pos = df_pos["close"].pct_change().dropna().iloc[-30:]

            if len(returns_pos) < min_days:
                continue

            aligned_ticker, aligned_pos = returns_ticker.align(returns_pos, join="inner")

            if len(aligned_ticker) < min_days:
                continue

            corr = aligned_ticker.corr(aligned_pos)

            logger.info(
                "Corrélation %s / %s : %.4f (seuil=%.2f)",
                ticker,
                pos_ticker,
                corr,
                threshold,
            )

            if corr > threshold:
                reason = (
                    f"{ticker} trop corrélé avec {pos_ticker} "
                    f"(corrélation={corr:.4f} > seuil={threshold})"
                )
                return True, reason

        return False, ""

    except Exception as exc:
        logger.warning("Erreur calcul corrélation pour %s : %s", ticker, exc)
        return False, "corrélation non calculable"
