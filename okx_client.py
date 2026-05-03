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
    results = data.get("data", [])
    # Vérifier le sCode au niveau de l'ordre individuel
    if results and isinstance(results, list):
        first = results[0]
        s_code = str(first.get("sCode", "0"))
        if s_code != "0":
            s_msg = first.get("sMsg", "Unknown order error")
            raise Exception(f"OKX order failed : {s_msg} (sCode {s_code})")
    return results


# ─── Balances ─────────────────────────────────────────────────────────────────

def get_balances() -> dict[str, float]:
    """
    Retourne les balances spot {ticker: quantite}.
    Utilise cashBal (solde total) plutôt que availBal (disponible seulement).
    Raison : availBal = 0 quand des fonds sont gelés dans un ordre algo OCO
    (stop-loss/take-profit OKX) → actifs invisibles au bot malgré leur existence.
    cashBal = solde réel incluant fonds gelés en ordres.
    """
    data = _get("/api/v5/account/balance")
    balances = {}
    for account in data:
        for detail in account.get("details", []):
            qty = float(detail.get("cashBal", 0))
            if qty > 0:
                balances[detail["ccy"]] = qty
    logger.info(f"Balances OKX : {balances}")
    return balances


def get_avg_entry_price(ticker: str) -> float | None:
    """
    Récupère le prix moyen d'entrée OKX (accAvgPx) depuis le compte.
    Utilisé comme fallback quand les fills historiques ne remontent pas assez loin.
    OKX calcule ce prix moyen sur l'ensemble de la position actuelle.
    """
    try:
        data = _get("/api/v5/account/balance")
        for account in data:
            for detail in account.get("details", []):
                if detail.get("ccy", "").upper() == ticker.upper():
                    px = detail.get("accAvgPx", "")
                    if px and float(px) > 0:
                        return float(px)
    except Exception as e:
        logger.debug(f"accAvgPx {ticker} : {e}")
    return None


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

def get_available_pairs(min_volume_usdc: float = 500_000) -> list[str]:
    """
    Retourne TOUS les tickers tradables sur OKX EEA en USDC,
    triés par volume 24h décroissant, avec un filtre de liquidité minimum.

    min_volume_usdc : volume 24h minimum en USDC (défaut 500k$)
                      Élimine les tokens trop illiquides pour être tradés.
    """
    try:
        # Récupérer tous les instruments SPOT disponibles
        instruments = _get("/api/v5/public/instruments", {"instType": "SPOT"})

        # Filtrer : paires USDC (ce qu'on trade réellement sur EEA) + actives
        usdc_pairs = {
            d["baseCcy"] for d in instruments
            if d.get("quoteCcy") == "USDC" and d.get("state") == "live"
        }

        if not usdc_pairs:
            # Fallback USDT si aucune paire USDC trouvée
            usdc_pairs = {
                d["baseCcy"] for d in instruments
                if d.get("quoteCcy") == "USDT" and d.get("state") == "live"
            }

        # Récupérer les volumes 24h pour trier et filtrer
        tickers_data = _get("/api/v5/market/tickers", {"instType": "SPOT"})

        volumes = {}
        for t in tickers_data:
            inst_id = t.get("instId", "")
            # Accepter USDC ou USDT pour le volume
            for quote in ("-USDC", "-USDT"):
                if inst_id.endswith(quote):
                    base = inst_id[: -len(quote)]
                    if base in usdc_pairs:
                        vol = float(t.get("volCcy24h", 0) or 0)
                        if base not in volumes or vol > volumes[base]:
                            volumes[base] = vol
                    break

        # Filtrer par volume minimum et trier par volume décroissant
        filtered = [
            ticker for ticker, vol in volumes.items()
            if vol >= min_volume_usdc
        ]
        filtered.sort(key=lambda t: volumes.get(t, 0), reverse=True)

        logger.info(
            f"OKX EEA : {len(usdc_pairs)} paires USDC disponibles, "
            f"{len(filtered)} avec volume > ${min_volume_usdc/1e6:.1f}M"
        )
        return filtered

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

    # ── Vérifier qu'il n'y a pas déjà un ordre ouvert pour ce ticker ─────────
    # Évite le "All operations failed" quand on essaie de vendre une position
    # déjà en cours de liquidation (ordre limit non rempli).
    try:
        pending = _get("/api/v5/trade/orders-pending", {
            "instId": inst_id,
            "instType": "SPOT",
        })
        if pending:
            same_side = [o for o in pending if o.get("side") == side]
            if same_side:
                existing_id = same_side[0].get("ordId", "?")
                logger.info(
                    f"{ticker} : ordre {side} déjà en attente (ID {existing_id}) — ignoré"
                )
                return {"ordId": existing_id, "status": "already_pending"}
    except Exception as e:
        logger.debug(f"Check ordres en attente {ticker} : {e}")

    # ── Étape 1 : calcul du prix et de la quantité ────────────────────────────
    fill_price = None
    use_limit = True

    if side == "buy":
        # Achat : ordre limit à ask + 0.3% pour garantir le remplissage
        ask = get_ask_price(ticker)
        if ask:
            fill_price = round(ask * 1.003, 8)
        else:
            use_limit = False
    else:
        # Vente : TOUJOURS market pour exécution immédiate et certaine.
        # Les ordres limit de vente peuvent rester en attente si le marché
        # baisse, ce qui cause des tentatives de double-vente aux cycles suivants.
        use_limit = False
        fill_price = get_price_usdc(ticker)  # Pour estimation seulement

    # Convertir montant USDC → quantité si nécessaire
    qty_computed = None
    if usdt_amount and fill_price:
        qty_computed = round(usdt_amount / fill_price, 8)
    elif quantity:
        qty_computed = round(quantity, 8)

    # ── Vérification montant minimum (~$1 pour OKX EEA) ──────────────────────
    if qty_computed and fill_price:
        notional = qty_computed * fill_price
        if notional < 1.0:
            raise Exception(
                f"Ordre trop petit : ${notional:.2f} (minimum $1) — ignoré"
            )

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
        logger.info(f"Ordre LIMIT buy {ticker} : {qty_computed} @ ${fill_price} (ask+0.3%)")
    else:
        # Market : ventes + fallback achats sans carnet
        body["ordType"] = "market"
        if side == "buy" and usdt_amount:
            body["tgtCcy"] = "quote_ccy"
            body["sz"] = str(round(usdt_amount, 2))
        elif qty_computed:
            body["sz"] = str(qty_computed)
        logger.info(f"Ordre MARKET {side} {ticker} : {qty_computed}")

    result = _post("/api/v5/trade/order", body)
    logger.info(f"Ordre OKX placé : {side} {ticker} — {result}")
    order_result = result[0] if result else {}

    # Enrichir le résultat avec le prix d'entrée estimé
    if fill_price:
        order_result["fill_price_estimate"] = fill_price
    if qty_computed:
        order_result["qty_estimate"] = qty_computed

    # ── Étape 3 : algo TP/SL — SUPPRIMÉ ──────────────────────────────────────
    # Les ordres OCO OKX ne sont plus utilisés. Raisons :
    # 1. Ils gèlent les fonds (frozenBal) → availBal = 0 → actifs invisibles au bot
    # 2. Ils créent un double mécanisme de sortie (OCO + position_manager)
    #    qui cause "All operations failed" quand les deux essaient de vendre
    # 3. Le stop OCO (-4%) était incohérent avec le stop position_manager (-7%)
    # 4. OCO placé avec succès ~1/5 du temps → comportement aléatoire
    #
    # position_manager.py gère TOUS les exits : -7% stop / +12% TP / 7j time stop.
    # C'est la seule source de vérité pour les sorties.

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
