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

# XRP : seuils stricts — analyse toutes les 2h uniquement
XRP_BUY_THRESHOLD  =  2.0   # >= 2.0 → "ACHETER"
XRP_SELL_THRESHOLD = -2.0   # <= -2.0 → "VENDRE" (signal très fort requis)
XRP_PRICE_MOVE_PCT =  4.0   # Alerte aussi si prix bouge > 4% sur la journée
XRP_ANALYSIS_INTERVAL_H = 2 # N'analyser XRP que toutes les 2h (heures paires UTC)

STABLES_EXCLUDE = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP",
    "WBTC", "WETH", "STETH", "BETH", "BBTC",
    # Restreints EEA — listés sur OKX mais ordres bloqués (code 1 "All operations failed")
    "OFC",
}

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

    # Filtre BTC 50MA — pas d'achat en marché baissier
    from position_manager import is_btc_uptrend
    if not is_btc_uptrend():
        logger.info("BTC sous MA50 — achats bloqués ce cycle")
        return []

    # Top N OKX EEA par volume — cap pour tenir dans le timeout GitHub Actions (14min)
    # Paires triées par volume décroissant → on garde les plus liquides
    universe = [t for t in okx.get_available_pairs(min_volume_usdc=500_000)
                if t not in STABLES_EXCLUDE and t != "XRP"][:40]
    logger.info(f"Alert scanner — univers : {len(universe)} actifs (cap=40)")

    ohlcv = okx.get_all_ohlcv(universe, days=60)
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

        # ── Rotation partielle si pas assez de USDC ────────────────────────
        usdc_pour_trade = usdc_available
        rotation_faite  = False
        besoin_usdc     = portfolio_value * 0.20  # 20% du portfolio = taille cible

        if usdc_available < 10 and positions_scores:
            # Trouver la position la plus faible (score le plus bas)
            pos_faible, score_faible = min(
                positions_scores.items(),
                key=lambda x: (x[1], positions_pnl.get(x[0], {}).get("pnl", 0))
            )
            pnl_faible    = positions_pnl.get(pos_faible, {}).get("pnl", 0)
            qty_faible    = positions_pnl.get(pos_faible, {}).get("qty", 0)
            prix_faible   = positions_pnl.get(pos_faible, {}).get("prix", 0)
            valeur_faible = qty_faible * prix_faible

            ecart_score = score - score_faible

            if ecart_score < 1.2:
                logger.info(
                    f"Rotation impossible : écart score trop faible "
                    f"({ticker} {score:+.2f} vs {pos_faible} {score_faible:+.2f})"
                )
                continue

            # ── Calcul du % à vendre : entre 10% et 100% selon l'écart ────
            # ecart 1.2 → vendre ~15%  (signal légèrement supérieur)
            # ecart 2.0 → vendre ~55%  (signal nettement supérieur)
            # ecart 3.0 → vendre 100%  (signal écrasant)
            pct_vente = min(1.0, max(0.10, (ecart_score - 1.2) / 1.8))

            # Ne vendre que ce dont on a besoin — pas plus
            usdc_libere_max = valeur_faible * pct_vente
            if usdc_libere_max < besoin_usdc:
                # On peut vendre plus si la position est perdante ou neutre
                if pnl_faible < -3.0 or score_faible < 0.5:
                    pct_vente = min(1.0, besoin_usdc / valeur_faible)
                    usdc_libere_max = valeur_faible * pct_vente

            qty_a_vendre  = qty_faible * pct_vente
            usdc_estime   = usdc_libere_max * 0.997  # après frais

            if usdc_estime < 5:
                logger.info(f"Rotation : montant libéré trop faible (${usdc_estime:.2f}) — ignoré")
                continue

            logger.info(
                f"Rotation {pct_vente*100:.0f}% de {pos_faible} "
                f"(score {score_faible:+.2f}, PnL {pnl_faible:+.1f}%) "
                f"→ libère ~${usdc_estime:.0f} pour {ticker} (score {score:+.2f})"
            )

            try:
                import time as _time
                okx.place_order(
                    ticker=pos_faible,
                    side="sell",
                    quantity=qty_a_vendre,  # 100% — plus de buffer qui laisse du dust
                    order_type="market",
                )
                pnl_emoji = "🟢" if pnl_faible >= 0 else "🔴"
                action_txt = "Vente totale" if pct_vente >= 0.99 else f"Vente partielle {pct_vente*100:.0f}%"
                _send_telegram(
                    f"🔄 *Rotation — {action_txt} {pos_faible}*\n\n"
                    f"{pnl_emoji} *{pos_faible}* `{pct_vente*100:.0f}%` vendu "
                    f"(PnL `{pnl_faible:+.1f}%`)\n"
                    f"💰 USDC libéré : `~${usdc_estime:.0f}`\n"
                    f"→ Achat *{ticker}* (signal `{score:+.2f}` vs `{score_faible:+.2f}`)"
                )
                usdc_pour_trade = usdc_estime
                rotation_faite  = True
                if pct_vente >= 0.99:
                    open_positions.discard(pos_faible)
                _time.sleep(3)  # laisser l'ordre se remplir
            except Exception as e:
                logger.error(f"Échec rotation {pos_faible} : {e}")
                continue

        elif usdc_available < 10:
            logger.info(f"{ticker} : pas de USDC et aucune position à rotater — ignoré")
            continue

        # ── Exécution du trade ──────────────────────────────────────────────
        pv = portfolio_value

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
# Chaque signal est taggé : "bull" (haussier), "bear" (baissier), "neutral"
# On filtre pour ne montrer QUE les raisons cohérentes avec la direction.

_SIGNAL_MAP = [
    # (mot-clé,                          traduction,                                           direction)
    ("EMA 8>21>55>200 alignées haussière","Toutes les tendances sont orientées à la hausse",    "bull"),
    ("EMA 8<21<55<200 alignées baissière","Toutes les tendances sont orientées à la baisse",    "bear"),
    ("EMA partiellement haussière",       "La tendance court terme est haussière",               "bull"),
    ("Golden Cross",                      "Signal d'achat déclenché",                            "bull"),
    ("Death Cross",                       "Signal de vente déclenché",                           "bear"),
    ("RSI survendu",                      "Les vendeurs s'épuisent — pression baissière faiblit","bear"),
    ("RSI suracheté",                     "XRP est en surachat — risque de retournement",        "bear"),
    ("StochRSI survendu",                 "Les oscillateurs touchent un plancher",               "bear"),
    ("StochRSI suracheté",                "Les oscillateurs sont en zone de retournement",       "bear"),
    ("StochRSI croisement haussier",      "Les oscillateurs court terme se retournent à la hausse","bull"),
    ("MACD croisement haussier",          "Le momentum bascule à la hausse",                    "bull"),
    ("MACD croisement baissier",          "Le momentum bascule à la baisse",                    "bear"),
    ("OBV haussier",                      "Le volume confirme la hausse",                        "bull"),
    ("Supertrend retournement HAUSSIER",  "Signal d'entrée technique fort",                     "bull"),
    ("Supertrend retournement BAISSIER",  "Signal de sortie technique fort",                    "bear"),
    ("Bollinger Squeeze",                 "Une forte variation de prix est imminente",           "neutral"),
    ("CVD divergence HAUSSIÈRE",          "Des acheteurs importants accumulent discrètement",   "bull"),
    ("CVD divergence BAISSIÈRE",          "Des vendeurs importants se déchargent discrètement", "bear"),
    ("Donchian BREAKOUT haussier",        "XRP vient de casser une résistance majeure",         "bull"),
    ("Donchian BREAKDOWN baissier",       "XRP vient de casser un support majeur",              "bear"),
    ("Elder Ray BUY",                     "La dynamique acheteur est dominante",                 "bull"),
    ("Elder Ray SELL",                    "La dynamique vendeur est dominante",                  "bear"),
    ("ADX",                               "La tendance est forte et confirmée",                  "neutral"),
    ("Prix sous bande BB inférieure",     "XRP est en survente extrême",                         "bear"),
    ("Prix au-dessus de la Value Area",   "XRP est au-dessus de sa zone de valeur — breakout",  "bull"),
    ("Prix en dessous de la Value Area",  "XRP est sous sa zone de valeur — pression baissière","bear"),
    ("Ichimoku",                          "Les indicateurs de tendance sont en zone d'indécision","neutral"),
    ("Volume",                            "Le volume confirme le mouvement",                     "neutral"),
]


def _traduire_signaux(signaux_bruts: list, direction: str = "neutral") -> list:
    """
    Convertit les signaux techniques en phrases compréhensibles.
    Ne garde QUE les raisons cohérentes avec la direction (bull/bear/neutral).
    Évite d'afficher "rebond probable" dans une alerte de vente, etc.
    """
    allowed = {direction, "neutral"}
    resultat = []

    for s in signaux_bruts:
        for cle, traduction, tag in _SIGNAL_MAP:
            if cle.lower() in s.lower():
                if tag in allowed and traduction not in resultat:
                    resultat.append(traduction)
                break  # signal reconnu, passer au suivant

    return resultat[:3]


# ── 3. Suivi XRP Binance ──────────────────────────────────────────────────────

def scan_xrp_binance():
    """
    Analyse XRP toutes les 2h (heures paires UTC) — pas à chaque run de 30min.
    Envoie une alerte uniquement si signal fort ou mouvement de prix brusque.

    Seuils :
    - Achat  : score >= 2.0  (signal fort haussier)
    - Vente  : score <= -2.0 (signal fort baissier — pas juste une correction)
    - Spike  : prix bouge > 4% sur la journée (urgence, toujours alerté)
    """
    now = datetime.now(timezone.utc)

    # Verrou 2h : n'analyser qu'aux heures paires (0h, 2h, 4h... UTC)
    # Exception : toujours analyser si spike de prix détecté (géré plus bas)
    is_analysis_hour = (now.hour % XRP_ANALYSIS_INTERVAL_H == 0)

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

        # Mode ACHAT UNIQUEMENT — pas d'alertes de vente XRP.
        # XRP est une position long terme sur Binance. Les alertes de vente
        # créaient un whipsawing systématique (vendre à X, racheter à X+5%).
        # Le bot alerte uniquement quand c'est un bon moment d'ACHETER davantage.
        is_buy_signal  = score >= XRP_BUY_THRESHOLD
        is_price_spike = is_price_spike  # spike toujours pertinent (info)

        actionnable = is_buy_signal or is_price_spike

        if not actionnable:
            logger.info(f"XRP neutre (score={score:+.2f}) — silence")
            return None

        # Verrou 2h — sauf spike
        if not is_price_spike and not is_analysis_hour:
            logger.info(f"XRP hors fenêtre 2h (heure {now.hour}h) — ignoré")
            return None

        # Raisons haussières uniquement
        signaux_bruts = sig.get("signaux", [])
        raisons = _traduire_signaux(signaux_bruts, direction="bull")

        # Calcul des niveaux
        if stop and target and stop != prix:
            risque_pct = abs(prix - stop) / prix * 100
            gain_pct   = abs(target - prix) / prix * 100
        else:
            risque_pct = gain_pct = 0

        # ── Message selon la situation ─────────────────────────────────────
        if is_buy_signal:
            titre  = "🟢 XRP — Bon moment pour renforcer"
            intro  = "Les indicateurs sont favorables pour accumuler du XRP."
            conseil = (
                f"Zone d'achat : `${prix:.4f}` — `${round(prix * 1.01, 4):.4f}`\n"
                f"🛡 Stop suggéré : `${stop:.4f}` (−{risque_pct:.1f}%)\n"
                f"🎯 Objectif : `${target:.4f}` (+{gain_pct:.1f}%)"
            )
        else:  # spike uniquement
            direction_spike = "hausse" if price_move_pct > 0 else "baisse"
            titre  = f"⚡ XRP — Mouvement brusque ({price_move_pct:+.1f}%)"
            intro  = f"XRP vient de bouger de {abs(price_move_pct):.1f}% en peu de temps."
            conseil = (
                f"💰 Prix actuel : `${prix:.4f}`\n"
                f"_Pas encore de signal d'achat clair — attends la confirmation._"
            )

        # ── Construction du message final ──────────────────────────────────
        raisons_txt = "\n".join(f"  • {r}" for r in raisons) if raisons else ""

        msg = f"*{titre}*\n\n"
        msg += f"_{intro}_\n"
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


# ── Arrêt d'urgence (30min) ──────────────────────────────────────────────────────
# Le bot principal vérifie les stops toutes les 4h → un actif peut chuter de -20%
# avant d'être stoppé. L'alert scanner (30min) ajoute une couche de protection :
# si une position dépasse le stop ATR, elle est vendue immédiatement.

def emergency_stop_check() -> list[str]:
    """
    Vérifie toutes les positions ouvertes contre leur stop ATR dynamique.
    Vend immédiatement toute position en dessous de son stop.
    Appelé toutes les 30min — réduit le gap risk de 4h à 30min.

    Retourne la liste des tickers vendus en urgence.
    """
    sold = []
    try:
        import position_manager as pm

        positions = pm.get_open_positions()
        if not positions:
            return []

        for pos in positions:
            ticker  = pos["ticker"]
            entree  = pos.get("prix_entree")
            prix    = pos.get("prix_actuel")
            pnl_pct = pos.get("pnl_pct")
            valeur  = pos.get("valeur_usd", 0)

            if not entree or not prix or pnl_pct is None:
                continue  # orphelin ou données manquantes

            # Calcul du stop ATR (même logique que position_manager)
            stop_price = pm.get_atr_stop(ticker, entree)
            stop_pct   = (stop_price - entree) / entree * 100

            if pnl_pct <= stop_pct:
                logger.warning(
                    f"[URGENCE] {ticker} stop ATR dépassé : "
                    f"P&L {pnl_pct:.1f}% <= stop {stop_pct:.1f}% — vente immédiate"
                )

                try:
                    fresh = okx.get_balances()
                    qty   = fresh.get(ticker.upper(), pos["qty"])
                    if qty <= 0:
                        continue

                    result   = okx.place_order(ticker=ticker, side="sell",
                                               quantity=qty, order_type="market")
                    ordre_id = result.get("ordId", "?")
                    sold.append(ticker)

                    _send_telegram(
                        f"🚨 *STOP URGENCE {ticker}*\n"
                        f"P&L `{pnl_pct:+.1f}%` — stop ATR `{stop_pct:.1f}%`\n"
                        f"Vendu avant le prochain cycle 4h\n"
                        f"Valeur : `${valeur:.2f}` | ID : `{ordre_id}`"
                    )

                    # Mémoriser l'outcome
                    try:
                        import ruflo_memory as rm
                        rm.store_trade_outcome({
                            "ticker":    ticker,
                            "pnl_pct":   pnl_pct,
                            "days_held": pos.get("days_held"),
                            "raison":    f"Stop ATR urgence 30min ({pnl_pct:.1f}% < {stop_pct:.1f}%)",
                            "valeur":    valeur,
                            "qty":       qty,
                        })
                    except Exception:
                        pass

                except Exception as e:
                    logger.error(f"[URGENCE] Vente {ticker} échouée : {e}")
                    _send_telegram(
                        f"⚠️ *STOP URGENCE {ticker} ÉCHOUÉ*\n"
                        f"P&L `{pnl_pct:+.1f}%` — vente manuelle requise !\n`{str(e)[:100]}`"
                    )

    except Exception as e:
        logger.error(f"emergency_stop_check erreur globale : {e}")

    return sold


# ── Point d'entrée ─────────────────────────────────────────────────────────────

def run():
    now = datetime.now(timezone.utc)
    logger.info(f"Alert scanner démarré — {now.strftime('%d/%m/%Y %H:%M UTC')}")

    # ── Priorité 1 : arrêts d'urgence (avant tout le reste) ──────────────────
    # Si une position dépasse son stop ATR, on vend immédiatement sans attendre
    # le prochain cycle 4h du bot principal.
    emergency_sold = emergency_stop_check()
    if emergency_sold:
        logger.warning(f"Stops d'urgence exécutés : {emergency_sold}")

    # Exécution directe (pas d'alerte inutile — le bot agit)
    trades_executed = scan_and_execute_signals()

    # Breaking news importantes uniquement
    news_alerts = scan_breaking_news()

    # XRP Binance — alerte manuelle uniquement (pas de trading auto possible)
    xrp_update = scan_xrp_binance()

    logger.info(
        f"Alert scanner terminé — "
        f"{len(emergency_sold)} stop(s) urgence, "
        f"{len(trades_executed)} trade(s) exécuté(s), "
        f"{len(news_alerts)} news, "
        f"XRP: {'mis à jour' if xrp_update else 'neutre'}"
    )

    return {"trades": trades_executed, "emergency_stops": emergency_sold,
            "news": news_alerts, "xrp": xrp_update}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run()
