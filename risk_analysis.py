"""
Module 2 — Analyse de Risque
Volatilité, corrélations, VaR, Drawdown, Sharpe & Sortino.
"""

import time
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests

import config

logger = logging.getLogger(__name__)

TRADING_DAYS_YEAR = 365  # crypto trade 24/7


# ─── Données OHLCV CoinGecko ─────────────────────────────────────────────────

def fetch_ohlcv(coingecko_id: str, days: int = config.OHLCV_DAYS) -> pd.DataFrame:
    """Récupère les données OHLCV journalières depuis CoinGecko."""
    url = f"{config.COINGECKO_BASE_URL}/coins/{coingecko_id}/market_chart"
    params = {"vs_currency": "usd", "days": days, "interval": "daily"}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Erreur OHLCV {coingecko_id}: {e}")
        return pd.DataFrame()

    prices = data.get("prices", [])
    volumes = data.get("total_volumes", [])

    if not prices:
        return pd.DataFrame()

    df = pd.DataFrame(prices, columns=["timestamp", "close"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)

    if volumes:
        vol_df = pd.DataFrame(volumes, columns=["timestamp", "volume"])
        vol_df["timestamp"] = pd.to_datetime(vol_df["timestamp"], unit="ms")
        vol_df.set_index("timestamp", inplace=True)
        df = df.join(vol_df, how="left")

    df.index = df.index.normalize()
    df = df[~df.index.duplicated(keep="last")]

    return df


def fetch_all_ohlcv(
    coingecko_ids: dict[str, str],
    days: int = config.OHLCV_DAYS,
    exclude_stables: bool = True,
) -> dict[str, pd.DataFrame]:
    """Récupère OHLCV pour tous les actifs non-stables."""
    result = {}
    for ticker, cg_id in coingecko_ids.items():
        if exclude_stables and ticker.lower() in config.STABLECOINS:
            continue
        logger.info(f"Téléchargement OHLCV : {ticker}")
        df = fetch_ohlcv(cg_id, days)
        if not df.empty:
            result[ticker] = df
        time.sleep(config.COINGECKO_DELAY)
    return result


# ─── Volatilité ──────────────────────────────────────────────────────────────

def compute_volatility(df: pd.DataFrame, window: int) -> float | None:
    """Volatilité historique annualisée sur `window` jours."""
    if len(df) < window + 1:
        return None
    returns = df["close"].pct_change().dropna().tail(window)
    return float(returns.std() * np.sqrt(TRADING_DAYS_YEAR))


def volatility_report(ohlcv_data: dict[str, pd.DataFrame]) -> dict[str, dict]:
    """Calcule la volatilité 30j et 90j pour chaque actif."""
    report = {}
    for ticker, df in ohlcv_data.items():
        report[ticker] = {
            "vol_30j": compute_volatility(df, 30),
            "vol_90j": compute_volatility(df, 90),
        }
    return report


# ─── Corrélations ────────────────────────────────────────────────────────────

def compute_correlation_matrix(ohlcv_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Matrice de corrélation des rendements journaliers (90 derniers jours)."""
    returns_dict = {}
    for ticker, df in ohlcv_data.items():
        if len(df) > 1:
            ret = df["close"].pct_change().dropna().tail(90)
            returns_dict[ticker] = ret

    if len(returns_dict) < 2:
        return pd.DataFrame()

    returns_df = pd.DataFrame(returns_dict).dropna()
    return returns_df.corr()


def avg_pairwise_correlation(corr_matrix: pd.DataFrame) -> float:
    """Corrélation moyenne entre les paires d'actifs (excl. diagonale)."""
    if corr_matrix.empty or len(corr_matrix) < 2:
        return 0.0
    vals = corr_matrix.values
    n = len(vals)
    mask = np.ones((n, n), dtype=bool)
    np.fill_diagonal(mask, False)
    return float(np.mean(np.abs(vals[mask])))


# ─── VaR ─────────────────────────────────────────────────────────────────────

def compute_portfolio_returns(
    ohlcv_data: dict[str, pd.DataFrame],
    weights: dict[str, float],
) -> pd.Series:
    """Rendements journaliers pondérés du portefeuille (hors stables)."""
    returns_dict = {}
    for ticker, df in ohlcv_data.items():
        if ticker in weights and len(df) > 1:
            returns_dict[ticker] = df["close"].pct_change().dropna()

    if not returns_dict:
        return pd.Series(dtype=float)

    returns_df = pd.DataFrame(returns_dict).dropna()
    w = np.array([weights.get(t, 0) for t in returns_df.columns])
    total_w = w.sum()
    if total_w == 0:
        return pd.Series(dtype=float)
    w = w / total_w

    portfolio_returns = returns_df.values @ w
    return pd.Series(portfolio_returns, index=returns_df.index)


def compute_var(
    portfolio_returns: pd.Series,
    portfolio_value: float,
) -> dict:
    """
    VaR historique à 95% et 99% sur 1 et 7 jours.
    Méthode : simulation historique (non-paramétrique).
    """
    if portfolio_returns.empty:
        return {}

    var_results = {}
    for conf in config.VAR_CONFIDENCE_LEVELS:
        alpha = 1 - conf
        for horizon in config.VAR_HORIZONS_DAYS:
            if horizon == 1:
                scaled_returns = portfolio_returns
            else:
                # Approximation racine du temps
                scaled_returns = portfolio_returns * np.sqrt(horizon)

            var_pct = float(np.percentile(scaled_returns, alpha * 100))
            cvar_pct = float(scaled_returns[scaled_returns <= var_pct].mean())

            key = f"VaR_{int(conf*100)}_{horizon}j"
            var_results[key] = {
                "var_pct": round(var_pct * 100, 2),
                "var_usd": round(var_pct * portfolio_value, 2),
                "cvar_pct": round(cvar_pct * 100, 2),
                "cvar_usd": round(cvar_pct * portfolio_value, 2),
            }

    return var_results


# ─── Drawdown ────────────────────────────────────────────────────────────────

def compute_max_drawdown(df: pd.DataFrame, since_date=None) -> dict:
    """
    Maximum Drawdown depuis la date d'achat (ou depuis le début de l'historique).
    Retourne le MDD en % et la date de peak/trough.
    """
    if df.empty:
        return {}

    series = df["close"]
    if since_date:
        series = series[series.index >= pd.Timestamp(since_date)]

    if series.empty:
        return {}

    rolling_max = series.expanding().max()
    drawdown = (series - rolling_max) / rolling_max
    mdd = float(drawdown.min())
    mdd_date = drawdown.idxmin()
    peak_date = series[:mdd_date].idxmax() if not series[:mdd_date].empty else None

    return {
        "max_drawdown_pct": round(mdd * 100, 2),
        "peak_date": str(peak_date.date()) if peak_date else None,
        "trough_date": str(mdd_date.date()),
        "current_drawdown_pct": round(float(drawdown.iloc[-1]) * 100, 2),
    }


# ─── Sharpe & Sortino ─────────────────────────────────────────────────────────

def compute_sharpe_sortino(
    portfolio_returns: pd.Series,
    risk_free_daily: float = 0.0,
) -> dict:
    """
    Sharpe et Sortino annualisés sur les 90 derniers jours.
    risk_free_daily : taux sans risque journalier (défaut 0 pour crypto).
    """
    if portfolio_returns.empty or len(portfolio_returns) < 30:
        return {"sharpe": None, "sortino": None}

    returns = portfolio_returns.tail(90)
    excess = returns - risk_free_daily
    avg_excess = excess.mean()
    std_all = excess.std()

    sharpe = float((avg_excess / std_all) * np.sqrt(TRADING_DAYS_YEAR)) if std_all > 0 else None

    downside = excess[excess < 0]
    std_down = downside.std()
    sortino = float((avg_excess / std_down) * np.sqrt(TRADING_DAYS_YEAR)) if std_down > 0 else None

    return {
        "sharpe": round(sharpe, 3) if sharpe else None,
        "sortino": round(sortino, 3) if sortino else None,
        "periode_jours": len(returns),
    }


# ─── Rapport complet ─────────────────────────────────────────────────────────

def run_from_ohlcv(snapshot: dict, ohlcv_data: dict) -> dict:
    """Exécute l'analyse de risque sur un ohlcv_data déjà chargé (Binance)."""

    # Poids par ticker (excl. stables) basés sur valeur actuelle
    weights = {}
    non_stable_value = sum(
        p["valeur_actuelle"] or 0
        for p in snapshot["positions"]
        if not p["is_stablecoin"] and p["valeur_actuelle"]
    )
    for pos in snapshot["positions"]:
        if not pos["is_stablecoin"] and pos["valeur_actuelle"] and non_stable_value > 0:
            weights[pos["ticker"]] = pos["valeur_actuelle"] / non_stable_value

    # Dates d'achat par ticker
    buy_dates = {
        p["ticker"]: p.get("date_achat") or None
        for p in snapshot["positions"]
    }

    # Volatilités
    vol_report = volatility_report(ohlcv_data)

    # Corrélations
    corr_matrix = compute_correlation_matrix(ohlcv_data)
    avg_corr = avg_pairwise_correlation(corr_matrix)

    # VaR
    port_returns = compute_portfolio_returns(ohlcv_data, weights)
    var_data = compute_var(port_returns, snapshot["valeur_totale_usd"])

    # Drawdowns individuels
    drawdowns = {}
    for ticker, df in ohlcv_data.items():
        since = buy_dates.get(ticker)
        drawdowns[ticker] = compute_max_drawdown(df, since_date=since)

    # Sharpe & Sortino portefeuille
    perf_metrics = compute_sharpe_sortino(port_returns)

    # Alertes corrélation
    correlation_alert = avg_corr > config.MAX_AVG_CORRELATION

    result = {
        "volatilites": vol_report,
        "correlation_matrix": corr_matrix.round(3).to_dict() if not corr_matrix.empty else {},
        "correlation_moyenne": round(avg_corr, 3),
        "correlation_alerte": correlation_alert,
        "var": var_data,
        "drawdowns": drawdowns,
        "performance_portefeuille": perf_metrics,
        "alertes_risque": [],
    }

    if correlation_alert:
        result["alertes_risque"].append(
            f"[CORRELATION] Corrélation moyenne {avg_corr:.2f} > seuil {config.MAX_AVG_CORRELATION} "
            f"— diversification insuffisante"
        )

    # Actifs très volatils (> 150% annualisé)
    for ticker, vols in vol_report.items():
        if vols["vol_30j"] and vols["vol_30j"] > 1.5:
            result["alertes_risque"].append(
                f"[VOLATILITE] {ticker} : vol 30j annualisée {vols['vol_30j']*100:.0f}%"
            )

    return result
