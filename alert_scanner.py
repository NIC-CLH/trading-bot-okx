"""
Scanner d'alertes temps-réel — tourne toutes les 30 min.
N'exécute AUCUN ordre — envoie uniquement des alertes Telegram si :
  1. Signal technique extrême détecté (score_tech >= 2.5 ou <= -2.5)
  2. Breaking news avec fort impact (votes élevés sur CryptoPanic)

Séparé du run_once.py pour ne pas surcharger le cycle principal.
"""

import logging
import os
import time
from datetime import datetime, timezone

import requests

import okx_client as okx
import technical_signals as ts

logger = logging.getLogger(__name__)

# ── Seuils ────────────────────────────────────────────────────────────────────
SIGNAL_ALERT_THRESHOLD = 2.5      # Score technique très fort (sur 3.0 max)
NEWS_VOTES_THRESHOLD   = 20       # Nb de votes CryptoPanic pour breaking news
NEWS_MAX_AGE_MINUTES   = 45       # Ignorer les news plus vieilles que ça

# Actifs surveillés en priorité (les plus liquides + watchlist)
WATCH_TICKERS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "AVAX", "LINK", "NEAR", "INJ", "TIA",
    "ARB", "OP", "SUI", "APT", "AAVE",
    "DOT", "ATOM", "UNI", "DYDX", "ENA",
]

# Cooldown anti-spam par ticker (en secondes)
_alert_cache: dict[str, float] = {}
COOLDOWN_SECONDS = 4 * 3600  # 4h entre deux alertes pour le même ticker


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


def _is_cooldown(ticker: str) -> bool:
    last = _alert_cache.get(ticker, 0)
    return (time.time() - last) < COOLDOWN_SECONDS


def _set_cooldown(ticker: str):
    _alert_cache[ticker] = time.time()


# ── 1. Scan signaux techniques extrêmes ───────────────────────────────────────

def scan_extreme_signals() -> list[dict]:
    """Scanne les signaux techniques très forts sur les tickers prioritaires."""
    alerts = []

    ohlcv = okx.get_all_ohlcv(WATCH_TICKERS, days=60)
    tech_results = ts.run(ohlcv)

    for ticker, tech in tech_results.items():
        if "erreur" in tech:
            continue

        score = tech.get("signal", {}).get("score", 0)
        if abs(score) < SIGNAL_ALERT_THRESHOLD:
            continue

        if _is_cooldown(ticker):
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
        intensite = "🔥🔥🔥" if abs(score) >= 2.8 else "🔥🔥" if abs(score) >= 2.5 else "🔥"

        signaux = tech.get("signal", {}).get("signaux", [])[:4]
        signaux_txt = "\n".join(f"  • {s}" for s in signaux) if signaux else ""

        msg = f"""{intensite} *SIGNAL FORT — {ticker}*
{direction}

💰 Prix : `${prix:.4f}`
📊 Score technique : `{score:+.2f} / 3.0`
📝 {verdict}

🛡 Stop-loss : `${stop:.4f}`
🎯 Objectif : `${target:.4f}`
⚖️ R/R : `{rr:.1f}:1`

*Signaux détectés :*
{signaux_txt}

_Ce cycle de scan ne trade pas automatiquement — le prochain cycle 4h évaluera l'ordre._"""

        alerts.append({"ticker": ticker, "score": score, "msg": msg})
        _send_telegram(msg)
        _set_cooldown(ticker)
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

            # Cooldown par titre (évite les doublons)
            cache_key = f"news_{hash(title) % 100000}"
            if _is_cooldown(cache_key):
                continue

            msg = f"""{emoji} *BREAKING NEWS CRYPTO*

{title}

🏷 Actifs concernés : `{currencies_txt}`
👍 Votes : `{total_votes}`
🕐 Il y a `{int(age_minutes)}` minutes

🔗 [Lire l'article]({url})

_Analyse l'impact sur ton portefeuille et agis si nécessaire._"""

            alerts.append({"title": title, "votes": total_votes, "msg": msg})
            _send_telegram(msg)
            _set_cooldown(cache_key)
            logger.info(f"Breaking news alertée : {title[:60]} ({total_votes} votes)")

    except Exception as e:
        logger.error(f"Erreur CryptoPanic : {e}")

    return alerts


# ── 3. Suivi XRP Binance ──────────────────────────────────────────────────────

def scan_xrp_binance():
    """
    Analyse XRP toutes les 30 min et envoie une recommandation claire
    pour la position manuelle sur Binance.
    Envoyé uniquement si le signal a changé ou est fort (évite le spam).
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

        # Recommandation selon le score + instructions de rachat
        rachat_txt = ""
        if score >= 2.0:
            action = "🟢 ACHETER / RENFORCER"
            conseil = "Signal très fort — opportunité d'entrée sur Binance"
            rachat_txt = ""
        elif score >= 1.0:
            action = "🟢 CONSERVER / RACHETER"
            conseil = "Momentum positif — si tu avais allégé, c'est le moment de reprendre"
            rachat_txt = (
                f"\n💡 *Niveau de rachat* : autour de `${prix:.4f}`"
                f"\n   Stop si ça repasse sous `${stop:.4f}`"
            )
        elif score >= -0.5:
            action = "🟡 ATTENDRE"
            conseil = "Signal neutre — ne rien faire, attends une direction claire"
            rachat_txt = (
                f"\n💡 *Racheter si* : score repasse au-dessus de +1.0"
                f"\n   Surveille le niveau `${target:.4f}` comme résistance clé"
            )
        elif score >= -1.0:
            action = "🟠 ALLÉGER 30-50%"
            conseil = "Signal qui se dégrade — sécurise une partie"
            rachat_txt = (
                f"\n♻️ *Quand racheter* : attends que le score repasse > +1.0"
                f"\n   Zone de rachat visée : `${stop:.4f}` — `${round(prix * 0.97, 4):.4f}`"
                f"\n   Tu recevras une alerte 🟢 dès que le signal se retourne"
            )
        else:
            action = "🔴 VENDRE"
            conseil = "Signal clairement baissier — coupe ta position"
            rachat_txt = (
                f"\n♻️ *Quand racheter* : attends un signal > +1.5 ET que le prix"
                f" repasse au-dessus de `${round(prix * 1.05, 4):.4f}`"
                f"\n   Tu recevras une alerte 🟢 automatiquement"
            )

        # N'envoie que si le signal change de zone (évite spam toutes les 30min)
        cache_key = f"xrp_zone_{int(score * 2)}"  # Change si score change de 0.5
        if _is_cooldown(cache_key) and abs(score) < 1.5:
            return None

        rr = abs(target - prix) / abs(prix - stop) if (stop and target and stop != prix) else 0
        signaux = sig.get("signaux", [])[:3]
        signaux_txt = "\n".join(f"  • {s}" for s in signaux) if signaux else ""

        msg = f"""📊 *XRP — Suivi Binance*

{action}
_{conseil}_

💰 Prix actuel : `${prix:.4f}`
📈 Score : `{score:+.2f} / 3.0` — {verdict}

🛡 Zone de stop : `${stop:.4f}`
🎯 Objectif : `${target:.4f}`
⚖️ R/R : `{rr:.1f}:1`
{rachat_txt}

{signaux_txt}

_Action manuelle sur Binance — alerte automatique au prochain retournement_"""

        _send_telegram(msg)
        _set_cooldown(cache_key)
        logger.info(f"XRP Binance update : score={score:+.2f} → {action}")
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
