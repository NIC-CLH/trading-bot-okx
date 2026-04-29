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
import news_sentiment as ns
import execution

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


# ── 1. Scan + exécution automatique des signaux forts ────────────────────────

def scan_and_execute_signals() -> list[dict]:
    """
    Scanne les signaux forts (score >= 2.5) et EXÉCUTE directement les trades.
    Pas d'alerte inutile — le bot agit, et tu reçois une confirmation d'ordre.

    Vérifications avant exécution :
    - Pas de position déjà ouverte sur ce ticker
    - Fondamentaux pas franchement négatifs (Fear & Greed rapide)
    - Budget USDC suffisant (géré par execution.execute_signal)
    """
    executed = []

    ohlcv = okx.get_all_ohlcv(WATCH_TICKERS, days=60)
    tech_results = ts.run(ohlcv)

    # Récupérer les positions actuelles (pour éviter de doubler)
    try:
        balances = okx.get_balances()
        stables = {"USDC", "USDT", "BUSD", "DAI"}
        open_positions = {t for t, q in balances.items() if t not in stables and q > 0}
        usdc_available = balances.get("USDC", 0) + balances.get("USDT", 0)
        portfolio_value = usdc_available + sum(
            (okx.get_price_usdc(t) or 0) * q
            for t, q in balances.items() if t not in stables
        )
    except Exception as e:
        logger.error(f"Erreur balance OKX : {e}")
        return []

    # Fear & Greed global — vérification rapide une seule fois
    try:
        fg_resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=6)
        fg_value = int(fg_resp.json()["data"][0]["value"])
        fg_extreme_greed = fg_value >= 85
        fg_extreme_fear  = fg_value <= 15
    except Exception:
        fg_value = 50
        fg_extreme_greed = fg_extreme_fear = False

    # Toujours calculer les scores des positions ouvertes dès le départ.
    # Même si on a assez de USDC maintenant, on peut en manquer après
    # le premier trade — la rotation doit rester possible à tout moment.
    positions_scores = {}
    positions_pnl = {}
    if open_positions:
        try:
            pos_ohlcv = okx.get_all_ohlcv(list(open_positions), days=60)
            pos_tech = ts.run(pos_ohlcv)
            for pos_ticker in open_positions:
                pt = pos_tech.get(pos_ticker, {})
                positions_scores[pos_ticker] = pt.get("signal", {}).get("score", 0)
                try:
                    from position_manager import _get_entry_price
                    entry = _get_entry_price(pos_ticker)
                    prix_actuel = okx.get_price_usdc(pos_ticker) or 0
                    qty = balances.get(pos_ticker, 0)
                    pnl = (prix_actuel - entry) / entry * 100 if entry and entry > 0 else 0
                    positions_pnl[pos_ticker] = {"pnl": pnl, "qty": qty, "prix": prix_actuel}
                except Exception:
                    positions_pnl[pos_ticker] = {"pnl": 0, "qty": 0, "prix": 0}
            logger.info(f"Scores positions : { {t: f'{s:+.2f}' for t, s in positions_scores.items()} }")
        except Exception as e:
            logger.warning(f"Erreur calcul scores positions : {e}")

    for ticker, tech in tech_results.items():
        if "erreur" in tech:
            continue
        if ticker == "XRP":   # XRP est sur Binance, pas OKX
            continue

        score = tech.get("signal", {}).get("score", 0)
        if abs(score) < SIGNAL_ALERT_THRESHOLD:
            continue

        # Pas de doublon dans ce run
        cache_key = f"exec_{ticker}"
        if cache_key in _sent_this_run:
            continue

        # Pas de position déjà ouverte sur ce ticker
        if ticker in open_positions:
            logger.info(f"{ticker} : position déjà ouverte — trade ignoré")
            continue

        # Blocage si Fear & Greed extrême dans la mauvaise direction
        if score > 0 and fg_extreme_greed:
            logger.info(f"{ticker} : Fear&Greed {fg_value} (euphorie) — achat bloqué")
            continue
        if score < 0 and fg_extreme_fear:
            logger.info(f"{ticker} : Fear&Greed {fg_value} (panique) — vente bloquée")
            continue

        prix = tech.get("prix_actuel", 0)
        stop = tech.get("stop_proche")
        target = tech.get("target_proche")
        atr = tech.get("atr_14", 0)

        if not stop and atr:
            stop = round(prix - atr * 2, 6)
        if not target and stop:
            target = round(prix + abs(prix - stop) * 2, 6)

        if not stop or not target or not prix:
            continue

        # Vérifier R/R minimum (>= 1.5:1)
        rr = abs(target - prix) / abs(prix - stop) if stop != prix else 0
        if rr < 1.5:
            logger.info(f"{ticker} : R/R {rr:.1f} insuffisant — trade ignoré")
            continue

        # ── Rotation si pas assez de USDC ──────────────────────────────────
        # Si USDC < 10$, chercher la position la plus faible à vendre
        usdc_pour_trade = usdc_available
        rotation_faite = False

        if usdc_available < 10 and positions_scores:
            # Trouver la position la plus faible :
            # Priorité 1 : score le plus bas (position qui se dégrade)
            # Priorité 2 : P&L le plus négatif (position perdante)
            candidat_rotation = min(
                positions_scores.items(),
                key=lambda x: (x[1], positions_pnl.get(x[0], {}).get("pnl", 0))
            )
            pos_faible, score_faible = candidat_rotation
            pnl_faible = positions_pnl.get(pos_faible, {}).get("pnl", 0)

            # Ne vendre que si la position faible est clairement inférieure
            # au nouveau signal (écart de score >= 1.5 points)
            ecart_score = score - score_faible
            pos_perdante = pnl_faible < -3.0   # En perte de plus de 3%
            pos_neutre   = score_faible < 0.5  # Signal faible ou négatif

            if ecart_score >= 1.5 and (pos_perdante or pos_neutre):
                qty_vendre = positions_pnl[pos_faible]["qty"]
                prix_faible = positions_pnl[pos_faible]["prix"]
                valeur_faible = qty_vendre * prix_faible

                logger.info(
                    f"Rotation : vente {pos_faible} (score={score_faible:+.2f}, "
                    f"PnL={pnl_faible:+.1f}%) pour financer {ticker} (score={score:+.2f})"
                )

                try:
                    okx.place_order(
                        ticker=pos_faible,
                        side="sell",
                        quantity=qty_vendre * 0.999,
                        order_type="market",
                    )
                    # Notifier la rotation
                    pnl_emoji = "🟢" if pnl_faible >= 0 else "🔴"
                    _send_telegram(
                        f"🔄 *Rotation de position*\n\n"
                        f"{pnl_emoji} Vente *{pos_faible}* "
                        f"(score `{score_faible:+.2f}`, PnL `{pnl_faible:+.1f}%`)\n"
                        f"→ Pour financer *{ticker}* (score `{score:+.2f}`)\n\n"
                        f"_Le signal sur {ticker} est nettement supérieur_"
                    )
                    usdc_pour_trade = valeur_faible * 0.997  # Après frais estimés
                    rotation_faite = True
                    open_positions.discard(pos_faible)
                    # Laisser le temps à l'ordre de se remplir
                    import time; time.sleep(3)
                except Exception as e:
                    logger.error(f"Échec vente rotation {pos_faible} : {e}")
                    continue  # Si la vente échoue, on n'achète pas

            else:
                logger.info(
                    f"Pas de rotation : {pos_faible} score={score_faible:+.2f} "
                    f"PnL={pnl_faible:+.1f}% — pas assez dégradé vs {ticker}"
                )
                continue  # Pas de USDC et pas de rotation possible → skip

        # ── Exécution du trade ──────────────────────────────────────────────
        # Recalculer le portfolio_value après rotation éventuelle
        pv = portfolio_value if not rotation_faite else (
            portfolio_value - positions_pnl.get(
                min(positions_scores, key=lambda x: positions_scores[x], default=""),
                {}
            ).get("qty", 0) * positions_pnl.get(
                min(positions_scores, key=lambda x: positions_scores[x], default=""),
                {}
            ).get("prix", 0) + usdc_pour_trade
        )

        signal = {
            "ticker": ticker,
            "score": score,
            "score_tech": score,
            "score_news": 0.0,
            "prix": prix,
            "stop": stop,
            "target": target,
            "rr": rr,
            "taille_usd": pv * 0.20,
            "verdict_news": "Rotation" if rotation_faite else "Signal rapide 30min",
            "trade_autorise": True,
            "source": "OKX Scanner 30min",
        }

        logger.info(
            f"Exécution {'(après rotation) ' if rotation_faite else ''}"
            f"{ticker} score={score:+.2f} prix={prix:.4f}"
        )

        success = execution.execute_signal(signal, pv)
        if success:
            executed.append({"ticker": ticker, "score": score, "prix": prix, "rotation": rotation_faite})
            _sent_this_run.add(cache_key)
            # Mettre à jour le USDC disponible pour les trades suivants
            usdc_available = execution.get_usdt_balance()

    return executed


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


# ── Traduction des signaux techniques en français simple ─────────────────────

def _traduire_signaux(signaux_bruts: list) -> list:
    """Convertit les signaux techniques en phrases compréhensibles."""
    traductions = {
        "EMA 8>21>55>200 alignées haussière": "Toutes les tendances sont orientées à la hausse",
        "EMA 8<21<55<200 alignées baissière": "Toutes les tendances sont orientées à la baisse",
        "EMA partiellement haussière": "La tendance court terme est haussière",
        "Golden Cross EMA8/EMA21": "Signal d'achat déclenché sur le court terme",
        "Death Cross EMA8/EMA21": "Signal de vente déclenché sur le court terme",
        "RSI survendu": "XRP est trop bas techniquement — rebond probable",
        "RSI suracheté": "XRP est trop haut techniquement — prudence",
        "StochRSI survendu": "XRP est en zone de survente — potentiel rebond",
        "StochRSI suracheté": "XRP est en zone de surachat",
        "StochRSI croisement haussier": "Les indicateurs court terme viennent de se retourner à la hausse",
        "MACD croisement haussier": "Le momentum vient de basculer à la hausse",
        "MACD croisement baissier": "Le momentum vient de basculer à la baisse",
        "OBV haussier (accumulation)": "Les acheteurs sont actifs — le volume confirme la hausse",
        "Supertrend retournement HAUSSIER": "Signal d'entrée technique fort confirmé",
        "Supertrend retournement BAISSIER": "Signal de sortie technique confirmé",
        "Bollinger Squeeze": "Une forte variation de prix est imminente",
        "CVD divergence HAUSSIÈRE": "Des acheteurs importants accumulent discrètement",
        "CVD divergence BAISSIÈRE": "Des vendeurs importants se déchargent discrètement",
        "Donchian BREAKOUT haussier": "XRP vient de casser une résistance majeure",
        "Donchian BREAKDOWN baissier": "XRP vient de casser un support majeur",
        "Elder Ray BUY": "La dynamique acheteur est clairement dominante",
        "Elder Ray SELL": "La dynamique vendeur est clairement dominante",
        "ADX": "La tendance est forte et confirmée",
        "Volume": "Le volume confirme le mouvement",
        "Prix sous bande BB inférieure": "XRP est en survente extrême — rebond possible",
        "Prix au-dessus de la Value Area": "XRP est au-dessus de sa zone de valeur — breakout",
        "Prix en dessous de la Value Area": "XRP est sous sa zone de valeur — pression baissière",
    }

    resultat = []
    for s in signaux_bruts:
        traduit = None
        for cle, traduction in traductions.items():
            if cle.lower() in s.lower():
                traduit = traduction
                break
        if traduit and traduit not in resultat:
            resultat.append(traduit)
        elif not traduit:
            # Garder les signaux non reconnus mais nettoyer le jargon
            s_clean = s.replace("📈", "").replace("📉", "").replace("🌟", "").strip()
            # Supprimer les termes trop techniques
            mots_a_supprimer = ["EMA", "RSI", "MACD", "ADX", "OBV", "ATR", "BB", "SAR", "CCI"]
            if not any(m in s_clean for m in mots_a_supprimer):
                resultat.append(s_clean)

    return resultat[:3]


# ── 3. Suivi XRP Binance ──────────────────────────────────────────────────────

def scan_xrp_binance():
    """
    Analyse XRP et envoie une alerte UNIQUEMENT si quelque chose d'actionnable.
    Message en français simple — zéro jargon technique ni mention de "score".

    Déclenche uniquement si :
    - Signal d'achat fort (score >= 2.0)
    - Signal de vente (score <= -1.0)
    - Mouvement de prix brusque > 4% sur la journée
    """
    try:
        ohlcv = okx.get_all_ohlcv(["XRP"], days=60)
        tech_results = ts.run(ohlcv)
        tech = tech_results.get("XRP", {})

        if not tech or "erreur" in tech:
            return None

        sig = tech.get("signal", {})
        score = sig.get("score", 0)
        prix = tech.get("prix_actuel", 0)
        stop = tech.get("stop_proche")
        target = tech.get("target_proche")
        atr = tech.get("atr_14", 0)

        if not stop and atr:
            stop = round(prix - atr * 2, 6)
        if not target and stop:
            target = round(prix + abs(prix - stop) * 2, 6)

        # ── Détection mouvement de prix brusque ────────────────────────────
        price_move_pct = 0.0
        is_price_spike = False
        try:
            df_xrp = ohlcv.get("XRP")
            if df_xrp is not None and len(df_xrp) >= 2:
                open_price = float(df_xrp["open"].iloc[-1])
                if open_price > 0:
                    price_move_pct = (prix - open_price) / open_price * 100
                    is_price_spike = abs(price_move_pct) >= XRP_PRICE_MOVE_PCT
        except Exception:
            pass

        is_buy_signal  = score >= XRP_BUY_THRESHOLD
        is_sell_signal = score <= XRP_SELL_THRESHOLD
        actionnable    = is_buy_signal or is_sell_signal or is_price_spike

        if not actionnable:
            logger.info(f"XRP neutre (score={score:+.2f}) — silence")
            return None

        # ── Raisons en français simple ─────────────────────────────────────
        signaux_bruts = sig.get("signaux", [])
        raisons = _traduire_signaux(signaux_bruts)

        # ── Calcul gain/risque ─────────────────────────────────────────────
        if stop and target and stop != prix:
            risque_pct  = abs(prix - stop) / prix * 100
            gain_pct    = abs(target - prix) / prix * 100
            rr          = gain_pct / risque_pct if risque_pct else 0
        else:
            risque_pct = gain_pct = rr = 0

        # ── Message selon la situation ─────────────────────────────────────
        if is_buy_signal:
            titre   = "🟢 XRP — ACHETER sur Binance"
            intro   = "Les indicateurs sont tous alignés à la hausse."
            suite   = ""
            conseil = (
                f"Entre entre `${prix:.4f}` et `${round(prix * 1.01, 4):.4f}`\n"
                f"🛡 Coupe si ça passe sous `${stop:.4f}` (−{risque_pct:.1f}%)\n"
                f"🎯 Objectif `${target:.4f}` (+{gain_pct:.1f}%)"
            )

        elif is_price_spike and not is_sell_signal:
            direction = "hausse" if price_move_pct > 0 else "baisse"
            titre   = f"⚡ XRP — Mouvement brusque ({price_move_pct:+.1f}%)"
            intro   = f"XRP vient de bouger de {abs(price_move_pct):.1f}% en peu de temps."
            suite   = "Les indicateurs ne donnent pas encore de direction claire — surveille avant d'agir."
            conseil = f"💰 Prix actuel : `${prix:.4f}`"

        elif score >= XRP_SELL_THRESHOLD and is_price_spike:
            titre   = "🟠 XRP — Alléger 30 à 50%"
            intro   = f"Le prix chute ({price_move_pct:+.1f}%) et les indicateurs se dégradent."
            suite   = "Sécurise une partie de ta position."
            conseil = (
                f"Vends 30 à 50% autour de `${prix:.4f}`\n"
                f"♻️ Zone de rachat : autour de `${stop:.4f}` si ça se stabilise\n"
                f"Tu recevras une alerte automatique quand ça repart à la hausse"
            )

        else:  # sell signal clair
            titre   = "🔴 XRP — VENDRE sur Binance"
            intro   = "Les indicateurs montrent une dégradation claire du XRP."
            suite   = "Coupe ta position avant que ça empire."
            conseil = (
                f"Vends autour de `${prix:.4f}`\n"
                f"♻️ Rachète si XRP remonte au-dessus de `${round(prix * 1.05, 4):.4f}`\n"
                f"Tu recevras une alerte automatique dès que les signaux se retournent"
            )

        # ── Construction du message final ──────────────────────────────────
        raisons_txt = "\n".join(f"  • {r}" for r in raisons) if raisons else ""

        msg = f"*{titre}*\n\n"
        msg += f"_{intro}_\n"
        if suite:
            msg += f"_{suite}_\n"
        msg += f"\n💰 Prix : `${prix:.4f}`"
        if is_price_spike:
            msg += f" ({price_move_pct:+.1f}% aujourd'hui)"
        msg += f"\n\n{conseil}\n"
        if raisons_txt:
            msg += f"\n*Pourquoi :*\n{raisons_txt}\n"
        msg += "\n_Action manuelle sur Binance_"

        _send_telegram(msg)
        logger.info(f"XRP alerte envoyée (score={score:+.2f})")
        return {"score": score, "action": titre, "prix": prix}

    except Exception as e:
        logger.error(f"Erreur analyse XRP : {e}")
        return None


# ── Point d'entrée ─────────────────────────────────────────────────────────────

def run():
    now = datetime.now(timezone.utc)
    logger.info(f"Alert scanner démarré — {now.strftime('%d/%m/%Y %H:%M UTC')}")

    # Exécution directe (pas d'alerte inutile — le bot agit)
    trades_executed = scan_and_execute_signals()

    # Breaking news importantes uniquement
    news_alerts = scan_breaking_news()

    # XRP Binance — alerte manuelle uniquement (pas de trading auto possible)
    xrp_update = scan_xrp_binance()

    logger.info(
        f"Alert scanner terminé — "
        f"{len(trades_executed)} trade(s) exécuté(s), "
        f"{len(news_alerts)} news, "
        f"XRP: {'mis à jour' if xrp_update else 'neutre'}"
    )

    return {"trades": trades_executed, "news": news_alerts, "xrp": xrp_update}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run()
