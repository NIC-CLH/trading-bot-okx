"""
Orchestrateur principal — Binance API.
Usage : python main.py [--no-report] [--skip-risk] [--no-trade-history]
"""

import argparse
import logging
import sys
import io

# Fix encodage Windows (cp1252 -> utf-8)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("portfolio.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")


def print_section(title: str):
    print(f"\n{'━'*55}")
    print(f"  {title}")
    print(f"{'━'*55}")


def main():
    parser = argparse.ArgumentParser(description="Gestionnaire de portefeuille crypto — Binance")
    parser.add_argument("--no-report", action="store_true", help="Ne pas générer le rapport Markdown")
    parser.add_argument("--skip-risk", action="store_true", help="Sauter l'analyse de risque")
    parser.add_argument("--no-trade-history", action="store_true",
                        help="Ne pas récupérer l'historique des trades (plus rapide)")
    args = parser.parse_args()

    # Connexion Binance unique partagée entre les modules
    import binance_client as bc
    print_section("CONNEXION BINANCE")
    client = bc.get_client()
    print("  Connexion établie.")

    # ── Module 1 : Portfolio Tracker ──────────────────────────────────────────
    print_section("MODULE 1 — PORTFOLIO TRACKER")
    import portfolio_tracker

    fetch_history = not args.no_trade_history
    if not fetch_history:
        print("  Mode rapide : prix d'achat depuis CSV uniquement.")

    snapshot = portfolio_tracker.run(client=client, fetch_trade_history=fetch_history)

    print(f"\n  Valeur totale  : ${snapshot['valeur_totale_usd']:>12,.2f}")
    print(f"  P&L total      : ${snapshot['pnl_total_absolu']:>+12,.2f} ({snapshot['pnl_total_pct']:+.2f}%)")
    print(f"  Stablecoins    : {snapshot['stablecoin_pct']:.1f}%")
    print(f"\n  Positions ({len(snapshot['positions'])}) :")

    for p in snapshot["positions"]:
        pnl = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "   N/A"
        alloc = f"{p['allocation_pct']:.1f}%" if p["allocation_pct"] else "─"
        price = f"${p['prix_actuel']:,.4f}" if p["prix_actuel"] else "─"
        print(f"    {p['ticker']:<10} {alloc:>6}   {price:>14}   {pnl}")

    if snapshot.get("alertes_concentration"):
        print("\n  Alertes :")
        for a in snapshot["alertes_concentration"]:
            print(f"    ⚠ {a}")

    tickers = [p["ticker"] for p in snapshot["positions"] if not p["is_stablecoin"]]

    # ── Module 2 : Analyse de Risque ──────────────────────────────────────────
    risk_data = {}
    ohlcv_data = {}

    print_section("MODULE 2 — TÉLÉCHARGEMENT OHLCV (Binance)")
    ohlcv_data = bc.get_all_ohlcv(client, tickers)
    print(f"  {len(ohlcv_data)} actifs chargés.")

    if not args.skip_risk:
        print_section("MODULE 2 — ANALYSE DE RISQUE")
        import risk_analysis

        risk_data = risk_analysis.run_from_ohlcv(snapshot, ohlcv_data)

        perf = risk_data.get("performance_portefeuille", {})
        print(f"\n  Sharpe (90j)    : {perf.get('sharpe', 'N/A')}")
        print(f"  Sortino (90j)   : {perf.get('sortino', 'N/A')}")
        print(f"  Corrélation moy : {risk_data.get('correlation_moyenne', 'N/A')}")

        var = risk_data.get("var", {})
        if var.get("VaR_95_1j"):
            v = var["VaR_95_1j"]
            print(f"  VaR 95% 1j      : {v['var_pct']:.2f}% (${v['var_usd']:,.0f})")
        if var.get("VaR_99_1j"):
            v = var["VaR_99_1j"]
            print(f"  VaR 99% 1j      : {v['var_pct']:.2f}% (${v['var_usd']:,.0f})")

        if risk_data.get("alertes_risque"):
            print("\n  Alertes risque :")
            for a in risk_data["alertes_risque"]:
                print(f"    ⚠ {a}")

    # ── Module 3 : Signaux Techniques ─────────────────────────────────────────
    print_section("MODULE 3 — SIGNAUX TECHNIQUES")
    import technical_signals

    tech_results = technical_signals.run(ohlcv_data)

    print(f"\n  {'Actif':<12} {'Score':>7}  Verdict")
    print(f"  {'─'*12} {'─'*7}  {'─'*22}")
    for ticker, tech in tech_results.items():
        if "erreur" in tech:
            print(f"  {ticker:<12} {'─':>7}  {tech['erreur']}")
        else:
            sig = tech.get("signal", {})
            score = sig.get("score", 0)
            verdict = sig.get("verdict", "─")
            print(f"  {ticker:<12} {score:>+7.1f}  {verdict}")

    # ── Module 4 : Gestion du Risque & Sizing ────────────────────────────────
    print_section("MODULE 4 — GESTION DU RISQUE & SIZING")
    import risk_management

    rm_results = risk_management.run(snapshot, tech_results, risk_data)

    print(f"\n  {rm_results['nb_alertes']} alerte(s) active(s)\n")
    for pa in rm_results["positions_analyse"]:
        ticker = pa["ticker"]
        stop = pa.get("stop_loss", {})
        kelly = pa.get("kelly", {})
        pct = pa.get("position_pct_portfolio", 0)
        stop_str = f"${stop.get('stop_price'):,.4f}" if stop.get("stop_price") else "─"
        kelly_str = (f"{kelly.get('kelly_partiel_pct'):.1f}%"
                     if kelly.get("kelly_partiel_pct") is not None else "─")
        print(f"  {ticker:<10}  Stop: {stop_str:<16} Kelly: {kelly_str:<8} Poids: {pct:.1f}%")
        for a in pa.get("alertes", []):
            print(f"    ⚠ {a}")

    # ── Module 5 : Rapport ────────────────────────────────────────────────────
    if not args.no_report:
        print_section("MODULE 5 — GÉNÉRATION DU RAPPORT")
        import rapport

        report_path = rapport.run(snapshot, risk_data, tech_results, rm_results)
        print(f"\n  Rapport sauvegardé : {report_path}")

    print(f"\n{'━'*55}")
    print("  Analyse terminée.")
    print(f"{'━'*55}\n")


if __name__ == "__main__":
    main()
