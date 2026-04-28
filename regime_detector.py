"""
Détection de régimes de marché — module critique.

Problème fondamental sans ce module :
  RSI=70 en bull market trending → signal d'achat valide
  RSI=70 en bear market → piège, faux signal

Ce module détecte dans quel régime on est AVANT d'interpréter
les signaux techniques. Tous les autres signaux sont conditionnés à ce contexte.

3 approches combinées :
  1. HMM (Hidden Markov Model) — régimes cachés bull/bear/sideways
  2. GARCH(1,1) — volatilité future estimée → taille de position dynamique
  3. Ruptures — détection de changements structurels récents

Output : régime + volatilité estimée + multiplicateur de position
"""

import logging
import warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

# Régimes possibles
REGIME_BULL     = "bull"
REGIME_BEAR     = "bear"
REGIME_SIDEWAYS = "sideways"


# ── 1. Hidden Markov Model ────────────────────────────────────────────────────

def detect_regime_hmm(df: pd.DataFrame, n_states: int = 3) -> dict:
    """
    Détecte le régime de marché actuel via HMM sur les returns + volatilité.

    États cachés : bull (trend haussier) / bear (trend baissier) / sideways (range)
    Le modèle apprend les transitions entre états depuis l'historique.

    Returns : régime actuel + probabilité de chaque état
    """
    try:
        from hmmlearn.hmm import GaussianHMM

        if df.empty or len(df) < 30:
            return {"regime": REGIME_SIDEWAYS, "confidence": 0.5, "source": "insufficient_data"}

        # Features : returns journaliers + volatilité réalisée rolling
        returns = df["close"].pct_change().dropna()
        vol = returns.rolling(5).std().dropna()

        # Aligner les deux séries
        min_len = min(len(returns), len(vol))
        returns = returns.iloc[-min_len:]
        vol = vol.iloc[-min_len:]

        X = np.column_stack([returns.values, vol.values])

        # Entraîner le HMM
        model = GaussianHMM(
            n_components=n_states,
            covariance_type="full",
            n_iter=200,
            random_state=42,
        )
        model.fit(X)

        # Prédire l'état actuel
        hidden_states = model.predict(X)
        current_state = hidden_states[-1]

        # Probabilités de l'état courant
        state_probs = model.predict_proba(X)[-1]
        confidence = float(state_probs[current_state])

        # Identifier quel état = bull/bear/sideways selon les means des returns
        state_means = []
        for s in range(n_states):
            mask = hidden_states == s
            if mask.sum() > 0:
                state_means.append((s, float(returns.values[mask].mean())))
            else:
                state_means.append((s, 0.0))

        # Trier par return moyen : état à return le plus élevé = bull
        sorted_states = sorted(state_means, key=lambda x: x[1])
        bear_state = sorted_states[0][0]
        bull_state = sorted_states[-1][0]
        sideways_state = sorted_states[1][0] if n_states == 3 else None

        if current_state == bull_state:
            regime = REGIME_BULL
        elif current_state == bear_state:
            regime = REGIME_BEAR
        else:
            regime = REGIME_SIDEWAYS

        # Proportion du temps dans chaque régime (30 dernières bougies)
        recent_states = hidden_states[-30:]
        bull_pct = float((recent_states == bull_state).mean() * 100)
        bear_pct = float((recent_states == bear_state).mean() * 100)

        logger.debug(f"HMM régime={regime} conf={confidence:.2f} bull30d={bull_pct:.0f}% bear30d={bear_pct:.0f}%")

        return {
            "regime": regime,
            "confidence": round(confidence, 3),
            "bull_pct_30d": round(bull_pct, 1),
            "bear_pct_30d": round(bear_pct, 1),
            "source": "hmm",
        }

    except ImportError:
        logger.warning("hmmlearn non installé — régime via règles simples")
        return _regime_fallback(df)
    except Exception as e:
        logger.warning(f"HMM erreur : {e} — fallback")
        return _regime_fallback(df)


def _regime_fallback(df: pd.DataFrame) -> dict:
    """Détection de régime simplifiée si hmmlearn non disponible."""
    if df.empty or len(df) < 20:
        return {"regime": REGIME_SIDEWAYS, "confidence": 0.5, "source": "fallback"}

    close = df["close"]
    ema20 = close.ewm(span=20).mean()
    ema50 = close.ewm(span=50).mean()
    current = close.iloc[-1]

    if current > ema20.iloc[-1] > ema50.iloc[-1]:
        regime = REGIME_BULL
        conf = 0.7
    elif current < ema20.iloc[-1] < ema50.iloc[-1]:
        regime = REGIME_BEAR
        conf = 0.7
    else:
        regime = REGIME_SIDEWAYS
        conf = 0.6

    return {"regime": regime, "confidence": conf, "source": "ema_fallback"}


# ── 2. GARCH — Volatilité future ──────────────────────────────────────────────

def estimate_volatility_garch(df: pd.DataFrame) -> dict:
    """
    Modèle GARCH(1,1) pour estimer la volatilité future.

    Volatilité attendue élevée → réduire la taille des positions
    Volatilité attendue faible → taille normale, signaux plus fiables

    Returns : volatilité annualisée estimée + multiplicateur de position (0.3 à 1.0)
    """
    try:
        from arch import arch_model

        if df.empty or len(df) < 30:
            return {"vol_annualized": 80.0, "position_multiplier": 0.7, "vol_regime": "normal", "source": "insufficient_data"}

        returns = df["close"].pct_change().dropna() * 100  # En pourcentage pour GARCH

        # GARCH(1,1) — standard industrie
        am = arch_model(returns, vol="Garch", p=1, q=1, dist="normal")
        res = am.fit(disp="off", show_warning=False)

        # Forecast 1 période
        forecast = res.forecast(horizon=1)
        vol_1d = float(np.sqrt(forecast.variance.iloc[-1, 0]))  # Volatilité journalière %
        vol_annualized = vol_1d * np.sqrt(365)  # Annualisée

        # Régime de volatilité
        if vol_annualized < 40:
            vol_regime = "calm"
            position_multiplier = 1.0
        elif vol_annualized < 80:
            vol_regime = "normal"
            position_multiplier = 0.85
        elif vol_annualized < 120:
            vol_regime = "elevated"
            position_multiplier = 0.65
        else:
            vol_regime = "extreme"
            position_multiplier = 0.40

        logger.debug(f"GARCH vol_ann={vol_annualized:.1f}% régime={vol_regime} mult={position_multiplier}")

        return {
            "vol_annualized": round(vol_annualized, 1),
            "vol_daily_pct": round(vol_1d, 2),
            "vol_regime": vol_regime,
            "position_multiplier": position_multiplier,
            "source": "garch",
        }

    except ImportError:
        logger.warning("arch non installé — volatilité via std rolling")
        return _vol_fallback(df)
    except Exception as e:
        logger.warning(f"GARCH erreur : {e} — fallback")
        return _vol_fallback(df)


def _vol_fallback(df: pd.DataFrame) -> dict:
    """Estimation de volatilité sans GARCH."""
    if df.empty:
        return {"vol_annualized": 80.0, "position_multiplier": 0.7, "vol_regime": "normal", "source": "fallback"}

    returns = df["close"].pct_change().dropna()
    vol_daily = float(returns.std() * 100)
    vol_ann = vol_daily * np.sqrt(365)

    if vol_ann < 40:
        mult, regime = 1.0, "calm"
    elif vol_ann < 80:
        mult, regime = 0.85, "normal"
    elif vol_ann < 120:
        mult, regime = 0.65, "elevated"
    else:
        mult, regime = 0.40, "extreme"

    return {"vol_annualized": round(vol_ann, 1), "vol_daily_pct": round(vol_daily, 2),
            "vol_regime": regime, "position_multiplier": mult, "source": "std_fallback"}


# ── 3. Ruptures — Détection de changepoints ───────────────────────────────────

def detect_changepoints(df: pd.DataFrame) -> dict:
    """
    Détecte les ruptures structurelles récentes dans la série de prix.
    Un changepoint récent = le marché vient de changer de comportement
    → réduire la confiance des signaux basés sur l'historique long

    Returns : nombre de changepoints 30 derniers jours + jours depuis le dernier
    """
    try:
        import ruptures as rpt

        if df.empty or len(df) < 20:
            return {"n_changepoints_30d": 0, "days_since_last": 99, "recent_break": False}

        signal = df["close"].values
        # Pelt avec coût rbf — sensible aux changements de niveau ET de variance
        algo = rpt.Pelt(model="rbf", min_size=3, jump=1).fit(signal)
        breakpoints = algo.predict(pen=3)  # pen=3 : sensibilité modérée

        n = len(df)
        # Garder uniquement les changepoints dans les 30 dernières bougies
        recent = [b for b in breakpoints if b >= n - 30 and b < n]
        days_since = (n - recent[-1]) if recent else 99

        return {
            "n_changepoints_30d": len(recent),
            "days_since_last": days_since,
            "recent_break": days_since <= 7,  # Rupture dans les 7 derniers jours
        }

    except ImportError:
        logger.warning("ruptures non installé")
        return {"n_changepoints_30d": 0, "days_since_last": 99, "recent_break": False}
    except Exception as e:
        logger.warning(f"Ruptures erreur : {e}")
        return {"n_changepoints_30d": 0, "days_since_last": 99, "recent_break": False}


# ── Analyse combinée ──────────────────────────────────────────────────────────

def analyze(df: pd.DataFrame) -> dict:
    """
    Analyse de régime complète : HMM + GARCH + ruptures.

    Outputs clés :
    - regime : bull / bear / sideways
    - position_multiplier : 0.3 à 1.0 (ajuste la taille des ordres)
    - score : -1.0 à +1.0 (intégré dans le score composite du scanner)
    - regime_context : description humaine pour les alertes Telegram
    """
    hmm = detect_regime_hmm(df)
    garch = estimate_volatility_garch(df)
    breaks = detect_changepoints(df)

    regime = hmm.get("regime", REGIME_SIDEWAYS)
    vol_regime = garch.get("vol_regime", "normal")
    position_mult = garch.get("position_multiplier", 0.85)
    recent_break = breaks.get("recent_break", False)

    # Score de régime : bull = positif, bear = négatif, sideways = neutre
    score = 0.0
    signals = []

    if regime == REGIME_BULL:
        score += 0.6
        signals.append(f"Régime BULL détecté (HMM conf={hmm['confidence']:.0%})")
    elif regime == REGIME_BEAR:
        score -= 0.6
        signals.append(f"Régime BEAR détecté (HMM conf={hmm['confidence']:.0%})")
    else:
        signals.append(f"Régime SIDEWAYS — signaux techniques moins fiables")

    # Pénalité si volatilité extrême
    if vol_regime == "extreme":
        score *= 0.5
        position_mult = min(position_mult, 0.4)
        signals.append(f"Volatilité EXTRÊME (GARCH {garch['vol_annualized']:.0f}%/an) — positions réduites à 40%")
    elif vol_regime == "elevated":
        score *= 0.75
        signals.append(f"Volatilité élevée (GARCH {garch['vol_annualized']:.0f}%/an) — prudence")
    elif vol_regime == "calm":
        signals.append(f"Volatilité faible ({garch['vol_annualized']:.0f}%/an) — conditions idéales")

    # Rupture récente = incertitude accrue
    if recent_break:
        score *= 0.7
        position_mult = min(position_mult, 0.6)
        signals.append(f"⚠️ Rupture structurelle il y a {breaks['days_since_last']}j — contexte en transition")

    score = round(max(-1.0, min(1.0, score)), 2)

    # Description humaine pour Telegram
    regime_context = {
        REGIME_BULL: "📈 Régime haussier",
        REGIME_BEAR: "📉 Régime baissier",
        REGIME_SIDEWAYS: "↔️ Régime lateral",
    }.get(regime, "❓ Indéterminé")

    if vol_regime in ("elevated", "extreme"):
        regime_context += f" | ⚡ Vol {garch['vol_annualized']:.0f}%"

    return {
        "regime": regime,
        "score": score,
        "position_multiplier": round(position_mult, 2),
        "vol_annualized": garch.get("vol_annualized", 80),
        "vol_regime": vol_regime,
        "vol_daily_pct": garch.get("vol_daily_pct", 2.0),
        "hmm_confidence": hmm.get("confidence", 0.5),
        "recent_break": recent_break,
        "days_since_break": breaks.get("days_since_last", 99),
        "signals": signals,
        "regime_context": regime_context,
        "verdict": regime_context,
    }


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    import okx_client as okx
    logging.basicConfig(level=logging.INFO)

    ohlcv = okx.get_all_ohlcv(["BTC", "ETH", "SOL"], days=90)
    for ticker, df in ohlcv.items():
        r = analyze(df)
        print(f"\n{ticker}: {r['regime_context']} | score={r['score']:+.2f} | "
              f"vol={r['vol_annualized']:.0f}%/an | position_mult={r['position_multiplier']}")
        for s in r["signals"]:
            print(f"  • {s}")
