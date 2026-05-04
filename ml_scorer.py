"""
ML Scorer — XGBoost/LightGBM pour remplacer la formule manuelle pondérée.

Au lieu de combiner les scores avec des poids fixes (Tech*0.4 + News*0.15...),
ce module apprend depuis les données réelles quels signaux fonctionnent ensemble.

Mode de fonctionnement :
1. COLLECTE : chaque signal envoyé est enregistré avec son issue (gain/perte)
2. ENTRAÎNEMENT : XGBoost apprend les patterns gagnants (après ~50 trades)
3. PRÉDICTION : le modèle remplace la formule manuelle si suffisamment entraîné

Pendant la phase de collecte (< 50 trades) : utilise le scoring classique pondéré.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

import config

logger = logging.getLogger(__name__)

MODEL_PATH = "ml_model.json"
FEATURES_DB = config.DB_PATH
MIN_SAMPLES_TO_TRAIN = 50   # Minimum de trades pour entraîner le modèle
RETRAIN_EVERY = 10          # Réentraîner tous les N nouveaux trades


# ── Features utilisées par le modèle ─────────────────────────────────────────

FEATURE_NAMES = [
    # Signaux existants
    "score_tech", "score_news", "score_ms", "score_oc", "score_cg", "score_macro",
    # Régime de marché
    "regime_bull", "regime_bear", "regime_sideways",
    "hmm_confidence", "vol_annualized", "recent_break",
    # Volume Profile
    "vp_score", "in_value_area", "above_poc", "distance_poc_pct",
    # Contexte macro
    "dxy", "dvol",
    # Microstructure
    "ls_ratio", "oi_change_4h_pct",
]


def build_feature_vector(signal: dict, regime: dict = None, vp: dict = None) -> list:
    """Construit le vecteur de features pour le modèle ML."""
    reg = regime or {}
    vp_data = vp or {}

    regime_name = reg.get("regime", "sideways")

    return [
        # Scores des modules
        signal.get("score_tech", 0),
        signal.get("score_news", 0),
        signal.get("score_ms", 0),
        signal.get("score_oc", 0),
        signal.get("score_cg", 0),
        signal.get("score_macro", 0),
        # Régime
        1 if regime_name == "bull" else 0,
        1 if regime_name == "bear" else 0,
        1 if regime_name == "sideways" else 0,
        reg.get("hmm_confidence", 0.5),
        reg.get("vol_annualized", 80) / 100,  # Normaliser
        1 if reg.get("recent_break", False) else 0,
        # Volume Profile
        vp_data.get("score", 0),
        1 if vp_data.get("in_value_area", False) else 0,
        1 if vp_data.get("above_poc", False) else 0,
        vp_data.get("distance_poc_pct", 0) / 10,  # Normaliser
        # Macro
        (signal.get("dxy", 104) - 100) / 10,  # Centrer autour de 0
        signal.get("dvol", 65) / 100,          # Normaliser
        # Microstructure
        signal.get("ls_ratio", 1.0),
        signal.get("oi_change_4h_pct", 0) / 10,  # Normaliser
    ]


def save_signal_for_training(signal: dict, regime: dict = None, vp: dict = None):
    """
    Enregistre un signal avec ses features pour l'entraînement futur.
    Le label (gain/perte) sera mis à jour lors de la fermeture de position.
    """
    try:
        conn = sqlite3.connect(FEATURES_DB)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ml_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                ticker TEXT,
                features TEXT,
                score_manual REAL,
                label REAL DEFAULT NULL,
                pnl_pct REAL DEFAULT NULL
            )
        """)

        features = build_feature_vector(signal, regime, vp)

        cursor.execute("""
            INSERT INTO ml_signals (timestamp, ticker, features, score_manual)
            VALUES (?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            signal.get("ticker", ""),
            json.dumps(features),
            signal.get("score", 0),
        ))

        conn.commit()
        conn.close()
        logger.debug(f"Signal ML enregistré pour {signal.get('ticker')}")

    except Exception as e:
        logger.warning(f"Erreur save_signal_for_training : {e}")


def update_trade_label(ticker: str, pnl_pct: float):
    """
    Met à jour le label d'un signal après fermeture de la position.
    Label : 1 si trade profitable, 0 sinon (classification binaire)
    """
    try:
        conn = sqlite3.connect(FEATURES_DB)
        cursor = conn.cursor()

        label = 1 if pnl_pct > 0 else 0

        cursor.execute("""
            UPDATE ml_signals
            SET label = ?, pnl_pct = ?
            WHERE id = (
                SELECT id FROM ml_signals
                WHERE ticker = ? AND label IS NULL
                ORDER BY id DESC LIMIT 1
            )
        """, (label, pnl_pct, ticker))

        conn.commit()
        conn.close()
        logger.info(f"Label ML mis à jour : {ticker} P&L={pnl_pct:+.1f}% label={label}")

    except Exception as e:
        logger.warning(f"Erreur update_trade_label : {e}")


def get_training_data() -> tuple:
    """Récupère les données d'entraînement depuis SQLite."""
    try:
        conn = sqlite3.connect(FEATURES_DB)
        cursor = conn.cursor()

        cursor.execute("""
            SELECT features, label FROM ml_signals
            WHERE label IS NOT NULL
        """)
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return None, None

        X = np.array([json.loads(r[0]) for r in rows])
        y = np.array([r[1] for r in rows])
        return X, y

    except Exception as e:
        logger.warning(f"Erreur get_training_data : {e}")
        return None, None


def train_model():
    """
    Entraîne un ensemble XGBoost + LightGBM — sélectionne le meilleur en CV.

    L'ensemble est plus robuste qu'un seul modèle sur des marchés changeants.
    Sauvegarde le modèle avec la meilleure accuracy walk-forward.
    """
    X, y = get_training_data()

    if X is None or len(X) < MIN_SAMPLES_TO_TRAIN:
        logger.info(f"Pas assez de données ({len(X) if X is not None else 0}/{MIN_SAMPLES_TO_TRAIN})")
        return False

    logger.info(f"Entraînement ensemble ML sur {len(X)} trades...")

    from sklearn.model_selection import cross_val_score

    best_model = None
    best_score = 0.0
    best_name = ""

    # ── XGBoost ──
    try:
        import xgboost as xgb
        xgb_model = xgb.XGBClassifier(
            n_estimators=150, max_depth=4, learning_rate=0.08,
            subsample=0.8, colsample_bytree=0.8,
            eval_metric="logloss", random_state=42, verbosity=0,
        )
        sc = cross_val_score(xgb_model, X, y, cv=5, scoring="accuracy")
        logger.info(f"XGBoost CV : {sc.mean():.2%} ± {sc.std():.2%}")
        if sc.mean() > best_score:
            best_score = sc.mean()
            best_model = xgb_model
            best_name = "XGBoost"
    except ImportError:
        logger.warning("xgboost non installé")
    except Exception as e:
        logger.warning(f"XGBoost erreur : {e}")

    # ── LightGBM ──
    try:
        import lightgbm as lgb
        lgb_model = lgb.LGBMClassifier(
            n_estimators=150, max_depth=4, learning_rate=0.08,
            subsample=0.8, colsample_bytree=0.8,
            random_state=42, verbosity=-1,
        )
        sc = cross_val_score(lgb_model, X, y, cv=5, scoring="accuracy")
        logger.info(f"LightGBM CV : {sc.mean():.2%} ± {sc.std():.2%}")
        if sc.mean() > best_score:
            best_score = sc.mean()
            best_model = lgb_model
            best_name = "LightGBM"
    except ImportError:
        logger.warning("lightgbm non installé")
    except Exception as e:
        logger.warning(f"LightGBM erreur : {e}")

    if best_model is None:
        logger.error("Aucun modèle ML disponible (xgboost et lightgbm manquants)")
        return False

    # Entraîner le meilleur sur toutes les données
    best_model.fit(X, y)
    logger.info(f"Meilleur modèle : {best_name} (CV {best_score:.2%})")

    # Sauvegarde
    if best_name == "XGBoost":
        best_model.save_model(MODEL_PATH)
        with open(MODEL_PATH + ".meta", "w") as f:
            f.write("XGBoost")
    else:
        import pickle
        lgb_path = MODEL_PATH.replace(".json", "_lgb.pkl")
        with open(lgb_path, "wb") as f:
            pickle.dump(best_model, f)
        with open(MODEL_PATH + ".meta", "w") as f:
            f.write("LightGBM")

    # Feature importance
    importance = dict(zip(FEATURE_NAMES, best_model.feature_importances_))
    top = sorted(importance.items(), key=lambda x: x[1], reverse=True)[:5]
    logger.info(f"Top features : {top}")
    return True


def _load_model():
    """Charge le modèle ML actif (XGBoost ou LightGBM)."""
    meta_path = MODEL_PATH + ".meta"
    model_type = "XGBoost"  # défaut

    if Path(meta_path).exists():
        with open(meta_path) as f:
            model_type = f.read().strip()

    if model_type == "LightGBM":
        import pickle
        lgb_path = MODEL_PATH.replace(".json", "_lgb.pkl")
        if Path(lgb_path).exists():
            with open(lgb_path, "rb") as f:
                return pickle.load(f), "LightGBM"

    # XGBoost par défaut
    if Path(MODEL_PATH).exists():
        import xgboost as xgb
        model = xgb.XGBClassifier()
        model.load_model(MODEL_PATH)
        return model, "XGBoost"

    return None, None


def predict_score(signal: dict, regime: dict = None, vp: dict = None) -> dict:
    """
    Prédit le score via le meilleur modèle ML disponible (XGBoost ou LightGBM).
    Fallback sur le score manuel sinon.

    Returns : score ML (-3 à +3) + confiance + source
    """
    meta_path = MODEL_PATH + ".meta"
    lgb_path = MODEL_PATH.replace(".json", "_lgb.pkl")
    model_exists = Path(MODEL_PATH).exists() or Path(lgb_path).exists()

    if not model_exists:
        return {
            "score_ml": signal.get("score", 0),
            "ml_confidence": 0.0,
            "ml_active": False,
            "source": "manual_scoring",
        }

    try:
        model, model_name = _load_model()
        if model is None:
            raise ValueError("Modèle introuvable")

        features = build_feature_vector(signal, regime, vp)
        X = np.array([features])

        proba = model.predict_proba(X)[0]
        prob_win = float(proba[1])

        # prob=0.5 → score=0, prob=1.0 → score=+3, prob=0.0 → score=-3
        score_ml = (prob_win - 0.5) * 6
        score_ml = round(max(-3.0, min(3.0, score_ml)), 2)

        # Respecter le signe directionnel du signal technique
        score_tech = signal.get("score_tech", 0)
        if score_tech < 0 and score_ml > 0:
            score_ml = -abs(score_ml)

        logger.info(
            f"ML ({model_name}) score={score_ml:+.2f} "
            f"(prob_win={prob_win:.2%}) vs manuel={signal.get('score', 0):+.2f}"
        )

        return {
            "score_ml": score_ml,
            "ml_confidence": prob_win,
            "ml_active": True,
            "source": model_name.lower(),
        }

    except Exception as e:
        logger.warning(f"Erreur prediction ML : {e}")
        return {
            "score_ml": signal.get("score", 0),
            "ml_confidence": 0.0,
            "ml_active": False,
            "source": "manual_fallback",
        }


def get_model_status() -> dict:
    """Retourne le statut du modèle ML."""
    X, y = get_training_data()
    n_samples = len(X) if X is not None else 0
    model_exists = Path(MODEL_PATH).exists()

    return {
        "n_labeled_trades": n_samples,
        "model_trained": model_exists,
        "ready_for_training": n_samples >= MIN_SAMPLES_TO_TRAIN,
        "samples_needed": max(0, MIN_SAMPLES_TO_TRAIN - n_samples),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    status = get_model_status()
    print(f"Status ML : {status}")

    if status["model_trained"]:
        print("Modèle XGBoost disponible")
    else:
        print(f"En attente de {status['samples_needed']} trades supplémentaires pour entraîner")
