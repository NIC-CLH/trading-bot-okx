"""
Scanner d'alertes temps-réel — tourne toutes les 30 min.
N'exécute AUCUN ordre — silencieux par défaut.

RÈGLE ANTI-SPAM : aucune notification si rien d'actionnable.
Une alerte est envoyée UNIQUEMENT si :
  1. Signal technique EXTRÊME détecté (score >= 2.5 ou <= -2.5) — rare
  2. Breaking news majeure (votes CryptoPanic >= 20, age <= 45min)
  3. XRP : uniquement si score FORT (>= 2.0 BUY ou <= -1.0 SELL)
     ou si le prix a bougé de plus de 4% dans les 2 dernières heures

Note sur le cooldown : GitHub Actions repart de zéro à chaque run.
Le cache in-memory ne persiste PAS entre les runs.
On utilise donc des seuils stricts (score fort) plutôt que des cooldowns.
"""

import logging
import os
from datetime import datetime, timezone

import requests

import okx_client as okx
import technical_signals as ts

logger = logging.getLogger(__name__)

# ── Seuils ────────────────────────────────────────────────────────────────────
SIGNAL_ALERT_THRESHOLD = 2.5      # Score très fort → alerte tous tickers
NEWS_VOTES_THRESHOLD   = 20       # Votes CryptoPanic minimum
NEWS_MAX_AGE_MINUTES   = 45       # Ignorer les news trop vieilles

# XRP : seuils stricts — pas d'alerte en zone neutre
XRP_BUY_THRESHOLD  =  2.0   # >= 2.0 → "ACHETER / RENFORCER"
XRP_SELL_THRESHOLD = -1.0   # <= -1.0 → "ALLÉGER / VENDRE"
XRP_PRICE_MOVE_PCT =  4.0   # Alerte aussi si prix bouge > 4% sur la session

WATCH_TICKERS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "AVAX", "LINK", "NEAR", "INJ", "TIA",
    "ARB", "OP", "SUI", "APT", "AAVE",
    "DOT", "ATOM", "UNI", "DYDX", "ENA",
]

# Cache in-run uniquement (évite d'envoyer 2x la même alerte dans un même run)
_sent_this_run: set = set()


def _send_telegram(msg: str):
    """Envoie une notification Telegram."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram non configuré")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram send error: {e}")


# ── 1. Scan signaux techniques extrêmes ───────────────────────────────────────

def scan_extreme_signals() -> list[dict]:
    """
    Scanne les signaux très forts (score >= 2.5 ou <= -2.5).
    Seuil élevé intentionnellement — ces alertes doivent être rares.
    """
    alerts = []

    ohlcv = okx.get_all_ohlcv(WATCH_TICKERS, days=60)
    tech_results = ts.run(ohlcv)

    for ticker, tech in tech_results.items():
        if "erreur" in tech:
            continue

        score = tech.get("signal", {}).get("score", 0)
        if abs(score) < SIGNAL_ALERT_THRESHOLD:
            continue

        # Éviter le doublon avec l'alerte XRP dédiée
        if ticker == "XRP":
            continue

        # Éviter d'envoyer deux fois dans le même run
        cache_key = f"extreme_{ticker}"
        if cache_key in _sent_this_run:
            continue

        prix = tech.get("prix_actuel", 0)
        verdict = tech.get("signal", {}).get("verdict", "")
        stop = tech.get("stop_proche")
        target = tech.get("target_proche")
        atr = tech.get("atr_14", 0)

        if not stop and atr:
            stop = round(prix - atr * 2, 6)
        if not target and stop:
            target = round(prix + abs(prix - stop) * 2, 6)

        rr = abs(target - prix) / abs(prix - stop) if (stop and target and stop != prix) else 2.0
        direction = "📈 OPPORTUNITÉ HAUSSIÈRE" if score > 0 else "📉 SIGNAL BAISSIER"
        intensite = "🔥🔥🔥" if abs(score) >= 2.8 else "🔥🔥"

        signaux = tech.get("signal", {}).get("signaux", [])[:3]
        signaux_txt = "\n".join(f"  • {s}" for s in signaux) if signaux else ""

        msg = (
            f"{intensite} *SIGNAL FORT — {ticker}*\n"
            f"{direction}\n\n"
            f"💰 Prix : `${prix:.4f}`\n"
            f"📊 Score : `{score:+.2f} / 3.0` — {verdict}\n\n"
            f"🛡 Stop : `${stop:.4f}`\n"
            f"🎯 Target : `${target:.4f}`\n"
            f"⚖️ R/R : `{rr:.1f}:1`\n\n"
            f"{signaux_txt}"
        )

        alerts.append({"ticker": ticker, "score": score})
        _send_telegram(msg)
        _sent_this_run.add(cache_key)
        logger.info(f"Alerte signal extrême : {ticker} score={score:+.2f}")

    return alerts


# ── 2. Breaking news CryptoPanic ──────────────────────────────────────────────

def scan_breaking_news() -> list[dict]:
    """Vérifie CryptoPanic pour les news très votées dans les 45 dernières minutes."""
    alerts = []
    now = datetime.now(timezone.utc)

    try:
        resp = requests.get(
            "https://cryptopanic.com/api/free/v1/posts/",
            params={
                "auth_token": os.getenv("CRYPTOPANIC_TOKEN", ""),
                "filter": "important",
                "public": "true",
                "kind": "news",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return []

        posts = resp.json().get("results", [])

        for post in posts[:20]:
            # Vérifier l'âge
            published_at = post.get("published_at", "")
            try:
                pub_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                age_minutes = (now - pub_dt).total_seconds() / 60
                if age_minutes > NEWS_MAX_AGE_MINUTES:
                    continue
            except Exception:
                continue

            # Votes
            votes = post.get("votes", {})
            total_votes = votes.get("liked", 0) + votes.get("important", 0)
            if total_votes < NEWS_VOTES_THRESHOLD:
                continue

            title = post.get("title", "")
            url = post.get("url", "")
            currencies = [c.get("code", "") for c in post.get("currencies", [])]
            currencies_txt = ", ".join(currencies) if currencies else "Marché général"

            # Sentiment basique
            neg_words = ["hack", "exploit", "ban", "crash", "scam", "sec", "lawsuit", "collapse"]
            pos_words = ["etf", "approval", "launch", "partnership", "institutional", "adoption", "upgrade"]
            title_lower = title.lower()
            is_negative = any(w in title_lower for w in neg_words)
            is_positive = any(w in title_lower for w in pos_words)
            emoji = "🚨" if is_negative else "🚀" if is_positive else "📰"

            # Éviter le doublon dans le même run
            cache_key = f"news_{hash(title) % 100000}"
            if cache_key in _sent_this_run:
                continue

            msg = f"""{emoji} *BREAKING NEWS CRYPTO*

{title}

🏷 Actifs concernés : `{currencies_txt}`
👍 Votes : `{total_votes}`
🕐 Il y a `{int(age_minutes)}` minutes

🔗 [Lire l'article]({url})

_Analyse l'impact sur ton portefeuille et agis si nécessaire._"""

            alerts.append({"title": title, "votes": total_votes})
            _send_telegram(msg)
            _sent_this_run.add(cache_key)
            logger.info(f"Breaking news alertée : {title[:60]} ({total_votes} votes)")

    except Exception as e:
        logger.error(f"Erreur CryptoPanic : {e}")

    return alerts


# ── 3. Suivi XRP Binance ──────────────────────────────────────────────────────

def scan_xrp_binance():
    """
    Analyse XRP et envoie une alerte UNIQUEMENT si quelque chose d'actionnable :
      - Score >= 2.0 (signal d'achat fort) → BUY
      - Score <= -1.0 (signal de vente) → ALLÉGER ou VENDRE
      - Prix a bougé de plus de 4% depuis l'ouverture journalière (mouvement brusque)

    SILENCE si score entre -1.0 et +2.0 (zone d'attente) — rien à faire.
    """
    try:
        ohlcv = okx.get_all_ohlcv(["XRP"], days=60)
        tech_results = ts.run(ohlcv)
        tech = tech_results.get("XRP", {})

        if not tech or "erreur" in tech:
            return None

        sig = tech.get("signal", {})
        score = sig.get("score", 0)
        verdict = sig.get("verdict", "")
        prix = tech.get("prix_actuel", 0)
        stop = tech.get("stop_proche")
        target = tech.get("target_proche")
        atr = tech.get("atr_14", 0)

        if not stop and atr:
            stop = round(prix - atr * 2, 6)
        if not target and stop:
            target = round(prix + abs(prix - stop) * 2, 6)

        # ── Détection mouvement de prix brusque (intraday) ─────────────────
        price_move_pct = 0.0
        is_price_spike = False
        try:
            df_xrp = ohlcv.get("XRP")
            if df_xrp is not None and len(df_xrp) >= 2:
                open_price = float(df_xrp["open"].iloc[-1])   # Ouverture bougie actuelle
                if open_price > 0:
                    price_move_pct = (prix - open_price) / open_price * 100
                    is_price_spike = abs(price_move_pct) >= XRP_PRICE_MOVE_PCT
        except Exception:
            pass

        # ── Décision d'envoyer ou non ───────────────────────────────────────
        is_buy_signal  = score >= XRP_BUY_THRESHOLD    # >= +2.0
        is_sell_signal = score <= XRP_SELL_THRESHOLD   # <= -1.0
        actionnable = is_buy_signal or is_sell_signal or is_price_spike

        if not actionnable:
            logger.info(
                f"XRP : score={score:+.2f} | prix_move={price_move_pct:+.1f}% "
                f"→ zone neutre, pas d'alerte"
            )
            return None

        # ── Construction du message ──────────────────────────────────────────
        if is_buy_signal:
            action = "🟢 ACHETER / RENFORCER"
            conseil = "Signal technique fort — opportunité d'entrée sur Binance"
            rachat_txt = ""
        elif score >= -0.5:  # Légèrement négatif mais spike de prix
            action = "⚠️ MOUVEMENT BRUSQUE — SURVEILLER"
            conseil = f"Prix a bougé de {price_move_pct:+.1f}% sur la bougie — score encore neutre"
            rachat_txt = f"\n💡 Pas encore de signal clair — attends confirmation"
        elif score >= -1.0 or (is_price_spike and not is_sell_signal):
            action = "🟠 ALLÉGER 30-50%"
            conseil = "Signal qui se dégrade — sécurise une partie de ta position"
            rachat_txt = (
                f"\n♻️ *Zone de rachat* : `${stop:.4f}` — `${round(prix * 0.97, 4):.4f}`"
                f"\n   Tu recevras une alerte 🟢 quand le score remonte au-dessus de +2.0"
            )
        else:
            action = "🔴 VENDRE — SORTIR"
            conseil = "Signal clairement baissier — coupe ta position XRP sur Binance"
            rachat_txt = (
                f"\n♻️ *Racheter si* : score XRP remonte > +2.0"
                f"\n   ET prix au-dessus de `${round(prix * 1.05, 4):.4f}`"
                f"\n   Tu recevras une alerte automatique"
            )

        rr = abs(target - prix) / abs(prix - stop) if (stop and target and stop != prix) else 0
        signaux = sig.get("signaux", [])[:3]
        signaux_txt = "\n".join(f"  • {s}" for s in signaux) if signaux else ""

        spike_txt = f"\n⚡ Mouvement intraday : `{price_move_pct:+.1f}%`" if is_price_spike else ""

        msg = (
            f"📊 *XRP — Alerte Binance*\n\n"
            f"{action}\n"
            f"_{conseil}_\n\n"
            f"💰 Prix : `${prix:.4f}`{spike_txt}\n"
            f"📈 Score : `{score:+.2f} / 3.0` — {verdict}\n\n"
            f"🛡 Stop : `${stop:.4f}`\n"
            f"🎯 Target : `${target:.4f}`\n"
            f"⚖️ R/R : `{rr:.1f}:1`\n"
            f"{rachat_txt}\n\n"
            f"{signaux_txt}\n"
            f"_Action manuelle sur Binance_"
        )

        _send_telegram(msg)
        logger.info(f"XRP alerte envoyée : score={score:+.2f} → {action}")
        return {"score": score, "action": action, "prix": prix}

    except Exception as e:
        logger.error(f"Erreur analyse XRP : {e}")
        return None


# ── Point d'entrée ─────────────────────────────────────────────────────────────

def run():
    now = datetime.now(timezone.utc)
    logger.info(f"Alert scanner démarré — {now.strftime('%d/%m/%Y %H:%M UTC')}")

    signal_alerts = scan_extreme_signals()
    news_alerts = scan_breaking_news()
    xrp_update = scan_xrp_binance()

    total = len(signal_alerts) + len(news_alerts)
    logger.info(f"Alert scanner terminé — {total} alerte(s) envoyée(s)")

    if total == 0 and not xrp_update:
        logger.info("Aucune alerte — marché calme")

    return {"signals": signal_alerts, "news": news_alerts, "xrp": xrp_update}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run()
