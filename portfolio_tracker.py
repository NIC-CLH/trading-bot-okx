"""
Module 1 — Portfolio Tracker (Binance)
Récupère les balances réelles depuis Binance, calcule P&L, allocations,
exporte un snapshot JSON et persiste en SQLite.
"""

import csv
import json
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

import config
import binance_client as bc

logger = logging.getLogger(__name__)


# ─── Prix d'achat depuis CSV (fallback) ──────────────────────────────────────

def load_cost_basis_csv(csv_path: str = config.PORTFOLIO_CSV) -> dict[str, dict]:
    """
    Charge les prix d'achat et dates depuis le CSV (optionnel).
    Utilisé comme fallback si l'historique Binance est insuffisant.
    """
    basis = {}
    if not Path(csv_path).exists():
        return basis

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ticker = row.get("ticker", "").strip().upper()
            if ticker:
                try:
                    basis[ticker] = {
                        "prix_achat": float(row.get("prix_achat", 0)),
                        "date_achat": date.fromisoformat(row["date_achat"].strip()),
                        "note": row.get("note", "").strip(),
                    }
                except (ValueError, KeyError):
                    pass
    return basis


# ─── Calcul snapshot ─────────────────────────────────────────────────────────

def compute_snapshot(
    client,
    cost_basis_csv: dict[str, dict] = None,
    fetch_trade_history: bool = True,
) -> dict:
    """
    Construit le snapshot complet depuis Binance.
    - Balances : Binance API (temps réel)
    - Prix d'achat : historique trades Binance ou CSV fallback
    """
    cost_basis_csv = cost_basis_csv or {}

    # 1. Balances réelles (Spot + Simple Earn)
    balances = bc.get_all_balances(client)
    if not balances:
        raise RuntimeError("Impossible de récupérer les balances Binance")

    tickers = list(balances.keys())

    # 2. Prix actuels
    prices = bc.get_prices_usd(client, tickers)

    # 3. Prix d'achat (Binance trade history ou CSV)
    cost_data = {}
    for ticker in tickers:
        if ticker.lower() in config.STABLECOINS:
            cost_data[ticker] = {"prix_achat": 1.0, "date_achat": date.today(), "note": "stablecoin"}
            continue

        if fetch_trade_history:
            history = bc.get_avg_buy_price(client, ticker)
            if history:
                cost_data[ticker] = {
                    "prix_achat": history["prix_achat_moyen"],
                    "date_achat": history["date_premier_achat"],
                    "note": f"{history['nb_trades']} trades Binance",
                }
                continue

        # Fallback CSV
        if ticker in cost_basis_csv:
            cost_data[ticker] = cost_basis_csv[ticker]
        else:
            logger.warning(f"{ticker} : prix d'achat inconnu (ni Binance ni CSV)")
            cost_data[ticker] = {"prix_achat": None, "date_achat": date.today(), "note": "inconnu"}

    # 4. Calcul P&L par position
    positions = []
    total_value = 0.0
    total_cost = 0.0

    for ticker, qty in balances.items():
        current_price = prices.get(ticker)
        cost_info = cost_data.get(ticker, {})
        avg_cost = cost_info.get("prix_achat")
        buy_date = cost_info.get("date_achat", date.today())
        is_stable = ticker.lower() in config.STABLECOINS

        current_value = qty * current_price if current_price else None
        cost_basis_val = qty * avg_cost if avg_cost else None
        pnl_abs = (current_value - cost_basis_val) if (current_value and cost_basis_val) else None
        pnl_pct = (pnl_abs / cost_basis_val * 100) if (pnl_abs is not None and cost_basis_val) else None
        days_held = (date.today() - buy_date).days if buy_date else None

        pos = {
            "ticker": ticker,
            "quantite": round(qty, 8),
            "prix_achat": round(avg_cost, 8) if avg_cost else None,
            "prix_actuel": round(current_price, 8) if current_price else None,
            "valeur_cout": round(cost_basis_val, 2) if cost_basis_val else None,
            "valeur_actuelle": round(current_value, 2) if current_value else None,
            "pnl_absolu": round(pnl_abs, 2) if pnl_abs is not None else None,
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "jours_detention": days_held,
            "is_stablecoin": is_stable,
            "note": cost_info.get("note", ""),
        }
        positions.append(pos)

        if current_value:
            total_value += current_value
        if cost_basis_val:
            total_cost += cost_basis_val

    # 5. Allocations %
    for pos in positions:
        if pos["valeur_actuelle"] and total_value > 0:
            pos["allocation_pct"] = round(pos["valeur_actuelle"] / total_value * 100, 2)
        else:
            pos["allocation_pct"] = None

    # 6. Métriques globales
    stable_value = sum(
        p["valeur_actuelle"] for p in positions
        if p["is_stablecoin"] and p["valeur_actuelle"]
    )
    stable_pct = (stable_value / total_value * 100) if total_value else 0
    total_pnl = total_value - total_cost
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0

    positions_sorted = sorted(positions, key=lambda x: x["valeur_actuelle"] or 0, reverse=True)

    snapshot = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "valeur_totale_usd": round(total_value, 2),
        "cout_total_usd": round(total_cost, 2),
        "pnl_total_absolu": round(total_pnl, 2),
        "pnl_total_pct": round(total_pnl_pct, 2),
        "stablecoin_pct": round(stable_pct, 2),
        "positions": positions_sorted,
    }

    return snapshot


# ─── Alertes concentration ────────────────────────────────────────────────────

def check_concentration_alerts(snapshot: dict) -> list[str]:
    alerts = []
    threshold = config.MAX_POSITION_PCT * 100

    for pos in snapshot["positions"]:
        alloc = pos["allocation_pct"]
        if alloc and alloc > threshold:
            alerts.append(
                f"[CONCENTRATION] {pos['ticker']} à {alloc:.1f}% du portefeuille "
                f"(seuil : {threshold:.0f}%)"
            )

    if snapshot["stablecoin_pct"] < config.MIN_STABLECOIN_PCT * 100:
        alerts.append(
            f"[LIQUIDITE] Stablecoins à {snapshot['stablecoin_pct']:.1f}% "
            f"(minimum recommandé : {config.MIN_STABLECOIN_PCT*100:.0f}%)"
        )

    return alerts


# ─── Persistance ──────────────────────────────────────────────────────────────

def save_snapshot_to_db(snapshot: dict, db_path: str = config.DB_PATH):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            valeur_totale REAL,
            cout_total REAL,
            pnl_absolu REAL,
            pnl_pct REAL,
            stablecoin_pct REAL,
            snapshot_json TEXT
        )
    """)
    cursor.execute("""
        INSERT INTO snapshots
        (timestamp, valeur_totale, cout_total, pnl_absolu, pnl_pct, stablecoin_pct, snapshot_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        snapshot["timestamp"],
        snapshot["valeur_totale_usd"],
        snapshot["cout_total_usd"],
        snapshot["pnl_total_absolu"],
        snapshot["pnl_total_pct"],
        snapshot["stablecoin_pct"],
        json.dumps(snapshot, default=str),
    ))
    conn.commit()
    conn.close()


def export_snapshot(snapshot: dict, path: str = "snapshot.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, default=str)


# ─── Point d'entrée ──────────────────────────────────────────────────────────

def run(client=None, fetch_trade_history: bool = True) -> dict:
    if client is None:
        client = bc.get_client()

    cost_basis = load_cost_basis_csv()
    snapshot = compute_snapshot(client, cost_basis, fetch_trade_history)
    alerts = check_concentration_alerts(snapshot)
    snapshot["alertes_concentration"] = alerts

    export_snapshot(snapshot)
    save_snapshot_to_db(snapshot)

    return snapshot
