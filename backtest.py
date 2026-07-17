"""
Backtest — rejoue la stratégie du bot sur l'historique OKX.

Réplique les règles réelles de production :
  Entrée : score technique >= 2.0 (analyze_asset, le même code que le scanner)
           BTC < MA50 → seuls les scores >= 2.5 passent, taille ×0.5
  Taille : 12% / 17% / 22% de l'équity selon le score (capital_allocator)
  Sorties (dans l'ordre de vérification quotidien) :
           1. Stop ATR figé à l'entrée (ATR14 ×1.5, borné -4% / -10%)
           2. Take profit +12% (+20% si score entrée >= 2.0)
           3. Trailing stop : pic >= +5% → plancher = pic × 0.50
           4. Time stop 7 jours
  Frais  : 0.10% par côté + 0.05% de slippage

Limites connues (à garder en tête en lisant les résultats) :
  - Score technique uniquement — news/microstructure/on-chain non
    reconstructibles historiquement. C'est fidèle au bot tel qu'il a
    réellement tradé jusqu'au 17/07/2026 (les autres sources étaient à 0).
  - Biais du survivant : l'univers est celui d'aujourd'hui, pas celui
    d'il y a un an (les tokens délistés n'y figurent plus).
  - Bougies daily : si stop ET TP touchés le même jour, le stop est
    compté en premier (hypothèse conservatrice).

Usage :
  python backtest.py                     # univers top 20, 400 jours
  python backtest.py --tickers SOL,DYDX  # tickers précis
  python backtest.py --days 600 --top 30
"""

import argparse
import json
import logging
import time
from datetime import datetime

import pandas as pd

import okx_client as okx
import technical_signals as ts

logger = logging.getLogger(__name__)

# ── Règles répliquées de la production ────────────────────────────────────────
ENTRY_SCORE        = 2.0    # seuil d'exécution auto (scanner.py)
BTC_BEAR_MIN_SCORE = 2.5    # mode baissier : seuls les signaux exceptionnels
BTC_BEAR_SIZE_MULT = 0.50
SIZE_TIERS         = [(2.5, 0.22), (2.0, 0.17), (1.5, 0.12)]  # capital_allocator
ATR_MULT           = 1.5    # position_manager.ATR_STOP_MULTIPLIER
ATR_STOP_MIN_PCT   = 0.04   # stop jamais plus serré que -4%
ATR_STOP_MAX_PCT   = 0.10   # stop jamais plus large que -10%
TP_PCT             = 12.0
TP_EXTENDED_PCT    = 20.0   # si score entrée >= 2.0 (STRONG_SCORE_MIN)
TRAIL_ACTIVATE     = 5.0    # trailing actif dès +5% de pic
TRAIL_RATIO        = 0.50
TIME_STOP_DAYS     = 7
FEE_PCT            = 0.0010  # 0.10% par côté (taker OKX)
SLIPPAGE_PCT       = 0.0005
WARMUP_DAYS        = 60      # bougies minimum avant le premier signal
CAPITAL_INITIAL    = 1000.0


# ── Données historiques ───────────────────────────────────────────────────────

def fetch_history(ticker: str, days: int = 400) -> pd.DataFrame:
    """
    Bougies daily paginées — /market/candles (récent) puis
    /market/history-candles (ancien) jusqu'à `days` bougies.
    """
    all_rows: list[dict] = []
    for quote in ("USDC", "USDT"):
        inst_id = f"{ticker.upper()}-{quote}"
        rows: list[dict] = []
        after = None
        endpoint = "/api/v5/market/candles"
        try:
            while len(rows) < days:
                params = {"instId": inst_id, "bar": "1D", "limit": "100"}
                if after:
                    params["after"] = after
                data = okx._get(endpoint, params)
                if not data:
                    if endpoint == "/api/v5/market/candles" and rows:
                        endpoint = "/api/v5/market/history-candles"
                        continue
                    break
                for c in data:
                    rows.append({
                        "timestamp": pd.to_datetime(int(c[0]), unit="ms"),
                        "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4]),
                        "volume": float(c[5]),
                    })
                after = data[-1][0]  # bougie la plus ancienne du lot
                time.sleep(0.15)     # anti rate-limit
            if rows:
                all_rows = rows
                break
        except Exception as e:
            logger.debug(f"fetch_history {inst_id} : {e}")
            continue

    if not all_rows:
        return pd.DataFrame()
    df = pd.DataFrame(all_rows).drop_duplicates(subset="timestamp")
    df.set_index("timestamp", inplace=True)
    df.sort_index(inplace=True)
    return df.tail(days)


# ── Moteur ────────────────────────────────────────────────────────────────────

def _position_size(score: float) -> float:
    for seuil, pct in SIZE_TIERS:
        if score >= seuil:
            return pct
    return 0.12


def _atr_stop(df: pd.DataFrame, prix: float) -> float:
    """Stop ATR figé à l'entrée — même formule que position_manager.get_atr_stop."""
    atr = ts.compute_atr(df)
    atr_val = float(atr.iloc[-1]) if not atr.empty else prix * 0.05
    stop = prix - atr_val * ATR_MULT
    stop = max(stop, prix * (1 - ATR_STOP_MAX_PCT))
    stop = min(stop, prix * (1 - ATR_STOP_MIN_PCT))
    return stop


def run_backtest(
    histories: dict[str, pd.DataFrame],
    btc_df: pd.DataFrame,
    capital: float = CAPITAL_INITIAL,
) -> dict:
    """
    Walk-forward jour par jour. À chaque date, seules les bougies
    antérieures sont visibles (aucune fuite du futur).
    """
    # Calendrier commun = union des dates, bornée par BTC (filtre MA50)
    all_dates = sorted(set().union(*[set(df.index) for df in histories.values()]))
    all_dates = [d for d in all_dates if d in btc_df.index]
    if len(all_dates) <= WARMUP_DAYS:
        raise ValueError("Historique insuffisant pour le warmup")

    btc_ma50 = btc_df["close"].rolling(50).mean()

    cash = capital
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    equity_curve: list[tuple] = []

    for date in all_dates[WARMUP_DAYS:]:
        # ── 1. Sorties (avant les entrées, comme en prod) ─────────────────────
        for tick in list(positions.keys()):
            df = histories[tick]
            if date not in df.index:
                continue
            pos = positions[tick]
            row = df.loc[date]
            pos["days"] += 1
            pos["peak_pnl"] = max(
                pos["peak_pnl"],
                (row["high"] - pos["entry"]) / pos["entry"] * 100,
            )
            close_pnl = (row["close"] - pos["entry"]) / pos["entry"] * 100
            tp = TP_EXTENDED_PCT if pos["score"] >= 2.0 else TP_PCT

            exit_price, reason = None, None
            if row["low"] <= pos["stop"]:                       # 1. stop d'abord (conservateur)
                exit_price, reason = pos["stop"], "stop_atr"
            elif (row["high"] - pos["entry"]) / pos["entry"] * 100 >= tp:
                exit_price = pos["entry"] * (1 + tp / 100)
                reason = "take_profit"
            elif pos["peak_pnl"] >= TRAIL_ACTIVATE:             # 3. trailing sur close
                floor = pos["peak_pnl"] * TRAIL_RATIO
                if close_pnl < floor:
                    exit_price, reason = row["close"], "trailing_stop"
            if exit_price is None and pos["days"] >= TIME_STOP_DAYS:
                exit_price, reason = row["close"], "time_stop"

            if exit_price is not None:
                exit_net = exit_price * (1 - FEE_PCT - SLIPPAGE_PCT)
                pnl_pct = (exit_net - pos["entry_net"]) / pos["entry_net"] * 100
                cash += pos["qty"] * exit_net
                trades.append({
                    "ticker": tick, "entry_date": str(pos["date"])[:10],
                    "exit_date": str(date)[:10], "days": pos["days"],
                    "score": pos["score"], "pnl_pct": round(pnl_pct, 2),
                    "reason": reason, "peak_pnl": round(pos["peak_pnl"], 2),
                    "btc_bear": pos["btc_bear"],
                })
                del positions[tick]

        # ── 2. Filtre BTC MA50 du jour ────────────────────────────────────────
        ma = btc_ma50.get(date)
        btc_bear = bool(ma and not pd.isna(ma) and btc_df["close"].loc[date] < ma)
        min_score = BTC_BEAR_MIN_SCORE if btc_bear else ENTRY_SCORE

        # ── 3. Entrées ────────────────────────────────────────────────────────
        for tick, df in histories.items():
            if tick in positions or date not in df.index:
                continue
            visible = df.loc[:date]
            if len(visible) < WARMUP_DAYS:
                continue
            try:
                analysis = ts.analyze_asset(tick, visible)
                score = analysis.get("signal", {}).get("score", 0)
            except Exception:
                continue
            if score < min_score:
                continue

            equity = cash + sum(
                p["qty"] * histories[t].loc[date, "close"]
                for t, p in positions.items() if date in histories[t].index
            )
            size_usd = equity * _position_size(score)
            if btc_bear:
                size_usd *= BTC_BEAR_SIZE_MULT
            size_usd = min(size_usd, equity * 0.25, cash)  # hard cap 25% + cash dispo
            if size_usd < 20:
                continue

            prix = float(df.loc[date, "close"])
            entry_net = prix * (1 + FEE_PCT + SLIPPAGE_PCT)
            positions[tick] = {
                "date": date, "entry": prix, "entry_net": entry_net,
                "qty": size_usd / entry_net, "score": round(score, 2),
                "stop": _atr_stop(visible, prix), "peak_pnl": 0.0,
                "days": 0, "btc_bear": btc_bear,
            }
            cash -= size_usd

        # ── 4. Équity du jour ─────────────────────────────────────────────────
        equity = cash + sum(
            p["qty"] * histories[t].loc[date, "close"]
            for t, p in positions.items() if date in histories[t].index
        )
        equity_curve.append((str(date)[:10], round(equity, 2)))

    # Liquider les positions restantes au dernier close (pour le bilan)
    last = all_dates[-1]
    for tick, pos in positions.items():
        if last in histories[tick].index:
            px = histories[tick].loc[last, "close"] * (1 - FEE_PCT - SLIPPAGE_PCT)
            pnl_pct = (px - pos["entry_net"]) / pos["entry_net"] * 100
            trades.append({
                "ticker": tick, "entry_date": str(pos["date"])[:10],
                "exit_date": str(last)[:10], "days": pos["days"],
                "score": pos["score"], "pnl_pct": round(pnl_pct, 2),
                "reason": "fin_backtest", "peak_pnl": round(pos["peak_pnl"], 2),
                "btc_bear": pos["btc_bear"],
            })

    return {"trades": trades, "equity_curve": equity_curve, "capital_initial": capital}


# ── Métriques ─────────────────────────────────────────────────────────────────

def compute_metrics(result: dict) -> dict:
    trades = [t for t in result["trades"] if t["reason"] != "fin_backtest"]
    curve = [e for _, e in result["equity_curve"]]
    if not trades or not curve:
        return {"erreur": "aucun trade"}

    wins = [t for t in trades if t["pnl_pct"] > 0]
    losses = [t for t in trades if t["pnl_pct"] <= 0]
    gross_win = sum(t["pnl_pct"] for t in wins)
    gross_loss = abs(sum(t["pnl_pct"] for t in losses))

    peak, max_dd = curve[0], 0.0
    for e in curve:
        peak = max(peak, e)
        max_dd = max(max_dd, (peak - e) / peak * 100)

    reasons: dict[str, dict] = {}
    for t in trades:
        r = reasons.setdefault(t["reason"], {"n": 0, "pnl_total": 0.0})
        r["n"] += 1
        r["pnl_total"] = round(r["pnl_total"] + t["pnl_pct"], 1)

    # Robustesse : première moitié vs seconde moitié de la période
    mid = trades[len(trades) // 2]["entry_date"] if len(trades) >= 10 else None
    halves = {}
    if mid:
        for label, subset in [
            ("moitie_1", [t for t in trades if t["entry_date"] < mid]),
            ("moitie_2", [t for t in trades if t["entry_date"] >= mid]),
        ]:
            if subset:
                halves[label] = {
                    "trades": len(subset),
                    "wr": round(sum(1 for t in subset if t["pnl_pct"] > 0) / len(subset) * 100, 1),
                    "ev": round(sum(t["pnl_pct"] for t in subset) / len(subset), 2),
                }

    return {
        "periode": f"{result['equity_curve'][0][0]} → {result['equity_curve'][-1][0]}",
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "ev_par_trade_pct": round(sum(t["pnl_pct"] for t in trades) / len(trades), 2),
        "gain_moyen_pct": round(gross_win / len(wins), 2) if wins else 0,
        "perte_moyenne_pct": round(-gross_loss / len(losses), 2) if losses else 0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else float("inf"),
        "max_drawdown_pct": round(max_dd, 1),
        "equity_finale": curve[-1],
        "rendement_total_pct": round((curve[-1] / result["capital_initial"] - 1) * 100, 1),
        "sorties": reasons,
        "robustesse": halves,
        "trades_btc_bear": sum(1 for t in trades if t["btc_bear"]),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtest de la stratégie du bot")
    parser.add_argument("--tickers", type=str, default="", help="Liste CSV (défaut : top N de l'univers)")
    parser.add_argument("--top", type=int, default=20, help="Taille de l'univers si --tickers absent")
    parser.add_argument("--days", type=int, default=400, help="Profondeur d'historique")
    parser.add_argument("--capital", type=float, default=CAPITAL_INITIAL)
    parser.add_argument("--out", type=str, default="", help="Fichier JSON de sortie (optionnel)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.tickers:
        universe = [t.strip().upper() for t in args.tickers.split(",")]
    else:
        from scanner import get_universe
        universe = get_universe()[:args.top]
    logger.info(f"Univers backtest : {len(universe)} tickers, {args.days}j")

    btc_df = fetch_history("BTC", args.days + 60)  # marge pour la MA50
    if btc_df.empty:
        raise SystemExit("Impossible de récupérer l'historique BTC")

    histories = {}
    for t in universe:
        df = fetch_history(t, args.days)
        if len(df) >= WARMUP_DAYS + 30:
            histories[t] = df
        else:
            logger.info(f"  {t} : historique trop court ({len(df)}j) — ignoré")
    logger.info(f"{len(histories)} tickers avec historique suffisant")

    result = run_backtest(histories, btc_df, args.capital)
    metrics = compute_metrics(result)

    print("\n" + "=" * 60)
    print("RÉSULTATS BACKTEST")
    print("=" * 60)
    print(json.dumps(metrics, indent=2, ensure_ascii=False))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"metrics": metrics, **result}, f, indent=2, ensure_ascii=False)
        print(f"\nDétail complet : {args.out}")


if __name__ == "__main__":
    main()
