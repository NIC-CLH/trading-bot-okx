"""
Volume Profile — niveaux de prix où le plus de volume a été échangé.

Les institutionnels (market makers, fonds) placent leurs ordres autour
du Point of Control (POC) et des bornes de la Value Area.
Ces niveaux sont les supports/résistances les plus respectés du marché.

Concepts :
- POC (Point of Control) : prix avec le plus grand volume échangé
- VAH (Value Area High) : borne haute de la zone de valeur (70% du volume)
- VAL (Value Area Low) : borne basse de la zone de valeur (70% du volume)
- HVN (High Volume Node) : zones d'accumulation → supports/résistances forts
- LVN (Low Volume Node) : zones de faible liquidité → prix traversent vite
"""

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_volume_profile(df: pd.DataFrame, n_bins: int = 50, lookback_days: int = 30) -> dict:
    """
    Calcule le Volume Profile sur les N derniers jours.

    Args:
        df: DataFrame OHLCV
        n_bins: nombre de niveaux de prix (résolution)
        lookback_days: fenêtre d'analyse

    Returns:
        POC, VAH, VAL, HVNs, LVNs, et analyse par rapport au prix actuel
    """
    if df.empty or len(df) < 5:
        return {"error": "données insuffisantes"}

    # Limiter à la fenêtre
    df_window = df.tail(lookback_days).copy()
    if df_window.empty:
        return {"error": "fenêtre vide"}

    price_min = float(df_window["low"].min())
    price_max = float(df_window["high"].max())

    if price_min >= price_max:
        return {"error": "range de prix invalide"}

    # Créer les bins de prix
    bins = np.linspace(price_min, price_max, n_bins + 1)
    bin_centers = (bins[:-1] + bins[1:]) / 2
    volume_at_price = np.zeros(n_bins)

    # Distribuer le volume de chaque bougie uniformément entre low et high
    for _, row in df_window.iterrows():
        low, high, vol = row["low"], row["high"], row["volume"]
        if vol <= 0 or pd.isna(vol):
            continue

        # Trouver les bins touchés par cette bougie
        low_idx = np.searchsorted(bins, low, side="left")
        high_idx = np.searchsorted(bins, high, side="right")
        low_idx = max(0, min(low_idx, n_bins - 1))
        high_idx = max(0, min(high_idx, n_bins))

        n_touched = high_idx - low_idx
        if n_touched <= 0:
            n_touched = 1
            high_idx = low_idx + 1

        vol_per_bin = vol / n_touched
        volume_at_price[low_idx:high_idx] += vol_per_bin

    if volume_at_price.sum() == 0:
        return {"error": "volume nul"}

    # POC — Point of Control
    poc_idx = int(np.argmax(volume_at_price))
    poc_price = float(bin_centers[poc_idx])

    # Value Area (70% du volume total)
    total_vol = volume_at_price.sum()
    target_vol = total_vol * 0.70

    # Expansion depuis le POC vers le haut et le bas
    accumulated = volume_at_price[poc_idx]
    low_idx_va = poc_idx
    high_idx_va = poc_idx

    while accumulated < target_vol:
        can_go_up = high_idx_va + 1 < n_bins
        can_go_down = low_idx_va - 1 >= 0

        if can_go_up and can_go_down:
            vol_up = volume_at_price[high_idx_va + 1]
            vol_down = volume_at_price[low_idx_va - 1]
            if vol_up >= vol_down:
                high_idx_va += 1
                accumulated += vol_up
            else:
                low_idx_va -= 1
                accumulated += vol_down
        elif can_go_up:
            high_idx_va += 1
            accumulated += volume_at_price[high_idx_va]
        elif can_go_down:
            low_idx_va -= 1
            accumulated += volume_at_price[low_idx_va]
        else:
            break

    vah = float(bin_centers[high_idx_va])
    val = float(bin_centers[low_idx_va])

    # HVN / LVN — Nœuds de haut et bas volume
    vol_mean = volume_at_price.mean()
    vol_std = volume_at_price.std()

    hvn_prices = [float(bin_centers[i]) for i in range(n_bins)
                  if volume_at_price[i] > vol_mean + vol_std]
    lvn_prices = [float(bin_centers[i]) for i in range(n_bins)
                  if volume_at_price[i] < vol_mean - 0.5 * vol_std]

    # Analyse par rapport au prix actuel
    current_price = float(df["close"].iloc[-1])

    in_value_area = val <= current_price <= vah
    above_poc = current_price > poc_price
    distance_poc_pct = (current_price - poc_price) / poc_price * 100

    # Nearest HVN supports (sous le prix) et resistances (au-dessus)
    supports = sorted([p for p in hvn_prices if p < current_price], reverse=True)[:3]
    resistances = sorted([p for p in hvn_prices if p > current_price])[:3]

    return {
        "poc": round(poc_price, 6),
        "vah": round(vah, 6),
        "val": round(val, 6),
        "current_price": round(current_price, 6),
        "in_value_area": in_value_area,
        "above_poc": above_poc,
        "distance_poc_pct": round(distance_poc_pct, 2),
        "hvn_supports": [round(p, 6) for p in supports],
        "hvn_resistances": [round(p, 6) for p in resistances],
        "lvn_prices": [round(p, 6) for p in lvn_prices[:5]],
        "lookback_days": lookback_days,
    }


def analyze(df: pd.DataFrame) -> dict:
    """
    Analyse Volume Profile et retourne un score + signaux pour le scanner.
    Score : -1.0 à +1.0
    """
    if df.empty:
        return {"score": 0.0, "verdict": "Volume Profile indisponible", "signals": []}

    vp = compute_volume_profile(df, lookback_days=30)

    if "error" in vp:
        return {"score": 0.0, "verdict": "Volume Profile indisponible", "signals": []}

    score = 0.0
    signals = []

    current = vp["current_price"]
    poc = vp["poc"]
    vah = vp["vah"]
    val = vp["val"]
    dist_poc = vp["distance_poc_pct"]

    # Prix dans la Value Area (zone de valeur institutionnelle)
    if vp["in_value_area"]:
        if vp["above_poc"]:
            score += 0.3
            signals.append(f"Prix au-dessus du POC (${poc:.4f}) dans la Value Area → momentum haussier")
        else:
            score -= 0.2
            signals.append(f"Prix en dessous du POC (${poc:.4f}) dans la Value Area → momentum baissier")
    else:
        # Hors Value Area
        if current > vah:
            score += 0.5
            signals.append(f"Prix au-dessus de la Value Area (VAH=${vah:.4f}) → breakout haussier fort")
        elif current < val:
            score -= 0.5
            signals.append(f"Prix en dessous de la Value Area (VAL=${val:.4f}) → breakdown baissier fort")

    # HVN supports proches
    if vp["hvn_supports"]:
        nearest_support = vp["hvn_supports"][0]
        dist_support = (current - nearest_support) / current * 100
        if dist_support < 3:
            score += 0.2
            signals.append(f"Support HVN très proche à ${nearest_support:.4f} ({dist_support:.1f}% sous le prix)")

    # HVN résistances proches
    if vp["hvn_resistances"]:
        nearest_resistance = vp["hvn_resistances"][0]
        dist_resistance = (nearest_resistance - current) / current * 100
        if dist_resistance < 3:
            score -= 0.2
            signals.append(f"Résistance HVN très proche à ${nearest_resistance:.4f} ({dist_resistance:.1f}% au-dessus)")

    score = round(max(-1.0, min(1.0, score)), 2)

    verdict = (
        "Volume Profile bullish" if score > 0.3 else
        "Volume Profile bearish" if score < -0.3 else
        "Volume Profile neutre"
    )

    return {
        "score": score,
        "verdict": verdict,
        "signals": signals,
        "poc": poc,
        "vah": vah,
        "val": val,
        "in_value_area": vp["in_value_area"],
        "hvn_supports": vp["hvn_supports"],
        "hvn_resistances": vp["hvn_resistances"],
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    import okx_client as okx
    logging.basicConfig(level=logging.INFO)

    ohlcv = okx.get_all_ohlcv(["BTC", "ETH", "SOL"], days=60)
    for ticker, df in ohlcv.items():
        r = analyze(df)
        print(f"\n{ticker}: score={r['score']:+.2f} | POC=${r.get('poc', 0):.4f} | "
              f"VAH=${r.get('vah', 0):.4f} | VAL=${r.get('val', 0):.4f}")
        for s in r["signals"]:
            print(f"  • {s}")
