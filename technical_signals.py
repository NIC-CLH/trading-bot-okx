"""
Module Signaux Techniques — arsenal complet pro.

Indicateurs intégrés :
  Momentum  : RSI(14), Stochastic RSI, Williams %R, CCI
  Tendance  : MACD, EMA alignment (8/21/55/200), ADX, Supertrend, Ichimoku Cloud
  Volatilité: Bollinger Bands (+ squeeze), ATR, Parabolic SAR
  Volume    : VWAP, OBV, Volume Momentum
  Niveaux   : Pivots fractaux S/R, Fibonacci auto-retracement

Score composite : -3 à +3. Seuil trade : >= 2.0
"""

import time
import logging

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


# ─── Indicateurs de base ──────────────────────────────────────────────────────

def compute_rsi(series: pd.Series, period: int = config.RSI_PERIOD) -> pd.Series:
    """RSI classique Wilder."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_stochastic_rsi(series: pd.Series, rsi_period: int = 14, stoch_period: int = 14) -> dict:
    """
    Stochastic RSI : oscillateur de la position du RSI dans son propre range.
    Valeurs 0-100. < 20 = survendu, > 80 = suracheté.
    Plus sensible que le RSI seul, capte les retournements en amont.
    """
    rsi = compute_rsi(series, rsi_period)
    rsi_min = rsi.rolling(stoch_period).min()
    rsi_max = rsi.rolling(stoch_period).max()
    stoch_rsi = 100 * (rsi - rsi_min) / (rsi_max - rsi_min + 1e-10)
    k = stoch_rsi.rolling(3).mean()
    d = k.rolling(3).mean()
    return {"k": k, "d": d, "raw": stoch_rsi}


def compute_macd(
    series: pd.Series,
    fast: int = config.MACD_FAST,
    slow: int = config.MACD_SLOW,
    signal_period: int = config.MACD_SIGNAL,
) -> dict[str, pd.Series]:
    """MACD ligne, signal et histogramme."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal_period, adjust=False).mean()
    histogram = macd_line - signal_line
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


def compute_ema_alignment(series: pd.Series) -> dict:
    """
    Alignement des EMA 8/21/55/200.
    Bullish alignment : EMA8 > EMA21 > EMA55 > EMA200 et prix > EMA200.
    C'est l'outil favori des traders swing pros.
    """
    ema8 = series.ewm(span=8, adjust=False).mean()
    ema21 = series.ewm(span=21, adjust=False).mean()
    ema55 = series.ewm(span=55, adjust=False).mean()
    ema200 = series.ewm(span=200, adjust=False).mean()

    p = float(series.iloc[-1])
    e8 = float(ema8.iloc[-1])
    e21 = float(ema21.iloc[-1])
    e55 = float(ema55.iloc[-1])
    e200 = float(ema200.iloc[-1])

    full_bull = p > e8 > e21 > e55 > e200
    partial_bull = p > e21 > e55
    full_bear = p < e8 < e21 < e55 < e200
    partial_bear = p < e21 < e55

    # Croisement récent EMA8/EMA21 (golden/death cross court terme)
    prev_e8 = float(ema8.iloc[-2]) if len(ema8) >= 2 else e8
    prev_e21 = float(ema21.iloc[-2]) if len(ema21) >= 2 else e21
    golden_cross = prev_e8 <= prev_e21 and e8 > e21
    death_cross = prev_e8 >= prev_e21 and e8 < e21

    return {
        "ema8": round(e8, 6), "ema21": round(e21, 6),
        "ema55": round(e55, 6), "ema200": round(e200, 6),
        "full_bull": full_bull, "partial_bull": partial_bull,
        "full_bear": full_bear, "partial_bear": partial_bear,
        "golden_cross": golden_cross, "death_cross": death_cross,
        "above_ema200": p > e200,
    }


def compute_adx(df: pd.DataFrame, period: int = 14) -> dict:
    """
    ADX (Average Directional Index) — mesure la FORCE d'une tendance.
    ADX > 25 = tendance forte (trade avec trend)
    ADX < 20 = range (éviter ou trade contre-tendance)
    +DI > -DI = tendance haussière, -DI > +DI = baissière.
    Outil clé des pros pour filtrer les faux signaux en range.
    """
    high = df.get("high", df["close"])
    low = df.get("low", df["close"])
    close = df["close"]

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    plus_dm = (high - high.shift(1)).clip(lower=0)
    minus_dm = (low.shift(1) - low).clip(lower=0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0)

    atr_s = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_s.replace(0, np.nan)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.ewm(span=period, adjust=False).mean()

    adx_val = float(adx.iloc[-1]) if not adx.empty else 0
    plus_di_val = float(plus_di.iloc[-1]) if not plus_di.empty else 0
    minus_di_val = float(minus_di.iloc[-1]) if not minus_di.empty else 0

    trending = adx_val > 25
    bullish_trend = trending and plus_di_val > minus_di_val
    bearish_trend = trending and minus_di_val > plus_di_val

    return {
        "adx": round(adx_val, 1),
        "plus_di": round(plus_di_val, 1),
        "minus_di": round(minus_di_val, 1),
        "trending": trending,
        "bullish_trend": bullish_trend,
        "bearish_trend": bearish_trend,
    }


def compute_bollinger(
    series: pd.Series,
    period: int = config.BB_PERIOD,
    num_std: float = config.BB_STD,
) -> dict[str, pd.Series]:
    """Bandes de Bollinger avec squeeze detector."""
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    bandwidth = (2 * num_std * std) / sma
    pct_b = (series - lower) / (upper - lower + 1e-10)

    # Bollinger Squeeze : bandwidth au plus bas des 20 dernières bougies
    bw_min = bandwidth.rolling(20).min()
    squeeze = bool(bandwidth.iloc[-1] <= bw_min.iloc[-1] * 1.02) if len(bandwidth) >= 20 else False

    return {
        "upper": upper, "middle": sma, "lower": lower,
        "bandwidth": bandwidth, "pct_b": pct_b, "squeeze": squeeze,
    }


def compute_atr(df: pd.DataFrame, period: int = config.ATR_PERIOD) -> pd.Series:
    """Average True Range."""
    high = df.get("high", df["close"])
    low = df.get("low", df["close"])
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def compute_vwap(df: pd.DataFrame) -> pd.Series:
    """VWAP cumulatif sur la période."""
    if "volume" not in df.columns:
        return pd.Series(dtype=float)
    typical_price = (df["high"] + df["low"] + df["close"]) / 3 if "high" in df.columns else df["close"]
    cumulative_pv = (typical_price * df["volume"]).cumsum()
    cumulative_vol = df["volume"].cumsum()
    return cumulative_pv / cumulative_vol.replace(0, np.nan)


def compute_obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume."""
    if "volume" not in df.columns:
        return pd.Series(dtype=float)
    direction = np.sign(df["close"].diff().fillna(0))
    return (direction * df["volume"]).cumsum()


def compute_volume_momentum(df: pd.DataFrame, period: int = 10) -> dict:
    """
    Momentum du volume : compare volume actuel à la moyenne récente.
    Volume > 1.5x moyenne = confirmation de mouvement (breakout réel vs faux).
    Outil critique pour éviter les faux breakouts.
    """
    if "volume" not in df.columns:
        return {"ratio": 1.0, "surge": False, "drying_up": False}
    vol_ma = df["volume"].rolling(period).mean()
    ratio = float(df["volume"].iloc[-1] / vol_ma.iloc[-1]) if vol_ma.iloc[-1] > 0 else 1.0
    return {
        "ratio": round(ratio, 2),
        "surge": ratio > 1.5,    # Volume x1.5 = confirmation forte
        "drying_up": ratio < 0.5  # Volume très bas = manque de conviction
    }


# ─── Ichimoku Cloud ──────────────────────────────────────────────────────────

def compute_ichimoku(df: pd.DataFrame) -> dict:
    """
    Ichimoku Kinko Hyo — système complet japonais utilisé par les pros asiatiques.

    Composants :
    - Tenkan-sen (9) : momentum court terme
    - Kijun-sen (26) : momentum moyen terme + support/résistance dynamique
    - Senkou A (26 ahead) + Senkou B (52) : le "nuage" — zone de support/résistance
    - Chikou Span : closing price décalé 26 en arrière — confirmation

    Signaux clés :
    - Prix au-dessus du nuage + nuage haussier = tendance haussière confirmée
    - TK Cross (Tenkan > Kijun) = signal d'achat
    - Prix dans le nuage = zone d'indécision
    """
    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]

    tenkan = (high.rolling(9).max() + low.rolling(9).min()) / 2
    kijun = (high.rolling(26).max() + low.rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((high.rolling(52).max() + low.rolling(52).min()) / 2).shift(26)
    chikou = close.shift(-26)

    if len(close) < 52:
        return {"score": 0.0, "signal": "données insuffisantes Ichimoku"}

    price = float(close.iloc[-1])
    tk = float(tenkan.iloc[-1]) if not pd.isna(tenkan.iloc[-1]) else price
    kj = float(kijun.iloc[-1]) if not pd.isna(kijun.iloc[-1]) else price
    sa = float(senkou_a.iloc[-1]) if not pd.isna(senkou_a.iloc[-1]) else price
    sb = float(senkou_b.iloc[-1]) if not pd.isna(senkou_b.iloc[-1]) else price

    cloud_top = max(sa, sb)
    cloud_bottom = min(sa, sb)
    above_cloud = price > cloud_top
    below_cloud = price < cloud_bottom
    in_cloud = cloud_bottom <= price <= cloud_top
    bullish_cloud = sa > sb  # Nuage haussier (vert)

    # TK Cross
    prev_tk = float(tenkan.iloc[-2]) if len(tenkan) >= 2 and not pd.isna(tenkan.iloc[-2]) else tk
    prev_kj = float(kijun.iloc[-2]) if len(kijun) >= 2 and not pd.isna(kijun.iloc[-2]) else kj
    tk_cross_up = prev_tk <= prev_kj and tk > kj
    tk_cross_down = prev_tk >= prev_kj and tk < kj

    score = 0.0
    signals = []

    if above_cloud and bullish_cloud:
        score += 1.0
        signals.append("Ichimoku : prix au-dessus nuage haussier ☁️✅")
    elif above_cloud and not bullish_cloud:
        score += 0.4
        signals.append("Ichimoku : prix au-dessus nuage baissier")
    elif below_cloud and not bullish_cloud:
        score -= 1.0
        signals.append("Ichimoku : prix sous nuage baissier ☁️⚠️")
    elif below_cloud and bullish_cloud:
        score -= 0.4
        signals.append("Ichimoku : prix sous nuage haussier")
    else:
        signals.append("Ichimoku : prix dans le nuage — indécision")

    if tk > kj:
        score += 0.3
    else:
        score -= 0.3

    if tk_cross_up:
        score += 0.5
        signals.append("Ichimoku TK Cross haussier 🌟")
    elif tk_cross_down:
        score -= 0.5
        signals.append("Ichimoku TK Cross baissier")

    return {
        "score": round(max(-2.0, min(2.0, score)), 2),
        "signals": signals,
        "above_cloud": above_cloud,
        "below_cloud": below_cloud,
        "in_cloud": in_cloud,
        "bullish_cloud": bullish_cloud,
        "tenkan": round(tk, 4),
        "kijun": round(kj, 4),
        "cloud_top": round(cloud_top, 4),
        "cloud_bottom": round(cloud_bottom, 4),
    }


# ─── Fibonacci Auto-Retracement ───────────────────────────────────────────────

def compute_fibonacci(df: pd.DataFrame, lookback: int = 50) -> dict:
    """
    Fibonacci auto-détecté sur le dernier swing haut/bas.

    Les algorithmes institutionnels placent des ordres limite sur ces niveaux.
    Niveaux clés : 23.6%, 38.2%, 50%, 61.8%, 78.6%
    - 38.2% et 61.8% : niveaux de retrace les plus respectés
    - 61.8% (Golden Ratio) : zone d'achat classique en tendance haussière

    Si le prix est exactement sur un niveau Fibonacci = probabilité de rebond élevée.
    """
    recent = df.tail(lookback)
    high = float(recent["high"].max() if "high" in recent.columns else recent["close"].max())
    low = float(recent["low"].min() if "low" in recent.columns else recent["close"].min())
    current = float(df["close"].iloc[-1])

    if high == low:
        return {"score": 0.0, "levels": {}, "nearest": None}

    diff = high - low
    levels = {
        "0.0%": round(high, 4),
        "23.6%": round(high - diff * 0.236, 4),
        "38.2%": round(high - diff * 0.382, 4),
        "50.0%": round(high - diff * 0.500, 4),
        "61.8%": round(high - diff * 0.618, 4),
        "78.6%": round(high - diff * 0.786, 4),
        "100%": round(low, 4),
    }

    # Trouver le niveau le plus proche (en %)
    nearest_name = min(levels, key=lambda k: abs(levels[k] - current))
    nearest_price = levels[nearest_name]
    distance_pct = abs(current - nearest_price) / current * 100

    score = 0.0
    signal = f"Prix proche Fibonacci {nearest_name} (${nearest_price:.4f}, dist {distance_pct:.1f}%)"

    # Si le prix est sur un niveau Fibonacci fort et en mouvement haussier
    if distance_pct < 2.0:
        if nearest_name in ("38.2%", "61.8%"):
            # Niveaux golden ratio - rebond probable
            if current > levels["50.0%"]:
                score = 0.4  # Retrace 38% et remonte = haussier
                signal = f"Prix sur Fib {nearest_name} (golden) — rebond probable 🎯"
            else:
                score = 0.2
                signal = f"Test Fib {nearest_name} — niveau clé"
        elif nearest_name == "50.0%":
            score = 0.2
            signal = f"Prix sur Fib 50% — niveau pivot"

    return {
        "score": round(score, 2),
        "levels": levels,
        "nearest": nearest_name,
        "nearest_price": nearest_price,
        "distance_pct": round(distance_pct, 2),
        "signal": signal,
        "swing_high": round(high, 4),
        "swing_low": round(low, 4),
    }


# ─── Supertrend ───────────────────────────────────────────────────────────────

def compute_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> dict:
    """
    Supertrend — indicateur de suivi de tendance basé sur ATR.
    Très populaire chez les traders swing pour les points d'entrée précis.

    Logique : ligne de support/résistance dynamique qui inverse quand le prix croise.
    - Prix > Supertrend ligne = tendance haussière (BUY)
    - Prix < Supertrend ligne = tendance baissière (SELL)
    - Croisement = signal d'entrée fort
    """
    if len(df) < period + 1:
        return {"score": 0.0, "bullish": None, "signal": "données insuffisantes"}

    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]

    # ATR
    atr = compute_atr(df, period)

    # Bandes upper/lower
    hl2 = (high + low) / 2
    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    # Calcul Supertrend
    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=float)  # 1 = haussier, -1 = baissier

    supertrend.iloc[0] = upper_band.iloc[0]
    direction.iloc[0] = -1

    for i in range(1, len(df)):
        if close.iloc[i] > supertrend.iloc[i - 1]:
            supertrend.iloc[i] = lower_band.iloc[i]
            direction.iloc[i] = 1
        else:
            supertrend.iloc[i] = upper_band.iloc[i]
            direction.iloc[i] = -1

        if direction.iloc[i] == 1 and supertrend.iloc[i] < supertrend.iloc[i - 1]:
            supertrend.iloc[i] = supertrend.iloc[i - 1]
        elif direction.iloc[i] == -1 and supertrend.iloc[i] > supertrend.iloc[i - 1]:
            supertrend.iloc[i] = supertrend.iloc[i - 1]

    current_dir = int(direction.iloc[-1])
    prev_dir = int(direction.iloc[-2]) if len(direction) >= 2 else current_dir
    bullish = current_dir == 1
    just_flipped = current_dir != prev_dir
    st_val = float(supertrend.iloc[-1])
    price = float(close.iloc[-1])
    distance_pct = abs(price - st_val) / price * 100

    score = 0.8 if bullish else -0.8
    signal = ""

    if just_flipped and bullish:
        score = 1.2
        signal = "🔄 Supertrend retournement HAUSSIER — signal d'achat fort"
    elif just_flipped and not bullish:
        score = -1.2
        signal = "🔄 Supertrend retournement BAISSIER — signal de vente"
    elif bullish:
        signal = f"Supertrend haussier (support ${st_val:.4f}, dist {distance_pct:.1f}%)"
    else:
        signal = f"Supertrend baissier (résistance ${st_val:.4f})"

    return {
        "score": round(score, 2),
        "bullish": bullish,
        "just_flipped": just_flipped,
        "value": round(st_val, 4),
        "distance_pct": round(distance_pct, 2),
        "signal": signal,
    }


# ─── Williams %R ─────────────────────────────────────────────────────────────

def compute_williams_r(df: pd.DataFrame, period: int = 14) -> dict:
    """
    Williams %R — oscillateur momentum [-100, 0].
    Moins de bruit que le RSI, signale les retournements plus tôt.

    > -20 : suracheté (vendre)
    < -80 : survendu (acheter)
    -50 : neutre
    """
    if len(df) < period:
        return {"value": -50, "score": 0.0}

    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]

    highest_high = high.rolling(period).max()
    lowest_low = low.rolling(period).min()
    wr = -100 * (highest_high - close) / (highest_high - lowest_low + 1e-10)

    wr_val = float(wr.iloc[-1])
    wr_prev = float(wr.iloc[-2]) if len(wr) >= 2 else wr_val

    score = 0.0
    if wr_val < -80:
        score = 0.5
        # Croisement haussier depuis zone survendue = fort signal
        if wr_prev < wr_val:
            score = 0.75
    elif wr_val > -20:
        score = -0.5
        if wr_prev > wr_val:
            score = -0.75

    return {"value": round(wr_val, 1), "score": round(score, 2)}


# ─── CCI (Commodity Channel Index) ───────────────────────────────────────────

def compute_cci(df: pd.DataFrame, period: int = 20) -> dict:
    """
    CCI — identifie les cycles et les conditions extrêmes.
    Très utilisé en crypto pour détecter les retournements de tendance.

    > +100 : surachat (mais tendance forte si > 200)
    < -100 : survente (mais tendance baissière forte si < -200)
    Croisement ±100 = signal d'entrée/sortie
    """
    if len(df) < period:
        return {"value": 0, "score": 0.0}

    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]

    typical = (high + low + close) / 3
    sma_tp = typical.rolling(period).mean()
    mean_dev = typical.rolling(period).apply(lambda x: abs(x - x.mean()).mean(), raw=True)
    cci = (typical - sma_tp) / (0.015 * mean_dev + 1e-10)

    cci_val = float(cci.iloc[-1])
    cci_prev = float(cci.iloc[-2]) if len(cci) >= 2 else cci_val

    score = 0.0
    cross_above_neg100 = cci_prev < -100 and cci_val > -100
    cross_below_pos100 = cci_prev > 100 and cci_val < 100

    if cross_above_neg100:
        score = 0.5  # Sortie zone survente = signal achat
    elif cross_below_pos100:
        score = -0.5  # Sortie zone surachat = signal vente
    elif cci_val < -150:
        score = 0.3  # Extrême survente
    elif cci_val > 150:
        score = -0.3  # Extrême surachat

    return {"value": round(cci_val, 1), "score": round(score, 2)}


# ─── Parabolic SAR ────────────────────────────────────────────────────────────

def compute_parabolic_sar(df: pd.DataFrame, af_start: float = 0.02, af_max: float = 0.2) -> dict:
    """
    Parabolic SAR — stop & reverse. Suit le prix comme un trailing stop.
    Utilisé pour les sorties de position et la direction de tendance.

    SAR sous le prix = tendance haussière (signal LONG)
    SAR au-dessus = tendance baissière (signal SHORT)
    Renversement = signal fort
    """
    if len(df) < 3:
        return {"bullish": None, "score": 0.0}

    high = (df["high"] if "high" in df.columns else df["close"]).values
    low = (df["low"] if "low" in df.columns else df["close"]).values
    close = df["close"].values

    af = af_start
    ep = high[0]  # extreme point
    sar = low[0]
    bullish = True

    sar_list = [sar]
    direction_list = [bullish]

    for i in range(1, len(close)):
        prev_sar = sar

        if bullish:
            sar = prev_sar + af * (ep - prev_sar)
            sar = min(sar, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])

            if low[i] < sar:
                bullish = False
                sar = ep
                ep = low[i]
                af = af_start
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_start, af_max)
        else:
            sar = prev_sar + af * (ep - prev_sar)
            sar = max(sar, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])

            if high[i] > sar:
                bullish = True
                sar = ep
                ep = high[i]
                af = af_start
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_start, af_max)

        sar_list.append(sar)
        direction_list.append(bullish)

    current_bullish = direction_list[-1]
    prev_bullish = direction_list[-2] if len(direction_list) >= 2 else current_bullish
    just_reversed = current_bullish != prev_bullish
    sar_val = sar_list[-1]
    price = float(close[-1])
    distance_pct = abs(price - sar_val) / price * 100

    score = 0.4 if current_bullish else -0.4
    if just_reversed:
        score = 0.6 if current_bullish else -0.6

    signal = ""
    if just_reversed and current_bullish:
        signal = "Parabolic SAR retournement haussier ↗️"
    elif just_reversed and not current_bullish:
        signal = "Parabolic SAR retournement baissier ↘️"
    elif current_bullish:
        signal = f"SAR haussier (${sar_val:.4f} — {distance_pct:.1f}% sous le prix)"
    else:
        signal = f"SAR baissier (${sar_val:.4f} — {distance_pct:.1f}% au-dessus)"

    return {
        "bullish": current_bullish,
        "just_reversed": just_reversed,
        "sar": round(sar_val, 4),
        "distance_pct": round(distance_pct, 2),
        "score": round(score, 2),
        "signal": signal,
    }


# ─── CVD — Cumulative Volume Delta ───────────────────────────────────────────

def compute_cvd(df: pd.DataFrame, window: int = 20) -> dict:
    """
    Cumulative Volume Delta — différence entre volume acheteur et vendeur.

    Logique : sur chaque bougie, on estime la pression acheteuse/vendeuse
    selon la position du close dans le range high-low.
    CVD haussier qui diverge avec le prix → accumulation institutionnelle.
    CVD baissier qui diverge avec le prix → distribution discrète.
    """
    if "volume" not in df.columns or df.empty or len(df) < 5:
        return {"score": 0.0, "trend": "neutre", "divergence": False, "signals": []}

    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]
    volume = df["volume"]

    range_hl = (high - low).replace(0, np.nan)
    buy_ratio = (close - low) / range_hl
    buy_ratio = buy_ratio.fillna(0.5)

    delta = volume * buy_ratio - volume * (1 - buy_ratio)
    cvd = delta.cumsum()

    cvd_tail = cvd.tail(window)
    price_tail = close.tail(window)

    if len(cvd_tail) < 5:
        return {"score": 0.0, "trend": "neutre", "divergence": False, "signals": []}

    cvd_slope = float(np.polyfit(np.arange(len(cvd_tail)), cvd_tail.values, 1)[0])
    price_slope = float(np.polyfit(np.arange(len(price_tail)), price_tail.values, 1)[0])

    divergence_bullish = price_slope < 0 and cvd_slope > 0  # Accumulation cachée
    divergence_bearish = price_slope > 0 and cvd_slope < 0  # Distribution cachée

    score = 0.0
    signals = []

    if cvd_slope > 0 and price_slope > 0:
        score += 0.5
        signals.append("CVD haussier — pression acheteuse confirmée")
    elif cvd_slope < 0 and price_slope < 0:
        score -= 0.5
        signals.append("CVD baissier — pression vendeuse confirmée")

    if divergence_bullish:
        score += 0.8
        signals.append("🔺 CVD divergence HAUSSIÈRE — accumulation institutionnelle")
    elif divergence_bearish:
        score -= 0.8
        signals.append("🔻 CVD divergence BAISSIÈRE — distribution discrète")

    trend = "haussier" if cvd_slope > 0 else ("baissier" if cvd_slope < 0 else "neutre")

    return {
        "score": round(max(-1.5, min(1.5, score)), 2),
        "trend": trend,
        "divergence": divergence_bullish or divergence_bearish,
        "divergence_type": "bullish" if divergence_bullish else ("bearish" if divergence_bearish else "none"),
        "signals": signals,
    }


# ─── Keltner Channel ──────────────────────────────────────────────────────────

def compute_keltner(df: pd.DataFrame, period: int = 20, multiplier: float = 2.0) -> dict:
    """
    Keltner Channel — EMA(20) ± 2×ATR(10).

    Squeeze Pro : quand les Bollinger Bands sont INSIDE les Keltner
    → compression extrême, explosion de volatilité imminente.
    Prix au-dessus Keltner haut = momentum très fort.
    """
    if df.empty or len(df) < period + 1:
        return {"score": 0.0, "squeeze_pro": False, "signals": []}

    close = df["close"]
    atr = compute_atr(df, period=10)
    ema = close.ewm(span=period, adjust=False).mean()

    kc_upper = ema + multiplier * atr
    kc_lower = ema - multiplier * atr
    bb = compute_bollinger(close, period=period)

    kc_up_val = float(kc_upper.iloc[-1])
    kc_lo_val = float(kc_lower.iloc[-1])
    bb_up_val = float(bb["upper"].iloc[-1]) if not bb["upper"].empty else kc_up_val
    bb_lo_val = float(bb["lower"].iloc[-1]) if not bb["lower"].empty else kc_lo_val
    price = float(close.iloc[-1])

    squeeze_pro = bb_up_val < kc_up_val and bb_lo_val > kc_lo_val

    score = 0.0
    signals = []

    if price > kc_up_val:
        score += 0.6
        signals.append("Prix au-dessus du Keltner haut — momentum fort")
    elif price < kc_lo_val:
        score -= 0.6
        signals.append("Prix sous le Keltner bas — pression vendeuse forte")
    else:
        pct_pos = (price - kc_lo_val) / (kc_up_val - kc_lo_val + 1e-10)
        score += 0.2 if pct_pos > 0.7 else (-0.2 if pct_pos < 0.3 else 0.0)

    if squeeze_pro:
        signals.append("💥 Keltner Squeeze PRO — BB dans Keltner, explosion imminente")

    return {
        "score": round(max(-1.0, min(1.0, score)), 2),
        "upper": round(kc_up_val, 4),
        "lower": round(kc_lo_val, 4),
        "squeeze_pro": squeeze_pro,
        "signals": signals,
    }


# ─── Elder Ray Index ──────────────────────────────────────────────────────────

def compute_elder_ray(df: pd.DataFrame, period: int = 13) -> dict:
    """
    Elder Ray Index — Bull Power et Bear Power.

    Outil institutionnel (Alexander Elder) :
    - Bull Power = High - EMA(13) → force des acheteurs
    - Bear Power = Low - EMA(13) → force des vendeurs

    Achat optimal : EMA en hausse + Bear Power négatif mais remontant.
    Vente optimale : EMA en baisse + Bull Power positif mais redescendant.
    """
    if df.empty or len(df) < period + 5:
        return {"score": 0.0, "bull_power": 0, "bear_power": 0, "signals": []}

    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]

    ema = close.ewm(span=period, adjust=False).mean()
    bull_power = high - ema
    bear_power = low - ema

    bp_val = float(bull_power.iloc[-1])
    br_val = float(bear_power.iloc[-1])
    bp_prev = float(bull_power.iloc[-2]) if len(bull_power) >= 2 else bp_val
    br_prev = float(bear_power.iloc[-2]) if len(bear_power) >= 2 else br_val
    ema_rising = float(ema.iloc[-1]) > float(ema.iloc[-2]) if len(ema) >= 2 else True

    score = 0.0
    signals = []

    if ema_rising and br_val < 0 and br_val > br_prev:
        score += 0.7
        signals.append(f"Elder Ray BUY — EMA↑ + Bear Power remonte ({br_val:+.4f})")
    elif ema_rising and bp_val > 0:
        score += 0.4
        signals.append(f"Elder Ray haussier — Bull Power positif ({bp_val:+.4f})")
    elif not ema_rising and bp_val > 0 and bp_val < bp_prev:
        score -= 0.7
        signals.append(f"Elder Ray SELL — EMA↓ + Bull Power redescend ({bp_val:+.4f})")
    elif not ema_rising and br_val < 0:
        score -= 0.4
        signals.append(f"Elder Ray baissier — Bear Power négatif ({br_val:+.4f})")

    return {
        "score": round(max(-1.0, min(1.0, score)), 2),
        "bull_power": round(bp_val, 6),
        "bear_power": round(br_val, 6),
        "ema_rising": ema_rising,
        "signals": signals,
    }


# ─── Donchian Channel ─────────────────────────────────────────────────────────

def compute_donchian(df: pd.DataFrame, period: int = 20) -> dict:
    """
    Donchian Channel — highest high / lowest low sur N périodes.

    Stratégie Turtle Traders (hedge funds) : achat au breakout 20j.
    - Breakout haussier = prix dépasse le plus haut des 20 dernières bougies
    - Breakdown baissier = prix casse le plus bas des 20 dernières bougies
    Signal fort quand confirmé par volume et ADX > 25.
    """
    if df.empty or len(df) < period:
        return {"score": 0.0, "breakout": None, "signals": []}

    high = df["high"] if "high" in df.columns else df["close"]
    low = df["low"] if "low" in df.columns else df["close"]
    close = df["close"]

    upper = high.rolling(period).max()
    lower = low.rolling(period).min()
    middle = (upper + lower) / 2

    up_val = float(upper.iloc[-1])
    lo_val = float(lower.iloc[-1])
    mid_val = float(middle.iloc[-1])
    price = float(close.iloc[-1])
    prev_price = float(close.iloc[-2]) if len(close) >= 2 else price

    channel_width = up_val - lo_val
    pct_pos = (price - lo_val) / (channel_width + 1e-10)

    breakout_up = prev_price < up_val and price >= up_val
    breakout_down = prev_price > lo_val and price <= lo_val

    score = 0.0
    signals = []

    if breakout_up:
        score += 1.0
        signals.append(f"🚀 Donchian BREAKOUT haussier ({period}j) — signal Turtle fort")
    elif breakout_down:
        score -= 1.0
        signals.append(f"🔻 Donchian BREAKDOWN baissier ({period}j)")
    elif pct_pos > 0.8:
        score += 0.4
        signals.append(f"Prix en zone haute Donchian ({period}j) — momentum haussier")
    elif pct_pos < 0.2:
        score -= 0.4
        signals.append(f"Prix en zone basse Donchian ({period}j) — momentum baissier")

    return {
        "score": round(max(-1.5, min(1.5, score)), 2),
        "upper": round(up_val, 4),
        "lower": round(lo_val, 4),
        "middle": round(mid_val, 4),
        "pct_position": round(pct_pos, 2),
        "breakout": "up" if breakout_up else ("down" if breakout_down else None),
        "signals": signals,
    }


# ─── Support / Résistance (Pivots fractaux) ───────────────────────────────────

def detect_pivot_levels(
    series: pd.Series,
    left: int = 5,
    right: int = 5,
    n_levels: int = 5,
) -> dict[str, list[float]]:
    """Détecte les niveaux S/R via pivots fractaux avec clustering."""
    prices = series.values
    n = len(prices)
    highs, lows = [], []

    for i in range(left, n - right):
        window = prices[i - left: i + right + 1]
        center = prices[i]
        if center == max(window):
            highs.append(center)
        if center == min(window):
            lows.append(center)

    def cluster(levels: list[float], tolerance: float = 0.01) -> list[float]:
        if not levels:
            return []
        levels = sorted(set(levels))
        clusters = [[levels[0]]]
        for v in levels[1:]:
            if abs(v - clusters[-1][-1]) / clusters[-1][-1] < tolerance:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [round(np.mean(c), 4) for c in clusters]

    current_price = float(series.iloc[-1])
    all_resistances = [h for h in cluster(highs) if h > current_price]
    all_supports = [l for l in cluster(lows) if l < current_price]

    return {
        "resistances": sorted(all_resistances)[:n_levels],
        "supports": sorted(all_supports, reverse=True)[:n_levels],
    }


# ─── Score composite professionnel ───────────────────────────────────────────

def compute_signal_score(df: pd.DataFrame) -> dict:
    """
    Score composite -3 à +3 (standard pro).

    Contributions :
    - RSI / Stoch RSI  : jusqu'à ±1.0
    - MACD             : jusqu'à ±0.75
    - EMA alignment    : jusqu'à ±0.75
    - ADX (régime)     : multiplicateur de confiance
    - Bollinger        : jusqu'à ±0.5
    - VWAP             : ±0.25
    - OBV              : ±0.25
    - Volume Momentum  : bonus/malus ±0.25
    """
    if len(df) < 30:
        return {"score": 0, "detail": {}, "signaux": [], "verdict": "DONNÉES INSUFFISANTES"}

    close = df["close"]
    score = 0.0
    detail = {}
    signaux = []

    # ── RSI + Stochastic RSI ──
    rsi = compute_rsi(close)
    rsi_val = float(rsi.iloc[-1]) if not rsi.empty else 50
    detail["rsi"] = round(rsi_val, 1)

    stoch = compute_stochastic_rsi(close)
    stoch_k = float(stoch["k"].iloc[-1]) if not stoch["k"].empty else 50
    stoch_d = float(stoch["d"].iloc[-1]) if not stoch["d"].empty else 50
    detail["stoch_rsi_k"] = round(stoch_k, 1)
    stoch_prev_k = float(stoch["k"].iloc[-2]) if len(stoch["k"]) >= 2 else stoch_k
    stoch_cross_up = stoch_prev_k < stoch_d and stoch_k > stoch_d

    if rsi_val < 30:
        score += 0.75
        signaux.append(f"RSI survendu ({rsi_val:.1f})")
    elif rsi_val > 70:
        score -= 0.75
        signaux.append(f"RSI suracheté ({rsi_val:.1f})")

    if stoch_k < 20:
        score += 0.5
        signaux.append(f"StochRSI survendu (K={stoch_k:.1f})")
        if stoch_cross_up:
            score += 0.25
            signaux.append("StochRSI croisement haussier (retournement)")
    elif stoch_k > 80:
        score -= 0.5
        signaux.append(f"StochRSI suracheté (K={stoch_k:.1f})")

    # ── EMA Alignment ──
    ema_data = compute_ema_alignment(close)
    detail["ema_alignment"] = {
        "full_bull": ema_data["full_bull"],
        "full_bear": ema_data["full_bear"],
        "above_200": ema_data["above_ema200"],
    }

    if ema_data["full_bull"]:
        score += 0.75
        signaux.append("EMA 8>21>55>200 alignées haussière")
    elif ema_data["partial_bull"]:
        score += 0.4
        signaux.append("EMA partiellement haussière (8>21>55)")
    elif ema_data["full_bear"]:
        score -= 0.75
        signaux.append("EMA 8<21<55<200 alignées baissière")
    elif ema_data["partial_bear"]:
        score -= 0.4

    if ema_data["golden_cross"]:
        score += 0.5
        signaux.append("🌟 Golden Cross EMA8/EMA21")
    elif ema_data["death_cross"]:
        score -= 0.5
        signaux.append("💀 Death Cross EMA8/EMA21")

    # ── ADX (filtre régime) ──
    adx_data = compute_adx(df)
    detail["adx"] = adx_data["adx"]
    adx_confidence = 1.0

    if adx_data["trending"]:
        if adx_data["bullish_trend"]:
            score += 0.5
            signaux.append(f"ADX {adx_data['adx']:.0f} — tendance haussière forte")
            adx_confidence = 1.2
        elif adx_data["bearish_trend"]:
            score -= 0.5
            signaux.append(f"ADX {adx_data['adx']:.0f} — tendance baissière forte")
            adx_confidence = 1.2
    else:
        # Marché en range : réduire confiance signaux directionnels
        adx_confidence = 0.7
        detail["regime"] = "range"

    # ── MACD ──
    macd_data = compute_macd(close)
    hist = macd_data["histogram"]
    hist_val = float(hist.iloc[-1]) if not hist.empty else 0
    hist_prev = float(hist.iloc[-2]) if len(hist) >= 2 else 0
    macd_val = float(macd_data["macd"].iloc[-1]) if not macd_data["macd"].empty else 0
    signal_val = float(macd_data["signal"].iloc[-1]) if not macd_data["signal"].empty else 0
    detail["macd_histogram"] = round(hist_val, 6)

    # Croisement MACD/Signal
    prev_macd = float(macd_data["macd"].iloc[-2]) if len(macd_data["macd"]) >= 2 else macd_val
    prev_sig = float(macd_data["signal"].iloc[-2]) if len(macd_data["signal"]) >= 2 else signal_val
    macd_cross_up = prev_macd < prev_sig and macd_val > signal_val
    macd_cross_down = prev_macd > prev_sig and macd_val < signal_val

    if macd_cross_up:
        score += 0.75
        signaux.append("MACD croisement haussier (signal d'achat)")
    elif macd_cross_down:
        score -= 0.75
        signaux.append("MACD croisement baissier (signal de vente)")
    elif hist_val > 0 and hist_val > hist_prev:
        score += 0.4
    elif hist_val < 0 and hist_val < hist_prev:
        score -= 0.4

    # ── Bollinger Bands ──
    bb = compute_bollinger(close)
    pct_b = float(bb["pct_b"].iloc[-1]) if not bb["pct_b"].empty else 0.5
    detail["bb_pct_b"] = round(pct_b, 3)
    detail["bb_squeeze"] = bb["squeeze"]

    if pct_b < 0.05:
        score += 0.5
        signaux.append(f"Prix sous bande BB inférieure — oversold extrême")
    elif pct_b < 0.15:
        score += 0.3
        signaux.append(f"Prix proche bande BB basse (%B={pct_b:.2f})")
    elif pct_b > 0.95:
        score -= 0.5
        signaux.append(f"Prix au-dessus bande BB supérieure — overbought extrême")
    elif pct_b > 0.85:
        score -= 0.3

    if bb["squeeze"]:
        signaux.append("⚡ Bollinger Squeeze — explosion de volatilité imminente")

    # ── VWAP ──
    vwap = compute_vwap(df)
    if not vwap.empty:
        vwap_val = float(vwap.iloc[-1])
        price = float(close.iloc[-1])
        deviation = (price - vwap_val) / vwap_val * 100
        detail["vwap"] = round(vwap_val, 4)
        detail["vwap_deviation_pct"] = round(deviation, 2)
        if price > vwap_val:
            score += 0.25
        else:
            score -= 0.25

    # ── OBV trend ──
    obv = compute_obv(df)
    if not obv.empty and len(obv) >= 14:
        obv_tail = obv.tail(14).values
        x = np.arange(len(obv_tail))
        slope = np.polyfit(x, obv_tail, 1)[0]
        detail["obv_slope"] = round(float(slope), 2)
        if slope > 0:
            score += 0.25
            signaux.append("OBV haussier (accumulation)")
        else:
            score -= 0.25

    # ── Volume Momentum ──
    vol_mom = compute_volume_momentum(df)
    detail["volume_momentum"] = vol_mom
    if vol_mom["surge"] and score > 0:
        score += 0.25
        signaux.append(f"Volume x{vol_mom['ratio']:.1f} — confirmation forte")
    elif vol_mom["surge"] and score < 0:
        score -= 0.25
    elif vol_mom["drying_up"]:
        score *= 0.85

    # ── Ichimoku Cloud ──
    ichi = compute_ichimoku(df)
    detail["ichimoku"] = {
        "above_cloud": ichi.get("above_cloud"),
        "bullish_cloud": ichi.get("bullish_cloud"),
    }
    if ichi["score"] != 0:
        score += ichi["score"] * 0.35  # Poids partiel dans le composite
        signaux.extend(ichi.get("signals", []))

    # ── Supertrend ──
    st = compute_supertrend(df)
    detail["supertrend"] = {"bullish": st.get("bullish"), "value": st.get("value")}
    if st.get("just_flipped"):
        score += st["score"] * 0.4
        signaux.append(st["signal"])
    else:
        score += st["score"] * 0.2
        if st["signal"]:
            signaux.append(st["signal"])

    # ── Williams %R ──
    wr = compute_williams_r(df)
    detail["williams_r"] = wr["value"]
    score += wr["score"] * 0.3

    # ── CCI ──
    cci_data = compute_cci(df)
    detail["cci"] = cci_data["value"]
    score += cci_data["score"] * 0.25

    # ── Parabolic SAR ──
    psar = compute_parabolic_sar(df)
    detail["psar"] = {"bullish": psar.get("bullish"), "sar": psar.get("sar")}
    if psar.get("just_reversed"):
        score += psar["score"] * 0.4
        signaux.append(psar["signal"])
    else:
        score += psar["score"] * 0.15

    # ── Fibonacci ──
    fib = compute_fibonacci(df)
    detail["fibonacci"] = {"nearest": fib.get("nearest"), "distance_pct": fib.get("distance_pct")}
    if fib["score"] != 0:
        score += fib["score"] * 0.3
        signaux.append(fib["signal"])

    # ── CVD (Cumulative Volume Delta) ──
    cvd_data = compute_cvd(df)
    detail["cvd"] = {"trend": cvd_data["trend"], "divergence": cvd_data["divergence"]}
    if cvd_data["score"] != 0:
        score += cvd_data["score"] * 0.35
        signaux.extend(cvd_data.get("signals", [])[:1])

    # ── Keltner Channel ──
    kelt = compute_keltner(df)
    detail["keltner"] = {"squeeze_pro": kelt["squeeze_pro"]}
    if kelt["score"] != 0:
        score += kelt["score"] * 0.25
    if kelt["squeeze_pro"]:
        signaux.extend(kelt.get("signals", [])[:1])

    # ── Elder Ray ──
    elder = compute_elder_ray(df)
    detail["elder_ray"] = {"bull_power": elder["bull_power"], "bear_power": elder["bear_power"]}
    if elder["score"] != 0:
        score += elder["score"] * 0.25
        signaux.extend(elder.get("signals", [])[:1])

    # ── Donchian Channel ──
    don = compute_donchian(df)
    detail["donchian"] = {"breakout": don["breakout"], "pct_pos": don["pct_position"]}
    if don["breakout"]:
        score += don["score"] * 0.5   # Breakout = signal fort, poids renforcé
        signaux.extend(don.get("signals", [])[:1])
    elif don["score"] != 0:
        score += don["score"] * 0.2

    # ── Application du coefficient ADX (filtre régime) ──
    score *= adx_confidence

    # Clamp [-3, +3]
    score = max(-3, min(3, round(score, 2)))

    # Verdict
    if score >= 2.5:
        verdict = "🔥 TRÈS HAUSSIER"
    elif score >= 2.0:
        verdict = "📈 HAUSSIER"
    elif score >= 1.0:
        verdict = "↗️ LÉGÈREMENT HAUSSIER"
    elif score <= -2.5:
        verdict = "💥 TRÈS BAISSIER"
    elif score <= -2.0:
        verdict = "📉 BAISSIER"
    elif score <= -1.0:
        verdict = "↘️ LÉGÈREMENT BAISSIER"
    else:
        verdict = "➡️ NEUTRE"

    return {
        "score": score,
        "verdict": verdict,
        "detail": detail,
        "signaux": signaux,
        "ema": ema_data,
        "adx": adx_data,
    }


# ─── Analyse complète par actif ───────────────────────────────────────────────

def analyze_asset(ticker: str, df: pd.DataFrame) -> dict:
    """Calcule tous les indicateurs et le score composite pour un actif."""
    if df.empty or len(df) < 30:
        logger.warning(f"{ticker} : données insuffisantes pour l'analyse technique")
        return {"ticker": ticker, "erreur": "données insuffisantes"}

    close = df["close"]
    current_price = float(close.iloc[-1])
    atr = compute_atr(df)
    atr_val = float(atr.iloc[-1]) if not atr.empty else None

    pivot_levels = detect_pivot_levels(close)
    signal = compute_signal_score(df)

    nearest_support = pivot_levels["supports"][0] if pivot_levels["supports"] else None
    nearest_resistance = pivot_levels["resistances"][0] if pivot_levels["resistances"] else None

    # Stop dynamique : Parabolic SAR > support proche > ATR×2 (du plus précis au plus large)
    atr_stop = current_price - atr_val * 2 if atr_val else None
    psar_stop = signal.get("detail", {}).get("psar", {}).get("sar") if signal.get("detail", {}).get("psar", {}).get("bullish") else None
    smart_stop = psar_stop or nearest_support or atr_stop

    # Target basé sur résistance proche OU R/R 2:1
    if smart_stop and current_price:
        risk = abs(current_price - smart_stop)
        rr_target = current_price + risk * 2
        smart_target = nearest_resistance if (nearest_resistance and nearest_resistance > current_price + risk) else rr_target
    else:
        smart_target = nearest_resistance

    # Niveaux Fibonacci
    fib = compute_fibonacci(df)
    ichi = compute_ichimoku(df)
    cvd = compute_cvd(df)
    don = compute_donchian(df)
    elder = compute_elder_ray(df)

    return {
        "ticker": ticker,
        "prix_actuel": round(current_price, 6),
        "atr_14": round(atr_val, 6) if atr_val else None,
        "atr_pct": round(atr_val / current_price * 100, 2) if atr_val else None,
        "support_proche": nearest_support,
        "resistance_proche": nearest_resistance,
        "stop_proche": round(smart_stop, 6) if smart_stop else None,
        "target_proche": round(smart_target, 6) if smart_target else None,
        "niveaux": pivot_levels,
        "fibonacci": fib,
        "ichimoku": ichi,
        "cvd": cvd,
        "donchian": don,
        "elder_ray": elder,
        "signal": signal,
    }


def is_weekly_uptrend(df_daily: pd.DataFrame) -> dict:
    """
    Détermine si le ticker est en tendance haussière sur le timeframe hebdomadaire.

    Méthode : resample le daily OHLCV en weekly, calcule MA20 weekly.

    Paramètres :
        df_daily : DataFrame avec colonnes ['open','high','low','close','volume']
                   index datetime, données daily (90j minimum recommandé)

    Retourne :
        {
            "uptrend": bool,          # True si prix > MA20 weekly
            "ma20_weekly": float,     # valeur de la MA20 weekly
            "price_weekly": float,    # dernier close weekly
            "score_adj": float,       # multiplicateur : 1.0 si uptrend, 0.7 si downtrend
            "verdict": str,
        }

    Logique :
    - Resample daily → weekly (W) : open=first, high=max, low=min, close=last, volume=sum
    - Calculer MA20 sur les closes weekly
    - Si moins de 20 semaines de données → retourner uptrend=True, score_adj=1.0 (pas bloquant)
    - Si prix_weekly > ma20_weekly → uptrend=True, score_adj=1.0
    - Si prix_weekly <= ma20_weekly → uptrend=False, score_adj=0.7
    - En cas d'erreur → uptrend=True, score_adj=1.0 (pas bloquant)
    """
    _default = {
        "uptrend": True,
        "ma20_weekly": None,
        "price_weekly": None,
        "score_adj": 1.0,
        "verdict": "données insuffisantes — filtre weekly ignoré",
    }

    try:
        if df_daily.empty or "close" not in df_daily.columns:
            return _default

        # Resample daily → weekly
        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
        }
        if "volume" in df_daily.columns:
            agg["volume"] = "sum"

        df_weekly = df_daily.resample("W").agg(agg).dropna(subset=["close"])

        if len(df_weekly) < 20:
            return {**_default, "verdict": f"données insuffisantes ({len(df_weekly)} semaines < 20) — filtre weekly ignoré"}

        ma20 = df_weekly["close"].rolling(20).mean()
        ma20_val = float(ma20.iloc[-1])
        price_val = float(df_weekly["close"].iloc[-1])

        if pd.isna(ma20_val):
            return {**_default, "verdict": "MA20 weekly non calculable — filtre weekly ignoré"}

        uptrend = price_val > ma20_val

        return {
            "uptrend": uptrend,
            "ma20_weekly": round(ma20_val, 6),
            "price_weekly": round(price_val, 6),
            "score_adj": 1.0 if uptrend else 0.7,
            "verdict": "Tendance weekly HAUSSIÈRE (prix > MA20 weekly)" if uptrend else "Tendance weekly BAISSIÈRE (prix <= MA20 weekly)",
        }

    except Exception as e:
        logger.warning(f"is_weekly_uptrend erreur : {e}")
        return {**_default, "verdict": f"erreur — filtre weekly ignoré ({e})"}


def run(ohlcv_data: dict[str, pd.DataFrame]) -> dict[str, dict]:
    """Analyse technique de tous les actifs."""
    results = {}
    for ticker, df in ohlcv_data.items():
        results[ticker] = analyze_asset(ticker, df)
    return results
