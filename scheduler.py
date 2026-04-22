"""
Planificateur — lance le scanner toutes les 4h et le rapport quotidien à 7h.
Utilise OKX comme exchange principal.
Tourne en arrière-plan, s'arrête proprement avec Ctrl+C.
"""

import logging
import sys
import io
import time
from datetime import datetime, date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scheduler.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("scheduler")

SCAN_INTERVAL_HOURS = 4
RAPPORT_HEURE = 7


def get_portfolio_value() -> float:
    """Récupère la valeur USDT disponible sur OKX."""
    try:
        import okx_client as okx
        balances = okx.get_balances()
        return balances.get("USDT", 0.0) + balances.get("USDC", 0.0)
    except Exception as e:
        logger.error(f"Erreur récupération valeur portefeuille OKX : {e}")
        return 718.0


def run_daily_report():
    """Génère et envoie le rapport quotidien complet."""
    logger.info("Génération rapport quotidien...")
    try:
        import okx_client as okx
        import technical_signals as ts
        import risk_analysis
        import alertes

        tickers = ["BTC", "ETH", "SOL", "LINK", "AVAX", "TIA", "INJ", "ARB", "JTO", "NEAR"]
        ohlcv_data = okx.get_all_ohlcv(tickers, days=90)
        tech_results = ts.run(ohlcv_data)

        # Résumé des signaux actifs
        lines = ["📊 *Rapport quotidien OKX*\n"]
        for ticker, tech in tech_results.items():
            if "erreur" in tech:
                continue
            score = tech.get("signal", {}).get("score", 0)
            verdict = tech.get("signal", {}).get("verdict", "NEUTRE")
            prix = tech.get("prix_actuel", 0)
            lines.append(f"`{ticker:8}` prix=${prix:.4f} score={score:+.1f} {verdict}")

        alertes.send("\n".join(lines))
        logger.info("Rapport quotidien envoyé.")
    except Exception as e:
        logger.error(f"Erreur rapport quotidien : {e}")
        import alertes
        alertes.send(f"❌ Erreur rapport quotidien : {str(e)[:100]}")


def main():
    import scanner
    import alertes

    logger.info("Planificateur OKX démarré.")
    alertes.send(
        "🤖 *Système de surveillance actif*\n"
        "📡 Exchange : OKX (eea.okx.com)\n"
        "🔄 Scan toutes les 4h | Rapport quotidien à 7h\n"
        "✅ Validation technique + fondamentale activée"
    )

    last_scan = 0
    last_report_date = None

    while True:
        now = datetime.now()

        # Rapport quotidien à 7h
        if now.hour == RAPPORT_HEURE and last_report_date != date.today():
            run_daily_report()
            last_report_date = date.today()

        # Scan toutes les 4h
        if time.time() - last_scan >= SCAN_INTERVAL_HOURS * 3600:
            portfolio_value = get_portfolio_value()
            logger.info(f"Budget disponible : ${portfolio_value:.2f}")
            scanner.run_scan(portfolio_value)
            last_scan = time.time()

        time.sleep(60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Planificateur arrêté.")
