# Design — Bot Crypto : Machine de Guerre
**Date :** 2026-05-25  
**Statut :** Validé — en attente d'implémentation  
**Contexte :** Portfolio $638, 20 trades réels, win rate 30%, EV/trade -1.6%

---

## Objectif

Transformer le bot en un système qui apprend, s'adapte et génère un EV positif sur chaque mois roulant. Deux axes principaux : améliorer la sélection des entrées + accélérer la capture des opportunités.

---

## Diagnostic de départ

- **Win rate 30%** (6/20 trades) — EV = -1.6%/trade
- **Cause principale identifiée** : entrées sur tokens old-cycle ou sans narrative active (SOL, ADA, ARB, CHZ...) vs wins sur tokens à narrative forte (DYDX, ARKM, ONDO, BIO)
- **Exits** : majoritairement corrigées (trailing 30min, P2.5, TP 30min — déployé le 20/05)
- **Mémoire** : ne fait rien en pratique (aucun ticker avec 3+ trades)
- **News Interpreter** : potentiellement en timeout silencieux — non vérifié

---

## Feature 1 — News Interpreter + Poids Narrative

### Problème
Le pattern wins/losses montre que la narrative est le signal le plus discriminant. Si score_news est 1/8ème du score final et que le News Interpreter crashe silencieusement, le bot ignore complètement l'information la plus prédictive.

### Implémentation
1. Vérifier les logs GitHub Actions des 10 derniers cycles — est-ce que `news_interpreter.py` produit une sortie ou timeout ?
2. Corriger si besoin (timeout, fallback manquant)
3. Augmenter provisoirement le poids de `score_news` à ×2 dans le score final
4. Valider sur 20 prochains trades si la sélection s'améliore

### Règle transverse
Momentum filter : si un token a fait **+15% dans les 72h précédentes** → score plafonné à 1.4 (on n'achète pas ce que tout le monde a déjà acheté).

---

## Feature 2 — Stockage des sous-scores bruts + Exit Quality

### Problème
On enregistre le PnL final mais pas les sous-scores à l'entrée. Impossible de savoir quelle dimension est prédictive. Impossible de repondérer les signaux plus tard sans données.

### Implémentation
Dans `store_trade_entry()` de `ruflo_memory.py`, ajouter tous les sous-scores bruts (non pondérés) :
```json
{
  "score_tech_raw": 0.72,
  "score_news_raw": 0.45,
  "score_micro_raw": 0.31,
  "score_macro_raw": 0.28,
  "score_onchain_raw": 0.15,
  "score_coinglass_raw": 0.42,
  "score_regime_raw": 0.60,
  "score_volprofile_raw": 0.38
}
```

Dans `store_trade_outcome()`, ajouter `exit_quality` :
```
exit_quality = (pnl_sortie / peak_pnl_connu) × 100
```
Un exit à +5% sur un token qui a ensuite fait +15% = exit_quality 33%.

### Utilisation future
Après 40-50 trades réels avec sub-scores stockés : régression logistique simple (sklearn) sur win/loss ~ sous-scores → nouvelles pondérations. Sub-scores stockés bruts permettent recalcul rétroactif sans discontinuité.

---

## Feature 3 — Dual Entry Path (30min scalp + 4h swing)

### Problème
Le bot entre toutes les 4h. Une opportunité scalp à 14h n'est capturée qu'à 16h — le mouvement est souvent passé. Recalculer les 8 dimensions en 30min est impossible (timeout, rate limits).

### Architecture
**4h scanner (actuel)** : calcule les scores complets sur 55 tokens, stocke les top-scores dans `trade_memory.json` :
```json
"pending_signals": [
  {"ticker": "XYZ", "score": 2.3, "timestamp": "...", "prix_ref": 1.234, "ttl": 14400}
]
```

**30min alert_scanner (nouveau chemin d'entrée)** : ne recalcule pas les 8 dimensions. Pour chaque signal en `pending_signals` :
1. Prix actuel toujours dans ±2% du prix de référence ?
2. Volume pas effondré (>50% de la moyenne 1h) ?
3. Si oui → ENTRÉE SCALP

### Paramètres scalp
- Score requis : ≥ 2.0 (seuil plus élevé que swing)
- Stop : -3% fixe (pas ATR — trop large pour scalp)
- TP : +5%
- Time stop : 4h (1 cycle principal)
- Si pas de mouvement à +1% après 2h → exit au prix actuel (libère capital)

### Paramètres swing (inchangés)
- Score requis : ≥ seuil dynamique (1.3–2.0)
- Stop : ATR dynamique (-4% à -10%)
- TP : +12% / +20% si score ≥ 2.0
- Time stop : 7 jours

### Règle transverse
Positions en profit ≥ +3% sont **protégées de la rotation**. Seules les positions à plat ou en perte sont candidates à la liquidation pour financer un nouveau signal.

---

## Feature 4 — EV Médiane Rolling + Mode Observation

### EV Rolling (remplace WR comme signal d'agressivité)
Calculé sur les **15 trades les plus récents** avec médiane (robuste aux outliers type CHZ -22%) :
```
EV = (WR × médiane_gains) − (LR × médiane_pertes)
```

**Hysteresis** pour éviter les flip-flops :
- EV < 0 sur **3 cycles consécutifs** → mode conservateur (threshold +0.15, taille ×0.8)
- EV > 1.5% sur **5 cycles consécutifs** → retour mode normal
- EV > 3% sur **5 cycles** → mode agressif (threshold -0.1, taille ×1.1)

### Mode Observation (double condition)
Aucune nouvelle entrée si **les deux conditions simultanément** :
1. BTC sous sa MA50 daily
2. Régime HMM = "bear"

Une seule condition → threshold +0.2 uniquement (pas de blocage total).

**Explicite :** mode observation = blocage des entrées uniquement. Position_manager, trailing stop, P2.5, emergency_stop_check continuent normalement. Les exits ne sont pas affectés.

---

## Feature 5 — Re-entry Graduated + Shadow Portfolio

### Re-entry Graduated (par ticker)
Après un stop loss, le seuil de réentrée sur CE ticker est temporairement relevé (durée 4h) :

| Taille de perte | Seuil temporaire |
|---|---|
| < 5% | 1.7 |
| 5–10% | 1.9 |
| > 10% | 2.2 |

Stocké dans `trade_memory.json` sous `"reentry_thresholds"`. Compatible avec la Feature 3 : le 30min path respecte aussi ces seuils.

### Shadow Portfolio
Chaque token scanné mais **non acheté avec score ≥ 1.0** est enregistré comme near-miss :
```json
"shadow_portfolio": [
  {"ticker": "ABC", "score": 1.35, "prix_ref": 0.452, "type": "swing", "timestamp": "..."}
]
```

Limité à **15 near-misses actifs** (évite surcharge API).

Mesure des outcomes :
- Type "scalp" (score ≥ 2.0) → mesuré à **4h**
- Type "swing" (score < 2.0) → mesuré à **48h**

**Ajustement du seuil** : lent et gardé-fous :
- Max ±0.05 par semaine
- Requiert 20 near-misses consécutifs dans le même sens
- Ne descend jamais sous 1.6 en mode conservateur
- Ne descend jamais sous 1.8 en mode observation

**Bootstrap** : au premier cycle après déploiement, le scanner logge les tokens scorés entre 1.0 et le seuil courant comme near-misses initiaux, avec le prix actuel au moment du scan comme prix de référence et `timestamp = now`.

---

## Règles Transverses Consolidées

| Règle | Portée |
|---|---|
| Positions ≥ +3% protégées de la rotation | capital_allocator.py |
| Momentum filter : +15% en 72h → score plafonné 1.4 | scanner.py |
| Mode observation = exits actifs, entrées bloquées | position_manager.py + scanner.py |
| Sub-scores stockés bruts pour repondération rétroactive | ruflo_memory.py |

---

## Architecture Fichiers Impactés

| Fichier | Modifications |
|---|---|
| `ruflo_memory.py` | Sub-scores bruts, exit_quality, reentry_thresholds, shadow_portfolio, pending_signals |
| `scanner.py` | Momentum filter, stockage pending_signals, mode observation |
| `alert_scanner.py` | Nouveau chemin d'entrée scalp depuis pending_signals |
| `capital_allocator.py` | Protection rotation positions ≥ +3%, EV médiane rolling |
| `position_manager.py` | Mode observation gate, exit_quality au moment de la vente |
| `news_interpreter.py` | Fix timeout + poids ×2 |
| `trade_memory.json` | Nouvelles clés : pending_signals, shadow_portfolio, reentry_thresholds, ev_history |

---

## Ordre d'Implémentation Recommandé

1. **Feature 1** — News Interpreter (diagnostic + fix) : impact immédiat, effort minimal
2. **Feature 2** — Sub-scores + exit_quality : effort minimal, critique pour l'apprentissage futur
3. **Feature 4** — EV rolling + mode observation : protection immédiate contre les macro bear
4. **Feature 5** — Re-entry graduated + shadow portfolio : protection contre doubles pertes
5. **Feature 3** — Dual entry path scalp : plus complexe, à faire en dernier

---

## Critères de Succès

- Win rate ≥ 40% sur les 20 prochains trades
- EV/trade > 0 sur fenêtre glissante 15 trades
- Zéro double-perte sur même ticker dans la même journée
- Shadow portfolio : <30% des near-misses auraient été profitables (valide que le seuil est bien calibré)
