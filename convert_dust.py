"""
Script de conversion des poussières (dust) en USDC via OKX Easy Convert.
Exécution manuelle : python convert_dust.py
"""
import logging
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

import okx_client as okx

DUST_THRESHOLD_USD = 1.0  # Tout ce qui vaut moins de $1


def print_manual_instructions(tickers: list = None):
    tickers_str = f" ({', '.join(tickers)})" if tickers else ""
    print(
        f"\n💡 Pour convertir manuellement{tickers_str} :\n"
        "   OKX App : Assets → icône engrenage ⚙ → 'Convertir les petits actifs'\n"
        "   OKX Web : https://my.okx.com/balance/spot\n"
        "             → Colonne de droite → 'Convert Small Balances' ou filtre 'Dust'"
    )


def main():
    print("\n=== Conversion poussières → USDC ===\n")

    # 1. Récupérer toutes les balances
    try:
        balances = okx.get_balances()
    except Exception as e:
        print(f"❌ Impossible de récupérer les balances : {e}")
        return

    stablecoins = {"USDC", "USDT", "BUSD", "DAI"}

    # 2. Identifier les poussières
    dust_tickers = []
    print("Analyse des balances :")
    for ticker, qty in balances.items():
        if ticker in stablecoins:
            continue
        price = okx.get_price_usdc(ticker)
        if price is None:
            print(f"  ⚠️  {ticker}: {qty:.6f} — prix inconnu, ignoré")
            continue
        value_usd = qty * price
        if value_usd < DUST_THRESHOLD_USD:
            dust_tickers.append(ticker)
            print(f"  🧹 {ticker}: {qty:.6f} = ${value_usd:.4f} → POUSSIÈRE")
        else:
            print(f"  ✅ {ticker}: {qty:.6f} = ${value_usd:.2f} → position normale")

    if not dust_tickers:
        print("\n✅ Aucune poussière détectée.")
        return

    print(f"\nPoussières à convertir : {dust_tickers}")

    # Vérifier les options disponibles
    from_ccys, to_ccys = okx.get_easy_convert_list()
    print(f"Easy Convert disponible pour : {from_ccys}")
    print(f"Cibles disponibles : {to_ccys}")

    # Choisir la cible : BTC si disponible (position existante)
    target = "BTC" if "BTC" in to_ccys else (to_ccys[0] if to_ccys else None)
    if not target:
        print("❌ Easy Convert non disponible sur ce compte OKX.")
        print_manual_instructions()
        return

    print(f"\nConversion → {target} (position existante)...")
    result = okx.convert_dust(dust_tickers, to_ccy=target)

    print("\n=== Résultat ===")
    if result["converted"]:
        print(f"✅ Convertis en {result['to_ccy']} : {result['converted']}")
    if result["errors"]:
        print(f"❌ Échec API : {result['errors']}")
        print_manual_instructions(result["errors"])

    # 4. Balance finale
    try:
        final = okx.get_balances()
        usdc = final.get("USDC", 0) + final.get("USDT", 0)
        print(f"\n💵 USDC final : ${usdc:.2f}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
