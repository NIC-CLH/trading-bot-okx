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

# Supprime toutes les alertes intermédiaires — on envoie un seul résumé à la fin
alertes.send = lambda *a, **kw: None
alertes.alerte_opportunite_enrichie = lambda *a, **kw: None
alertes.alerte_portefeuille = lambda *a, **kw: None
alertes.alerte_risque = lambda *a, **kw: None

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
print(f"  Cycle terminé — envoi résumé Telegram")
print(f"{'='*55}\n")

# ── Résumé unique envoyé sur Telegram ─────────────────────────────────────
import requests as _req, os as _os

def _send_final(msg):
    token = _os.getenv("TELEGRAM_TOKEN")
    chat_id = _os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )

try:
    balances_final = okx.get_balances()
    usdc_final = balances_final.get("USDC", 0) + balances_final.get("USDT", 0)
    positions_final = {k: v for k, v in balances_final.items() if k not in ("USDC", "USDT")}

    # Valeur totale
    portfolio_final = usdc_final
    pos_lines = []
    for ticker, qty in positions_final.items():
        prix = okx.get_price_usdc(ticker) or 0
        valeur = qty * prix
        portfolio_final += valeur

        # P&L
        entry = pm._get_entry_price(ticker)
        if entry and prix:
            pnl_pct = (prix - entry) / entry * 100
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            pos_lines.append(f"{pnl_emoji} *{ticker}* `${prix:.4f}` — P&L `{pnl_pct:+.1f}%`")
        else:
            pos_lines.append(f"⚪ *{ticker}* `${prix:.4f}`")

    # Décisions prises ce cycle
    actions_taken = pm_result.get("actions", []) if "pm_result" in dir() else []
    decisions_lines = []
    for a in actions_taken:
        emoji = "💰" if "SELL" in a["decision"] else "📈"
        decisions_lines.append(f"{emoji} {a['ticker']} — {a['raison']}")

    # Nouveaux ordres
    signals_taken = signals if "signals" in dir() else []
    for s in signals_taken:
        decisions_lines.append(f"🛒 Achat *{s['ticker']}* `${s.get('prix', 0):.4f}` score `{s['score']:+.2f}`")

    # Construction du message
    msg_lines = [
        f"📊 *Rapport {now.strftime('%d/%m %H:%M')} UTC*\n",
        f"💼 Portfolio : `${portfolio_final:.2f}`",
        f"💵 USDC libre : `${usdc_final:.2f}`\n",
    ]

    if pos_lines:
        msg_lines.append("*Positions :*")
        msg_lines.extend(pos_lines)
        msg_lines.append("")

    if decisions_lines:
        msg_lines.append("*Décisions :*")
        msg_lines.extend(decisions_lines)
    else:
        msg_lines.append("*Décisions :* aucune action — positions maintenues")

    _send_final("\n".join(msg_lines))
    print("Résumé Telegram envoyé.")

except Exception as e:
    print(f"Erreur résumé final : {e}")
