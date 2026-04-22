"""
Point d'entrée GitHub Actions — exécuté toutes les 4h.
Cycle complet : gestion positions → scan nouvelles opportunités.
"""
import sys
import io
import logging
from datetime import datetime, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

import okx_client as okx
import technical_signals as ts
import position_manager as pm
import scanner
import alertes

now = datetime.now(timezone.utc)
print(f"\n{'='*55}")
print(f"  CYCLE {now.strftime('%d/%m/%Y %H:%M UTC')}")
print(f"{'='*55}\n")

# ── Balance et état du compte ──────────────────────────────────────────────
try:
    balances = okx.get_balances()
    usdc = balances.get("USDC", 0) + balances.get("USDT", 0)
    positions_ouvertes = {k: v for k, v in balances.items() if k not in ("USDC", "USDT")}
    portfolio_value = usdc + sum(
        (okx.get_price_usdc(t) or 0) * q for t, q in positions_ouvertes.items()
    )
    print(f"Portfolio total : ${portfolio_value:.2f}")
    print(f"USDC libre      : ${usdc:.2f}")
    print(f"Positions       : {list(positions_ouvertes.keys()) or 'aucune'}\n")
except Exception as e:
    print(f"Erreur compte : {e}")
    usdc = 100
    portfolio_value = 100

# ── Étape 1 : Gestion des positions existantes ────────────────────────────
print("─── GESTION POSITIONS ───")
try:
    pm_result = pm.run(portfolio_value)
    actions = pm_result.get("actions", [])
    if actions:
        print(f"{len(actions)} action(s) exécutée(s) sur les positions")
    else:
        print("Toutes les positions maintenues (HOLD)")
except Exception as e:
    print(f"Erreur gestion positions : {e}")

# ── Étape 2 : Recalculer la balance après les ventes éventuelles ──────────
try:
    balances = okx.get_balances()
    usdc = balances.get("USDC", 0) + balances.get("USDT", 0)
    nb_positions = len([k for k in balances if k not in ("USDC", "USDT")])
except Exception:
    pass

# ── Étape 3 : Scan nouvelles opportunités ─────────────────────────────────
print("\n─── SCAN OPPORTUNITÉS ───")
try:
    # On scanne toujours, même si peu de USDC libre
    # (le scanner vérifie la balance avant de placer des ordres)
    signals = scanner.run_scan(portfolio_value=usdc)
    print(f"{len(signals)} signal(s) actionnable(s)")
except Exception as e:
    print(f"Erreur scan : {e}")

# ── Rapport quotidien à 7h UTC ─────────────────────────────────────────────
if now.hour == 7:
    print("\n─── RAPPORT QUOTIDIEN ───")
    try:
        tickers_watch = ["BTC", "ETH", "SOL", "LINK", "AVAX", "TIA", "NEAR", "ARB", "INJ", "AAVE"]
        ohlcv = okx.get_all_ohlcv(tickers_watch, days=90)
        tech = ts.run(ohlcv)

        lines = [f"📊 *Rapport {now.strftime('%d/%m/%Y')}*\n",
                 f"💼 Portfolio : `${portfolio_value:.2f}` | USDC libre : `${usdc:.2f}`\n",
                 "*Signaux du marché :*"]

        for ticker, t in tech.items():
            if "erreur" in t:
                continue
            score = t.get("signal", {}).get("score", 0)
            verdict = t.get("signal", {}).get("verdict", "")
            prix = t.get("prix_actuel", 0)
            bar = "█" * min(int((score + 3) / 6 * 8), 8)
            lines.append(f"`{ticker:6}` `{bar:8}` {score:+.2f}  ${prix:.4f}")

        alertes.send("\n".join(lines))
        print("Rapport envoyé sur Telegram.")
    except Exception as e:
        print(f"Erreur rapport : {e}")

print(f"\n{'='*55}")
print(f"  Cycle terminé.")
print(f"{'='*55}\n")
