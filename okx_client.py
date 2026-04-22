"""
Client OKX — exchange principal pour le trading autonome.
500+ paires USDT, MiCA France, API spot complète.
"""

import base64
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

OKX_BASE = "https://eea.okx.com"
QUOTE_CCY = "USDC"  # Compte EEA OKX — paires USDC (pas USDT)
API_KEY = os.getenv("OKX_API_KEY", "")
SECRET = os.getenv("OKX_SECRET", "")
PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")


# ─── Authentification ─────────────────────────────────────────────────────────

def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    msg = f"{timestamp}{method.upper()}{path}{body}"
    return base64.b64encode(
        hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def _headers(method: str, path: str, body: str = "") -> dict:
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
        "Content-Type": "application/json",
    }


def _get(path: str, params: dict = None) -> dict:
    # Les query params doivent être inclus dans le path signé
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        signed_path = f"{path}?{query}"
    else:
        signed_path = path

    url = OKX_BASE + signed_path
    resp = requests.get(url, headers=_headers("GET", signed_path), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise Exception(f"OKX API error : {data.get('msg')} (code {data.get('code')})")
    return data.get("data", [])


def _post(path: str, body: dict) -> dict:
    import json
    body_str = json.dumps(body)
    url = OKX_BASE + path
    resp = requests.post(url, headers=_headers("POST", path, body_str),
                         data=body_str, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise Exception(f"OKX API error : {data.get('msg')} (code {data.get('code')})")
    return data.get("data", [])


# ─── Balances ─────────────────────────────────────────────────────────────────

def get_balances() -> dict[str, float]:
    """Retourne les balances spot non nulles {ticker: quantite}."""
    data = _get("/api/v5/account/balance")
    balances = {}
    for account in data:
        for detail in account.get("details", []):
            qty = float(detail.get("availBal", 0))
            if qty > 0:
                balances[detail["ccy"]] = qty
    logger.info(f"Balances OKX : {balances}")
    return balances


# ─── Prix ─────────────────────────────────────────────────────────────────────

def get_price_usdt(ticker: str) -> float | None:
    """Prix spot en USDC (alias USDT pour compatibilité)."""
    return get_price_usdc(ticker)


def get_price_usdc(ticker: str) -> float | None:
    """Prix spot en USDC."""
    if ticker.upper() in ("USDT", "USDC"):
        return 1.0
    # Essai USDC d'abord, fallback USDT pour les données de marché
    for quote in ("USDC", "USDT"):
        try:
            data = _get("/api/v5/market/ticker", {"instId": f"{ticker.upper()}-{quote}"})
            if data:
                return float(data[0]["last"])
        except Exception:
            continue
    logger.warning(f"Prix OKX {ticker} : indisponible")
    return None


def get_all_prices_usdt(tickers: list[str]) -> dict[str, float]:
    """Prix en USDC pour une liste de tickers."""
    prices = {}
    for ticker in tickers:
        price = get_price_usdc(ticker)
        if price:
            prices[ticker] = price
        time.sleep(0.05)
    return prices


def get_ask_price(ticker: str) -> float | None:
    """Retourne le meilleur ask (prix vendeur) depuis le carnet d'ordres."""
    for quote in ("USDC", "USDT"):
        try:
            data = _get("/api/v5/market/books", {
                "instId": f"{ticker.upper()}-{quote}",
                "sz": "1",
            })
            if data and data[0].get("asks"):
                return float(data[0]["asks"][0][0])
        except Exception:
            continue
    # Fallback au prix last si carnet indisponible
    return get_price_usdc(ticker)


def get_bid_price(ticker: str) -> float | None:
    """Retourne le meilleur bid (prix acheteur) depuis le carnet d'ordres."""
    for quote in ("USDC", "USDT"):
        try:
            data = _get("/api/v5/market/books", {
                "instId": f"{ticker.upper()}-{quote}",
                "sz": "1",
            })
            if data and data[0].get("bids"):
                return float(data[0]["bids"][0][0])
        except Exception:
            continue
    return get_price_usdc(ticker)


# ─── OHLCV ────────────────────────────────────────────────────────────────────

def get_ohlcv(ticker: str, days: int = 90) -> pd.DataFrame:
    """Bougies journalières OKX — essaie USDC puis USDT en fallback."""
    for quote in ("USDC", "USDT"):
        inst_id = f"{ticker.upper()}-{quote}"
        try:
            data = _get("/api/v5/market/candles", {
                "instId": inst_id,
                "bar": "1D",
                "limit": min(days, 100),
            })
            if not data:
                continue

            rows = []
            for candle in data:
                rows.append({
                    "timestamp": pd.to_datetime(int(candle[0]), unit="ms"),
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                })

            df = pd.DataFrame(rows)
            df.set_index("timestamp", inplace=True)
            df.sort_index(inplace=True)
            return df

        except Exception as e:
            logger.debug(f"OHLCV {ticker}-{quote} : {e}")
            continue

    logger.warning(f"OHLCV OKX {ticker} : aucune paire disponible")
    return pd.DataFrame()


def get_all_ohlcv(tickers: list[str], days: int = 90) -> dict[str, pd.DataFrame]:
    """OHLCV pour tous les actifs cibles."""
    import config
    result = {}
    for ticker in tickers:
        if ticker.lower() in config.STABLECOINS:
            continue
        df = get_ohlcv(ticker, days)
        if not df.empty:
            result[ticker] = df
            logger.info(f"OHLCV OKX {ticker} : {len(df)} bougies")
        time.sleep(0.2)
    return result


# ─── Ordres ───────────────────────────────────────────────────────────────────

def get_available_pairs() -> list[str]:
    """Liste tous les tickers avec paire USDT disponibles sur OKX Spot."""
    try:
        data = _get("/api/v5/public/instruments", {"instType": "SPOT"})
        return [
            d["baseCcy"] for d in data
            if d.get("quoteCcy") == "USDT" and d.get("state") == "live"
        ]
    except Exception as e:
        logger.error(f"Erreur get_available_pairs : {e}")
        return []


def place_order(
    ticker: str,
    side: str,
    usdt_amount: float = None,
    quantity: float = None,
    order_type: str = "market",
    stop_loss: float = None,
    take_profit: float = None,
) -> dict:
    """
    Passe un ordre Spot sur OKX avec stratégie limit intelligente.

    Stratégie d'exécution :
    - ACHAT : limite à ask + 0.3% → évite l'annulation slippage 5% OKX sur paires peu liquides
    - VENTE : limite à bid - 0.2% → garantit le remplissage rapide en sortie
    - Si le carnet est indisponible : fallback market

    Puis place les ordres algo TP/SL séparément (OKX spot ne supporte pas l'inline).
    """
    inst_id = f"{ticker.upper()}-{QUOTE_CCY}"  # USDC sur compte EEA

    # ── Étape 1 : calcul du prix limite et de la quantité ─────────────────────
    fill_price = None  # Prix utilisé pour les calculs (TP/SL algo + rapport)
    use_limit = True

    if side == "buy":
        ask = get_ask_price(ticker)
        if ask:
            fill_price = round(ask * 1.003, 8)  # +0.3% au-dessus de l'ask
        else:
            use_limit = False

    elif side == "sell":
        bid = get_bid_price(ticker)
        if bid:
            fill_price = round(bid * 0.998, 8)  # -0.2% en dessous du bid
        else:
            use_limit = False

    # Convertir montant USDC → quantité si nécessaire
    qty_computed = None
    if usdt_amount and fill_price:
        qty_computed = round(usdt_amount / fill_price, 8)
    elif quantity:
        qty_computed = round(quantity, 8)

    # ── Étape 2 : construction du corps de l'ordre ────────────────────────────
    body = {
        "instId": inst_id,
        "tdMode": "cash",
        "side": side,
    }

    if use_limit and fill_price and qty_computed:
        body["ordType"] = "limit"
        body["px"] = str(fill_price)
        body["sz"] = str(qty_computed)
        logger.info(
            f"Ordre LIMIT {side} {ticker} : {qty_computed} @ ${fill_price} "
            f"({'ask+0.3%' if side == 'buy' else 'bid-0.2%'})"
        )
    else:
        # Fallback market
        body["ordType"] = "market"
        if side == "buy" and usdt_amount:
            body["tgtCcy"] = "quote_ccy"
            body["sz"] = str(round(usdt_amount, 2))
        elif qty_computed:
            body["sz"] = str(qty_computed)
        logger.info(f"Ordre MARKET {side} {ticker} (carnet indisponible)")

    result = _post("/api/v5/trade/order", body)
    logger.info(f"Ordre OKX placé : {side} {ticker} — {result}")
    order_result = result[0] if result else {}

    # Enrichir le résultat avec le prix d'entrée estimé
    if fill_price:
        order_result["fill_price_estimate"] = fill_price
    if qty_computed:
        order_result["qty_estimate"] = qty_computed

    # ── Étape 3 : algo TP/SL (ordre conditionnel post-entrée) ─────────────────
    if (stop_loss or take_profit) and order_result.get("ordId"):
        try:
            # Quantité pour l'algo : on connaît qty_computed depuis l'étape 1
            qty_algo = qty_computed
            if not qty_algo and usdt_amount:
                # Dernier recours : estimation par prix actuel
                price_now = get_price_usdc(ticker)
                qty_algo = round(usdt_amount / price_now, 8) if price_now else None

            if qty_algo:
                algo_body = {
                    "instId": inst_id,
                    "tdMode": "cash",
                    "side": "sell" if side == "buy" else "buy",
                    "ordType": "oco",
                    "sz": str(qty_algo),
                }

                if take_profit:
                    algo_body["tpTriggerPx"] = str(round(take_profit, 6))
                    algo_body["tpOrdPx"] = "-1"
                    algo_body["tpTriggerPxType"] = "last"

                if stop_loss:
                    algo_body["slTriggerPx"] = str(round(stop_loss, 6))
                    algo_body["slOrdPx"] = "-1"
                    algo_body["slTriggerPxType"] = "last"

                algo_result = _post("/api/v5/trade/order-algo", algo_body)
                logger.info(f"Algo TP/SL OKX : {ticker} — {algo_result}")
                order_result["algoId"] = algo_result[0].get("algoId", "") if algo_result else ""

        except Exception as e:
            logger.warning(f"Algo TP/SL {ticker} non placé : {e} (l'ordre principal est exécuté)")

    return order_result


def cancel_order(ticker: str, order_id: str) -> dict:
    """Annule un ordre ouvert."""
    return _post("/api/v5/trade/cancel-order", {
        "instId": f"{ticker.upper()}-USDT",
        "ordId": order_id,
    })


def get_open_orders(ticker: str = None) -> list:
    """Retourne les ordres ouverts."""
    params = {"instType": "SPOT"}
    if ticker:
        params["instId"] = f"{ticker.upper()}-USDT"
    return _get("/api/v5/trade/orders-pending", params)
