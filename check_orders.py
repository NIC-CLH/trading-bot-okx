import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import okx_client as okx

b = okx.get_balances()
print("Balances:", b)

fills = okx._get('/api/v5/trade/fills', {'instType': 'SPOT', 'limit': '10'})
print("\nDerniers trades executes sur OKX:")
for f in fills:
    from datetime import datetime
    ts = int(f.get("ts", 0)) // 1000
    dt = datetime.utcfromtimestamp(ts).strftime('%d/%m %H:%M')
    print(f"  {dt} | {f.get('instId')} | {f.get('side')} | qty={f.get('fillSz')} @ {f.get('fillPx')}")
