"""
Client Kraken — remplace Binance pour l'exécution des ordres.
Lecture des balances, prix, OHLCV et passage d'ordres Spot.
"""

import logging
import os
import time
import urllib.parse
import hashlib
import hmac
import base64
from datetime import datetime, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

KRAKEN_API_URL = "https://api.kraken.com"
KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_SECRET = os.getenv("KRAKEN_SECRET", "")

# Mapping ticker -> paire Kraken
TICKER_TO_KRAKEN = {
    "XRP": "XRPUSDC",
    "BTC": "XBTUSDC",
    "ETH": "ETHUSDC",
    "SOL": "SOLUSDC",
    "ARB": "ARBUSD",
    "TIA": "TIAUSD",
    "JTO": "JTOUSD",
    "LINK": "LINKUSDC",
    "AVAX": "AVAXUSDC",
    "DOT": "DOTUSDC",
    "NEAR": "NEARUSD",
    "AAVE": "AAVEUSD",
    "UNI": "UNIUSD",
    "ATOM": "ATOMUSDC",
    "USDC": "USDC",
}


# ─── Authentification ─────────────────────────────────────────────────────────

def _sign(urlpath: str, data: dict, secret: str) -> str:
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = urlpath.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


def _private(endpoint: str, data: dict = None) -> dict:
    """Appel API privé Kraken (authentifié)."""
    if not KRAKEN_API_KEY or not KRAKEN_SECRET:
        raise EnvironmentError("KRAKEN_API_KEY ou KRAKEN_SECRET manquant dans .env")

    url = f"{KRAKEN_API_URL}/0/private/{endpoint}"
    data = data or {}
    data["nonce"] = str(int(time.time() * 1000))
    urlpath = f"/0/private/{endpoint}"

    headers = {
        "API-Key": KRAKEN_API_KEY,
        "API-Sign": _sign(urlpath, data, KRAKEN_SECRET),
    }

    resp = requests.post(url, headers=headers, data=data, timeout=10)
    resp.raise_for_status()
    result = resp.json()

    if result.get("error"):
        raise Exception(f"Kraken API error : {result['error']}")

    return result.get("result", {})


def _public(endpoint: str, params: dict = None) -> dict:
    """Appel API public Kraken (non authentifié)."""
    url = f"{KRAKEN_API_URL}/0/public/{endpoint}"
    resp = requests.get(url, params=params or {}, timeout=10)
    resp.raise_for_status()
    result = resp.json()

    if result.get("error"):
        raise Exception(f"Kraken API error : {result['error']}")

    return result.get("result", {})


# ─── Balances ────────────────────────────────────────────────────────────────

def get_balances() -> dict[str, float]:
    """Retourne les balances non nulles {ticker: quantite}."""
    data = _private("Balance")
    balances = {}
    for asset, qty in data.items():
        qty = float(qty)
        if qty > 0:
            # Normalisation des noms Kraken (XBT->BTC, XXRP->XRP, etc.)
            clean = asset.lstrip("XZ") if len(asset) == 4 else asset
            clean = clean.replace("XBT", "BTC")
            balances[clean] = qty
    logger.info(f"Balances Kraken : {balances}")
    return balances


# ─── Prix ────────────────────────────────────────────────────────────────────

def get_price_usdc(ticker: str) -> float | None:
    """Prix spot en USDC pour un ticker."""
    pair = TICKER_TO_KRAKEN.get(ticker.upper())
    if not pair or pair == "USDC":
        return 1.0
    try:
        data = _public("Ticker", {"pair": pair})
        for key, val in data.items():
            return float(val["c"][0])  # last trade price
    except Exception as e:
        logger.warning(f"Prix Kraken indisponible pour {ticker} : {e}")
        return None


# ─── OHLCV ───────────────────────────────────────────────────────────────────

def get_ohlcv(ticker: str, days: int = 90) -> pd.DataFrame:
    """Bougies journalières depuis Kraken."""
    pair = TICKER_TO_KRAKEN.get(ticker.upper())
    if not pair:
        logger.warning(f"Paire Kraken inconnue pour {ticker}")
        return pd.DataFrame()

    since = int((datetime.utcnow() - timedelta(days=days)).timestamp())

    try:
        data = _public("OHLC", {"pair": pair, "interval": 1440, "since": since})
        # Kraken retourne {pair_name: [[time,open,high,low,close,vwap,volume,count]]}
        rows = list(data.values())[0]
    except Exception as e:
        logger.warning(f"OHLCV Kraken {ticker} : {e}")
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.set_index("time", inplace=True)

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    return df[["open", "high", "low", "close", "volume"]]


def get_all_ohlcv(tickers: list[str], days: int = 90) -> dict[str, pd.DataFrame]:
    """OHLCV pour tous les actifs disponibles sur Kraken."""
    import config
    result = {}
    for ticker in tickers:
        if ticker.lower() in config.STABLECOINS:
            continue
        if ticker not in TICKER_TO_KRAKEN:
            logger.info(f"{ticker} non disponible sur Kraken — ignoré")
            continue
        df = get_ohlcv(ticker, days)
        if not df.empty:
            result[ticker] = df
            logger.info(f"OHLCV Kraken {ticker} : {len(df)} bougies")
        time.sleep(0.5)
    return result


# ─── Passage d'ordres ────────────────────────────────────────────────────────

def place_order(
    ticker: str,
    side: str,          # "buy" ou "sell"
    volume: float,      # quantité en token
    order_type: str = "market",
    limit_price: float = None,
    take_profit: float = None,
    stop_loss: float = None,
) -> dict:
    """
    Passe un ordre Spot sur Kraken avec TP/SL optionnels.
    Retourne le résultat de l'ordre.
    """
    pair = TICKER_TO_KRAKEN.get(ticker.upper())
    if not pair:
        raise ValueError(f"Paire Kraken inconnue pour {ticker}")

    data = {
        "pair": pair,
        "type": side,
        "ordertype": order_type if not limit_price else "limit",
        "volume": str(round(volume, 8)),
    }

    if limit_price:
        data["price"] = str(round(limit_price, 6))

    # TP/SL via ordres conditionnels Kraken
    if take_profit and stop_loss:
        data["close[ordertype]"] = "stop-loss-limit"
        data["close[price]"] = str(round(stop_loss * 0.995, 6))   # SL déclenche
        data["close[price2]"] = str(round(stop_loss * 0.990, 6))  # SL limite

    try:
        result = _private("AddOrder", data)
        logger.info(f"Ordre Kraken placé : {side} {volume} {ticker} — {result}")
        return result
    except Exception as e:
        logger.error(f"Erreur ordre Kraken {ticker} : {e}")
        raise


def cancel_order(txid: str) -> dict:
    """Annule un ordre ouvert par son ID."""
    return _private("CancelOrder", {"txid": txid})


def get_open_orders() -> dict:
    """Retourne les ordres ouverts."""
    return _private("OpenOrders")


def get_trade_history() -> dict:
    """Retourne l'historique des trades."""
    return _private("TradesHistory")
