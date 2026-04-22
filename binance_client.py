"""
Client Binance centralisé — toutes les interactions API passent par ici.
Charge les clés depuis .env, ne les expose jamais dans les logs.
"""

import logging
import os
import time
from datetime import datetime, timedelta
from functools import lru_cache

import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv()


def get_client() -> Client:
    """Initialise et retourne le client Binance authentifié."""
    api_key = os.getenv("BINANCE_API_KEY")
    secret_key = os.getenv("BINANCE_SECRET_KEY")

    if not api_key or not secret_key:
        raise EnvironmentError("Clés Binance manquantes dans .env")

    client = Client(api_key, secret_key)
    return client


# ─── Balances spot ────────────────────────────────────────────────────────────

def get_spot_balances(client: Client, min_usd_value: float = 1.0) -> dict[str, float]:
    """
    Retourne les balances spot non nulles {ticker: quantite}.
    Filtre les dust (valeur < min_usd_value USD approximatif).
    """
    try:
        account = client.get_account()
    except BinanceAPIException as e:
        logger.error(f"Erreur get_account : {e}")
        return {}

    balances = {}
    for asset in account["balances"]:
        free = float(asset["free"])
        locked = float(asset["locked"])
        total = free + locked
        if total > 0:
            balances[asset["asset"]] = total

    # Filtrage des dust via prix BTC
    prices = get_all_prices(client)
    filtered = {}
    for ticker, qty in balances.items():
        usdt_pair = f"{ticker}USDT"
        price = prices.get(usdt_pair) or prices.get(f"{ticker}BUSD")
        if price:
            if qty * price >= min_usd_value:
                filtered[ticker] = qty
        elif ticker in ("USDT", "BUSD", "USDC", "DAI", "FDUSD"):
            if qty >= min_usd_value:
                filtered[ticker] = qty

    logger.info(f"Balances spot : {len(filtered)} actifs")
    return filtered


def get_earn_balances(client: Client) -> dict[str, float]:
    """
    Récupère les balances Simple Earn (Flexible + Locked) via l'API Binance.
    Ces actifs n'apparaissent pas dans get_account().
    """
    earn_balances = {}

    # Flexible savings
    try:
        page = 1
        while True:
            resp = client._request_margin_api(
                "get", "lending/daily/token/position", True,
                data={"size": 100, "current": page}
            )
            if not resp:
                break
            for item in resp:
                asset = item.get("asset", "")
                qty = float(item.get("totalAmount", 0) or 0)
                if qty > 0:
                    earn_balances[asset] = earn_balances.get(asset, 0) + qty
            if len(resp) < 100:
                break
            page += 1
    except Exception as e:
        logger.debug(f"Simple Earn Flexible indisponible : {e}")

    # Locked savings / staking
    try:
        resp = client._request_margin_api(
            "get", "lending/project/position/list", True,
            data={"size": 100, "current": 1, "status": "HOLDING"}
        )
        if resp:
            for item in resp.get("rows", []):
                asset = item.get("asset", "")
                qty = float(item.get("amount", 0) or 0)
                if qty > 0:
                    earn_balances[asset] = earn_balances.get(asset, 0) + qty
    except Exception as e:
        logger.debug(f"Simple Earn Locked indisponible : {e}")

    if earn_balances:
        logger.info(f"Simple Earn : {len(earn_balances)} actifs ({list(earn_balances.keys())})")
    return earn_balances


def get_all_balances(client: Client, min_usd_value: float = 1.0) -> dict[str, float]:
    """Fusionne balances Spot + Simple Earn."""
    spot = get_spot_balances(client, min_usd_value)
    earn = get_earn_balances(client)

    merged = dict(spot)
    for ticker, qty in earn.items():
        merged[ticker] = merged.get(ticker, 0) + qty

    logger.info(f"Balances totales (Spot + Earn) : {len(merged)} actifs")
    return merged


# ─── Prix spot ────────────────────────────────────────────────────────────────

def get_all_prices(client: Client) -> dict[str, float]:
    """Récupère tous les prix spot en une seule requête."""
    try:
        tickers = client.get_all_tickers()
        return {t["symbol"]: float(t["price"]) for t in tickers}
    except BinanceAPIException as e:
        logger.error(f"Erreur get_all_tickers : {e}")
        return {}


def get_prices_usd(client: Client, tickers: list[str]) -> dict[str, float]:
    """Prix en USD pour une liste de tickers."""
    all_prices = get_all_prices(client)
    result = {}

    for ticker in tickers:
        ticker = ticker.upper()
        # Stablecoins
        if ticker in ("USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD"):
            result[ticker] = 1.0
            continue
        # Paire USDT directe
        if f"{ticker}USDT" in all_prices:
            result[ticker] = all_prices[f"{ticker}USDT"]
        # Paire via BTC
        elif f"{ticker}BTC" in all_prices and "BTCUSDT" in all_prices:
            result[ticker] = all_prices[f"{ticker}BTC"] * all_prices["BTCUSDT"]
        else:
            logger.warning(f"Prix introuvable pour {ticker}")

    return result


# ─── OHLCV ───────────────────────────────────────────────────────────────────

def get_ohlcv(
    client: Client,
    ticker: str,
    interval: str = Client.KLINE_INTERVAL_1DAY,
    days: int = 365,
) -> pd.DataFrame:
    """
    Récupère les bougies journalières depuis Binance.
    Retourne un DataFrame avec colonnes : open, high, low, close, volume.
    """
    symbol = f"{ticker.upper()}USDT"
    start_str = (datetime.utcnow() - timedelta(days=days)).strftime("%d %b %Y")

    try:
        klines = client.get_historical_klines(symbol, interval, start_str)
    except BinanceAPIException as e:
        # Essai avec BUSD
        try:
            symbol = f"{ticker.upper()}BUSD"
            klines = client.get_historical_klines(symbol, interval, start_str)
        except BinanceAPIException:
            logger.warning(f"OHLCV indisponible pour {ticker} : {e}")
            return pd.DataFrame()

    if not klines:
        return pd.DataFrame()

    df = pd.DataFrame(klines, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])

    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    df.set_index("timestamp", inplace=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    df = df[["open", "high", "low", "close", "volume"]]
    df.index = df.index.normalize()
    df = df[~df.index.duplicated(keep="last")]

    return df


def get_all_ohlcv(
    client: Client,
    tickers: list[str],
    days: int = 365,
    exclude_stables: bool = True,
) -> dict[str, pd.DataFrame]:
    """OHLCV pour tous les actifs du portefeuille."""
    import config
    result = {}
    for ticker in tickers:
        if exclude_stables and ticker.lower() in config.STABLECOINS:
            continue
        df = get_ohlcv(client, ticker, days=days)
        if not df.empty:
            result[ticker] = df
            logger.info(f"OHLCV {ticker} : {len(df)} bougies")
        time.sleep(0.1)  # Binance rate limit très permissif
    return result


# ─── Historique des trades (prix d'achat moyen) ───────────────────────────────

def get_avg_buy_price(client: Client, ticker: str) -> dict:
    """
    Calcule le prix d'achat moyen pondéré depuis l'historique Binance.
    Limité aux 500 derniers trades par paire.
    """
    symbol = f"{ticker.upper()}USDT"
    try:
        trades = client.get_my_trades(symbol=symbol, limit=500)
    except BinanceAPIException as e:
        logger.warning(f"Historique trades {ticker} indisponible : {e}")
        return {}

    if not trades:
        return {}

    buy_trades = [t for t in trades if t["isBuyer"]]
    if not buy_trades:
        return {}

    total_qty = sum(float(t["qty"]) for t in buy_trades)
    total_cost = sum(float(t["qty"]) * float(t["price"]) for t in buy_trades)
    avg_price = total_cost / total_qty if total_qty > 0 else 0

    first_buy = min(int(t["time"]) for t in buy_trades)
    first_date = datetime.utcfromtimestamp(first_buy / 1000).date()

    return {
        "prix_achat_moyen": round(avg_price, 8),
        "date_premier_achat": first_date,
        "nb_trades": len(buy_trades),
        "quantite_achetee_totale": round(total_qty, 8),
    }
