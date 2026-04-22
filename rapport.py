"""
Module 5 — Générateur de Rapport Quotidien Markdown
Sauvegarde dans /rapports/YYYY-MM-DD.md
"""

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

import requests

import config

logger = logging.getLogger(__name__)


# ─── Données contexte macro ───────────────────────────────────────────────────

def fetch_fear_greed() -> dict:
    """Fear & Greed Index via alternative.me."""
    try:
        resp = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        entry = data["data"][0]
        return {
            "value": int(entry["value"]),
            "label": entry["value_classification"],
        }
    except Exception as e:
        logger.warning(f"Fear & Greed indisponible : {e}")
        return {"value": None, "label": "N/A"}


def fetch_btc_dominance() -> float | None:
    """Dominance BTC depuis CoinGecko."""
    try:
        resp = requests.get(
            f"{config.COINGECKO_BASE_URL}/global", timeout=10
        )
        resp.raise_for_status()
        dom = resp.json()["data"]["market_cap_percentage"].get("btc")
        return round(float(dom), 1) if dom else None
    except Exception as e:
        logger.warning(f"Dominance BTC indisponible : {e}")
        return None


# ─── Helpers de formatage ─────────────────────────────────────────────────────

def _pnl_emoji(pnl_pct: float | None) -> str:
    if pnl_pct is None:
        return "─"
    return "▲" if pnl_pct >= 0 else "▼"


def _score_bar(score: float) -> str:
    """Représentation visuelle du score [-3, +3]."""
    bars = {-3: "■■■□□□□", -2: "■■□□□□□", -1: "■□□□□□□",
            0: "□□□■□□□", 1: "□□□□□■□", 2: "□□□□■■□", 3: "□□□■■■■"}
    rounded = max(-3, min(3, round(score)))
    return bars.get(rounded, "□□□■□□□")


def _conviction_label(score: float) -> str:
    mapping = {
        range(-3, -1): ("EXIT / REDUCE", 5),
        range(-1, 1): ("HOLD", 2),
        range(1, 2): ("HOLD / ADD", 3),
        range(2, 4): ("BUY", 4),
    }
    for r, (label, conv) in mapping.items():
        if int(score) in r:
            return label, conv
    return "HOLD", 2


# ─── Sections du rapport ──────────────────────────────────────────────────────

def _section_header(today: date, fear_greed: dict, btc_dom: float | None) -> str:
    fg_val = fear_greed["value"]
    fg_label = fear_greed["label"]
    dom_str = f"{btc_dom}%" if btc_dom else "N/A"

    fg_str = f"{fg_val} — {fg_label}" if fg_val else "N/A"

    return f"""# Rapport Portefeuille — {today.strftime('%d %B %Y')}
*Généré le {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC*

---

## Contexte Macro

| Indicateur | Valeur |
|---|---|
| BTC Dominance | {dom_str} |
| Fear & Greed Index | {fg_str} |

"""


def _section_snapshot(snapshot: dict) -> str:
    positions = snapshot["positions"]
    total = snapshot["valeur_totale_usd"]
    pnl_abs = snapshot["pnl_total_absolu"]
    pnl_pct = snapshot["pnl_total_pct"]
    stable_pct = snapshot["stablecoin_pct"]

    sign = "+" if pnl_abs >= 0 else ""
    lines = [
        "## Snapshot Portefeuille\n",
        f"**Valeur totale :** ${total:,.2f}  ",
        f"**P&L total :** {sign}${pnl_abs:,.2f} ({sign}{pnl_pct:.2f}%)  ",
        f"**Liquidité stablecoins :** {stable_pct:.1f}%\n",
        "| Actif | Allocation | Prix actuel | P&L | Jours détenus |",
        "|---|---|---|---|---|",
    ]

    for pos in positions:
        pnl = pos["pnl_pct"]
        pnl_str = f"{_pnl_emoji(pnl)} {pnl:+.1f}%" if pnl is not None else "─"
        price_str = f"${pos['prix_actuel']:,.4f}" if pos["prix_actuel"] else "N/A"
        alloc = f"{pos['allocation_pct']:.1f}%" if pos["allocation_pct"] else "─"
        lines.append(
            f"| {pos['ticker']} | {alloc} | {price_str} | {pnl_str} | {pos['jours_detention']}j |"
        )

    return "\n".join(lines) + "\n\n"


def _section_risque(risk_data: dict) -> str:
    lines = ["## Analyse de Risque\n"]

    # VaR
    var = risk_data.get("var", {})
    if var:
        lines.append("### Value at Risk\n")
        lines.append("| Horizon | Confiance | VaR % | VaR USD | CVaR USD |")
        lines.append("|---|---|---|---|---|")
        for key, v in sorted(var.items()):
            parts = key.split("_")
            conf, horizon = parts[1], parts[2]
            lines.append(
                f"| {horizon} | {conf}% | {v['var_pct']:.2f}% | "
                f"${v['var_usd']:,.0f} | ${v['cvar_usd']:,.0f} |"
            )
        lines.append("")

    # Sharpe / Sortino
    perf = risk_data.get("performance_portefeuille", {})
    if perf:
        sharpe = perf.get("sharpe", "N/A")
        sortino = perf.get("sortino", "N/A")
        periode = perf.get("periode_jours", "─")
        lines.append(f"**Sharpe (90j) :** {sharpe}  |  **Sortino (90j) :** {sortino}  "
                     f"*(sur {periode} jours)*\n")

    # Corrélation
    avg_corr = risk_data.get("correlation_moyenne", None)
    if avg_corr is not None:
        alert_flag = " ⚠" if risk_data.get("correlation_alerte") else " ✓"
        lines.append(f"**Corrélation moyenne portefeuille :** {avg_corr:.2f}{alert_flag}\n")

    # Volatilités
    vols = risk_data.get("volatilites", {})
    if vols:
        lines.append("### Volatilité par actif\n")
        lines.append("| Actif | Vol 30j (ann.) | Vol 90j (ann.) |")
        lines.append("|---|---|---|")
        for ticker, v in vols.items():
            v30 = f"{v['vol_30j']*100:.0f}%" if v.get("vol_30j") else "─"
            v90 = f"{v['vol_90j']*100:.0f}%" if v.get("vol_90j") else "─"
            lines.append(f"| {ticker} | {v30} | {v90} |")
        lines.append("")

    # Drawdowns
    dd = risk_data.get("drawdowns", {})
    if dd:
        lines.append("### Drawdowns (depuis achat)\n")
        lines.append("| Actif | MDD | DD Actuel | Date Peak | Date Trough |")
        lines.append("|---|---|---|---|---|")
        for ticker, d in dd.items():
            if d:
                mdd = f"{d.get('max_drawdown_pct', '─'):.1f}%" if d.get("max_drawdown_pct") else "─"
                cur = f"{d.get('current_drawdown_pct', 0):.1f}%"
                lines.append(
                    f"| {ticker} | {mdd} | {cur} | "
                    f"{d.get('peak_date', '─')} | {d.get('trough_date', '─')} |"
                )
        lines.append("")

    return "\n".join(lines) + "\n"


def _section_signaux(technical_results: dict) -> str:
    lines = ["## Signaux Techniques\n"]
    lines.append("| Actif | Score | Verdict | RSI | MACD Histo | %B |")
    lines.append("|---|---|---|---|---|---|")

    for ticker, tech in technical_results.items():
        if "erreur" in tech:
            lines.append(f"| {ticker} | ─ | {tech['erreur']} | ─ | ─ | ─ |")
            continue

        sig = tech.get("signal", {})
        score = sig.get("score", 0)
        verdict = sig.get("verdict", "─")
        detail = sig.get("detail", {})
        bar = _score_bar(score)

        rsi = f"{detail.get('rsi', '─')}"
        macd_h = f"{detail.get('macd_histogram', '─')}"
        pct_b = f"{detail.get('bb_pct_b', '─')}"

        lines.append(f"| {ticker} | `{bar}` {score:+.1f} | {verdict} | {rsi} | {macd_h} | {pct_b} |")

    lines.append("")

    # Signaux détaillés
    lines.append("### Signaux par actif\n")
    for ticker, tech in technical_results.items():
        if "erreur" in tech:
            continue
        sig = tech.get("signal", {})
        signaux = sig.get("signaux", [])
        if signaux:
            lines.append(f"**{ticker}**")
            for s in signaux:
                lines.append(f"- {s}")
            niveaux = tech.get("niveaux", {})
            sup = tech.get("support_proche")
            res = tech.get("resistance_proche")
            if sup:
                lines.append(f"- Support proche : ${sup:,.4f}")
            if res:
                lines.append(f"- Résistance proche : ${res:,.4f}")
            lines.append("")

    return "\n".join(lines) + "\n"


def _section_recommandations(
    snapshot: dict,
    technical_results: dict,
    risk_results: dict,
) -> str:
    lines = ["## Recommandations d'Action\n"]

    # Trier par score technique descendant
    scored = []
    for pos in snapshot["positions"]:
        if pos["is_stablecoin"]:
            continue
        ticker = pos["ticker"]
        tech = technical_results.get(ticker, {})
        score = tech.get("signal", {}).get("score", 0)
        scored.append((ticker, score, pos, tech))

    scored.sort(key=lambda x: x[1], reverse=True)

    for ticker, score, pos, tech in scored:
        action, conviction = _conviction_label(score)
        atr = tech.get("atr_14")
        price = pos.get("prix_actuel") or 0
        sup = tech.get("support_proche")
        res = tech.get("resistance_proche")

        stop_str = f"${price - (atr * 2):,.4f}" if (atr and price) else "ATR N/A"
        tp_str = f"${price + (atr * 4):,.4f}" if (atr and price) else "N/A"

        lines.append(f"### {ticker} — **{action}** (conviction {conviction}/5)")
        lines.append(f"- Score technique : {score:+.1f}/3")
        lines.append(f"- Prix actuel : ${price:,.4f}")
        if atr:
            lines.append(f"- Stop suggéré (ATR×2) : {stop_str}")
            lines.append(f"- Target R/R 2:1 : {tp_str}")
        if sup:
            lines.append(f"- Support clé : ${sup:,.4f}")
        if res:
            lines.append(f"- Résistance clé : ${res:,.4f}")

        # Biais de confirmation (si signal très bullish, mentionner le risque)
        if score >= 2:
            lines.append(
                f"> ⚠️ **Biais de confirmation** : signal haussier fort — "
                f"valider le catalyst fondamental avant entrée."
            )
        lines.append("")

    return "\n".join(lines) + "\n"


def _section_alertes(all_alerts: list[str]) -> str:
    if not all_alerts:
        return "## Alertes\n\n*Aucune alerte active.*\n\n"
    lines = [f"## Alertes ({len(all_alerts)})\n"]
    for a in all_alerts:
        lines.append(f"- ⚠ {a}")
    return "\n".join(lines) + "\n\n"


# ─── Assemblage du rapport ────────────────────────────────────────────────────

def generate_report(
    snapshot: dict,
    risk_data: dict,
    technical_results: dict,
    risk_management_results: dict,
) -> str:
    """Assemble le rapport Markdown complet."""

    today = date.today()

    # Données macro
    fear_greed = fetch_fear_greed()
    time.sleep(0.5)
    btc_dom = fetch_btc_dominance()

    # Toutes les alertes
    all_alerts = risk_management_results.get("alertes_globales", [])

    report = ""
    report += _section_header(today, fear_greed, btc_dom)
    report += _section_alertes(all_alerts)
    report += _section_snapshot(snapshot)
    report += _section_risque(risk_data)
    report += _section_signaux(technical_results)
    report += _section_recommandations(snapshot, technical_results, risk_management_results)

    report += "---\n*Rapport généré automatiquement — pas un conseil en investissement.*\n"

    return report


def save_report(content: str, rapports_dir: str = config.RAPPORTS_DIR) -> str:
    """Sauvegarde le rapport dans /rapports/YYYY-MM-DD.md."""
    Path(rapports_dir).mkdir(parents=True, exist_ok=True)
    filename = f"{date.today().isoformat()}.md"
    filepath = Path(rapports_dir) / filename
    filepath.write_text(content, encoding="utf-8")
    logger.info(f"Rapport sauvegardé : {filepath}")
    return str(filepath)


def run(snapshot, risk_data, technical_results, risk_management_results) -> str:
    content = generate_report(snapshot, risk_data, technical_results, risk_management_results)
    path = save_report(content)
    return path
