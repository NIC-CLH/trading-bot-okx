"""
Module Alertes Telegram — envoie des notifications formatées.
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send(message: str, parse_mode: str = "Markdown") -> bool:
    """Envoie un message Telegram. Retourne True si succès."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant dans .env")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": parse_mode},
            timeout=10,
        )
        return resp.json().get("ok", False)
    except Exception as e:
        logger.error(f"Erreur Telegram : {e}")
        return False


def alerte_opportunite(signal: dict):
    """Formate et envoie une alerte d'opportunité d'achat/vente."""
    ticker = signal["ticker"]
    score = signal["score"]
    verdict = signal["verdict"]
    prix = signal["prix"]
    stop = signal.get("stop")
    target = signal.get("target")
    rr = signal.get("rr")
    taille_usd = signal.get("taille_usd")
    raisons = signal.get("raisons", [])
    source = signal.get("source", "Binance")

    direction = "📈 ACHAT" if score > 0 else "📉 VENTE/RÉDUCTION"
    etoiles = "⭐" * min(int(abs(score)), 3)

    msg = f"""🚨 *SIGNAL {direction}* {etoiles}

*{ticker}* — {verdict}
Prix : `${prix:,.4f}`
Source : {source}

📊 *Analyse :*
"""
    for r in raisons:
        msg += f"• {r}\n"

    if stop and target:
        msg += f"""
🛡 Stop-loss : `${stop:,.4f}`
🎯 Target : `${target:,.4f}`
⚖️ R/R : {rr:.1f}:1
"""

    if taille_usd:
        msg += f"💰 Taille suggérée (20% max) : `${taille_usd:,.0f}`\n"

    msg += f"\nScore : {score:+.1f}/3 | _Pas un conseil en investissement_"

    send(msg)


def alerte_opportunite_enrichie(signal: dict):
    """Alerte 4 dimensions : technique + microstructure + on-chain + fondamental."""
    # Silencer les signaux bloqués — seuls les ordres actionnables méritent une notif
    if not signal.get("trade_autorise", False):
        return
    ticker = signal["ticker"]
    score = signal["score"]
    score_tech = signal.get("score_tech", score)
    score_news = signal.get("score_news", 0)
    score_ms = signal.get("score_ms", 0)
    score_oc = signal.get("score_oc", 0)
    verdict = signal.get("verdict", "")
    verdict_news = signal.get("verdict_news", "")
    verdict_ms = signal.get("verdict_ms", "")
    verdict_oc = signal.get("verdict_oc", "")
    prix = signal["prix"]
    stop = signal.get("stop")
    target = signal.get("target")
    rr = signal.get("rr", 2.0)
    taille_usd = signal.get("taille_usd")
    raisons = signal.get("raisons", [])
    ms_signals = signal.get("ms_signals", [])
    oc_signals = signal.get("oc_signals", [])
    news_signals = signal.get("news_signals", [])
    fg = signal.get("fear_greed", {})
    ichi = signal.get("ichimoku", {})
    fib = signal.get("fibonacci", {})
    funding = signal.get("funding_rate")
    source = signal.get("source", "OKX")

    direction = "📈 ACHAT" if score > 0 else "📉 VENTE"
    etoiles = "⭐" * min(int(abs(score)), 3)

    # Score bar visuel
    bar_filled = min(int(abs(score) / 3 * 10), 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    msg = f"""🚨 *SIGNAL {direction}* {etoiles}

*{ticker}* @ `${prix:,.4f}` | {source}
Score : `{bar}` {score:+.2f}/3

📊 *Technique* `{score_tech:+.2f}` — {verdict}
"""
    for r in raisons[:3]:
        msg += f"• {r}\n"

    # Ichimoku + Fibonacci si disponibles
    if ichi.get("above_cloud") is not None:
        cloud_emoji = "☁️✅" if ichi.get("above_cloud") else ("☁️⚠️" if ichi.get("below_cloud") else "☁️➡️")
        msg += f"• Ichimoku {cloud_emoji} ({'nuage haussier' if ichi.get('bullish_cloud') else 'nuage baissier'})\n"
    if fib.get("nearest"):
        msg += f"• Fibonacci {fib['nearest']} (à {fib.get('distance_pct', 0):.1f}% du niveau)\n"

    msg += f"\n🔬 *Microstructure* `{score_ms:+.2f}` — {verdict_ms}\n"
    for s in ms_signals[:2]:
        msg += f"• {s}\n"
    if funding is not None:
        msg += f"• Funding rate : `{funding:+.3f}%`\n"

    msg += f"\n⛓ *On-Chain* `{score_oc:+.2f}` — {verdict_oc}\n"
    for s in oc_signals[:2]:
        msg += f"• {s}\n"

    msg += f"\n📰 *Fondamentaux* `{score_news:+.2f}` — {verdict_news}\n"
    for n in news_signals[:2]:
        msg += f"• {n}\n"
    if fg.get("value") is not None:
        fg_emoji = "😱" if fg["value"] <= 25 else ("😰" if fg["value"] <= 40 else ("😄" if fg["value"] >= 70 else "😐"))
        msg += f"• {fg_emoji} Fear & Greed : `{fg['value']}` ({fg.get('label', '')})\n"

    if stop and target:
        msg += f"""
🛡 Stop : `${stop:,.4f}`
🎯 Target : `${target:,.4f}`
⚖️ R/R : `{rr:.1f}:1`
"""
    if taille_usd:
        msg += f"💰 Taille : `${taille_usd:,.0f}` (20% budget)\n"

    trade_ok = signal.get("trade_autorise", False)
    status = "✅ *Ordre automatique placé*" if trade_ok and score >= 2.0 else "⏳ *Signal — vérifier avant d'agir*"
    msg += f"\n{status}\n_Pas un conseil en investissement_"

    send(msg)


def alerte_portefeuille(snapshot: dict):
    """Résumé quotidien du portefeuille."""
    total = snapshot["valeur_totale_usd"]
    pnl = snapshot["pnl_total_absolu"]
    pnl_pct = snapshot["pnl_total_pct"]
    stable_pct = snapshot["stablecoin_pct"]

    signe = "+" if pnl >= 0 else ""
    emoji = "🟢" if pnl >= 0 else "🔴"

    msg = f"""{emoji} *Rapport Portefeuille*

💼 Valeur totale : `${total:,.2f}`
📈 P&L : `{signe}${pnl:,.2f}` ({signe}{pnl_pct:.2f}%)
💵 Stablecoins : {stable_pct:.1f}%

*Positions :*
"""
    for pos in snapshot["positions"]:
        if not pos["valeur_actuelle"]:
            continue
        pnl_p = pos.get("pnl_pct")
        pnl_str = f"{pnl_p:+.1f}%" if pnl_p is not None else "N/A"
        emoji_p = "🟢" if (pnl_p or 0) >= 0 else "🔴"
        msg += f"{emoji_p} {pos['ticker']} — `${pos['valeur_actuelle']:,.2f}` ({pnl_str})\n"

    alertes = snapshot.get("alertes_concentration", [])
    if alertes:
        msg += "\n⚠️ *Alertes :*\n"
        for a in alertes:
            msg += f"• {a}\n"

    send(msg)


def alerte_risque(alertes: list[str]):
    """Envoie les alertes de risque critiques."""
    if not alertes:
        return
    msg = "⚠️ *Alertes Risque*\n\n"
    for a in alertes:
        msg += f"• {a}\n"
    send(msg)
