import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
import logging
logging.basicConfig(level=logging.WARNING)

import okx_client as okx
import technical_signals as ts
import alertes

balances = okx.get_balances()
usdc = balances.get("USDC", 0)
print(f"USDC disponible : ${usdc:.2f}")

# Garde 15$ de réserve frais
montant = round(usdc - 15, 2)
print(f"Montant à déployer sur TIA : ${montant:.2f}")

ohlcv = okx.get_all_ohlcv(["TIA"], days=90)
tech = ts.run(ohlcv)
t = tech.get("TIA", {})
prix = t.get("prix_actuel", 0)
stop = t.get("stop_proche")
target = t.get("target_proche")
atr = t.get("atr_14", 0)
if not stop: stop = round(prix - atr * 2, 6)
if not target: target = round(prix + abs(prix - stop) * 2, 6)

print(f"TIA : prix=${prix:.4f} | stop=${stop:.4f} | target=${target:.4f}")
print(f"Score : {t.get('signal',{}).get('score',0):+.2f}")

result = okx.place_order("TIA", "buy", usdt_amount=montant, order_type="market",
                          stop_loss=stop, take_profit=target)
ordre_id = result.get("ordId", "?")
print(f"Ordre TIA : {ordre_id}")

alertes.send(
    f"🛒 *Achat TIA* (déploiement USDC)\n"
    f"Montant : `${montant:.2f} USDC`\n"
    f"Prix : `${prix:.4f}`\n"
    f"🛡 Stop : `${stop:.4f}`\n"
    f"🎯 Target : `${target:.4f}`\n"
    f"ID : `{ordre_id}`"
)

b2 = okx.get_balances()
print(f"\nBalances après : {b2}")
