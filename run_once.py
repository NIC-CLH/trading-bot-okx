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

logger = logging.getLogger(__name__)

import okx_client as okx
import technical_signals as ts
import position_manager as pm
import scanner
import alertes
import ruflo_memory as rm

# Mémoire d'apprentissage : charger l'historique JSON dans ruflo au démarrage
try:
    rm.seed_ruflo_from_json()
except Exception as _e:
    logger.warning(f"ruflo seed ignoré : {_e}")

# Supprime les alertes intermédiaires bruyantes — on envoie un résumé à la fin
# EXCEPTION : alertes.send reste actif pour les ventes urgentes (stop loss)
# et les alertes de danger de position_manager
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

# ── Étape 1 : Gestion des positions existantes ───────────────────────────
# Sorties fixes uniquement : -7% stop / +12% objectif / 7 jours
print("─── GESTION POSITIONS ───")
pm_result = {"actions": []}  # valeur par défaut si pm.run() lève une exception
try:
    pm_result = pm.run(portfolio_value)
    actions = pm_result.get("actions", [])
    if actions:
        print(f"{len(actions)} action(s) exécutée(s) sur les positions")
    else:
        print("Positions maintenues (stops/objectifs non atteints)")
except Exception as e:
    print(f"Erreur gestion positions : {e}")

# Attendre que les ordres de vente soient settlés sur OKX
# (les ordres market prennent 2-5s pour apparaître dans les balances)
import time as _time
_time.sleep(5)

# ── Étape 2 : Recalculer balance + valeur totale après ventes ────────────
try:
    balances = okx.get_balances()
    usdc = balances.get("USDC", 0) + balances.get("USDT", 0)
    nb_positions = len([k for k in balances if k not in ("USDC", "USDT")])
    positions_apres = {k: v for k, v in balances.items() if k not in ("USDC", "USDT")}
    # Recalcul du portfolio total (positions liquidées → USDC monté)
    portfolio_value = usdc + sum(
        (okx.get_price_usdc(t) or 0) * q for t, q in positions_apres.items()
    )
    print(f"Portfolio mis à jour : ${portfolio_value:.2f} (USDC libre : ${usdc:.2f})")
except Exception:
    pass

# ── Étape 3 : Scan nouvelles opportunités ────────────────────────────────
# On passe la valeur TOTALE du portefeuille (positions + USDC), pas seulement USDC.
# Cela garantit que la taille des trades est proportionnelle à la richesse réelle,
# plafonnée par le USDC effectivement disponible dans execution.py.
print("\n─── SCAN OPPORTUNITÉS ───")
signals = []
try:
    signals = scanner.run_scan(portfolio_value=portfolio_value)
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
    # Utiliser get_open_positions() pour avoir exactement les mêmes positions
    # que le gestionnaire voit : filtre poussières < $3, P&L calculé correctement.
    positions_obj = pm.get_open_positions()

    balances_final = okx.get_balances()
    usdc_final = balances_final.get("USDC", 0) + balances_final.get("USDT", 0)

    # Valeur totale = USDC + positions filtrées (pas les poussières)
    portfolio_final = usdc_final + sum(p["valeur_usd"] for p in positions_obj)

    pos_lines = []
    for p in positions_obj:
        ticker = p["ticker"]
        prix   = p["prix_actuel"]
        pnl    = p.get("pnl_pct")
        days   = f" {p['days_held']:.0f}j" if p.get("days_held") else ""
        if pnl is not None:
            pnl_emoji = "🟢" if pnl >= 0 else "🔴"
            pos_lines.append(f"{pnl_emoji} *{ticker}* `${prix:.4f}`{days} — P&L `{pnl:+.1f}%`")
        else:
            pos_lines.append(f"⚪ *{ticker}* `${prix:.4f}`{days}")

    # Décisions prises ce cycle
    actions_taken = pm_result.get("actions", [])
    decisions_lines = []
    for a in actions_taken:
        valeur_str = f"`${a['valeur']:.2f}`" if a.get("valeur") else ""
        pnl_str = f"P&L `{a['pnl_pct']:+.1f}%`" if a.get("pnl_pct") is not None else ""
        if a["decision"] == "FULL_SELL":
            decisions_lines.append(f"💰 Vente *{a['ticker']}* {valeur_str} {pnl_str} — {a['raison']}")
        elif a["decision"] == "PARTIAL_SELL":
            decisions_lines.append(f"🟡 Vente partielle *{a['ticker']}* 50% {valeur_str} {pnl_str} — {a['raison']}")

    # Nouveaux achats — uniquement si l'ordre a vraiment été exécuté
    # (signals retourne seulement les ordres confirmés par OKX)
    signals_taken = [s for s in signals if s.get("ordre_execute", False)]
    for s in signals_taken:
        decisions_lines.append(
            f"🛒 Achat *{s['ticker']}* `${s.get('taille_allouee', s.get('taille_usd', 0)):.0f}` "
            f"@ `${s.get('prix', 0):.4f}` — score `{s['score']:+.2f}`"
        )

    # ── Envoyer uniquement si quelque chose s'est passé OU rapport 7h ──────
    has_activity = bool(decisions_lines)
    is_daily_report = (now.hour == 7)

    if has_activity or is_daily_report:
        msg_lines = [
            f"📊 *{'Rapport quotidien' if is_daily_report else 'Cycle'} {now.strftime('%d/%m %H:%M')} UTC*\n",
            f"💼 Portfolio : `${portfolio_final:.2f}`",
            f"💵 USDC libre : `${usdc_final:.2f}`\n",
        ]

        if pos_lines:
            msg_lines.append("*Positions :*")
            msg_lines.extend(pos_lines)
            msg_lines.append("")

        if decisions_lines:
            msg_lines.append("*Actions exécutées :*")
            msg_lines.extend(decisions_lines)
        elif is_daily_report:
            msg_lines.append("_Aucune action ce cycle — positions maintenues_")

        _send_final("\n".join(msg_lines))
        print("Résumé Telegram envoyé.")
    else:
        print("Aucune activité ce cycle — pas de notification Telegram.")

except Exception as e:
    print(f"Erreur résumé final : {e}")
