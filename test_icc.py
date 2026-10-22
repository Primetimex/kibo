import sys, os, requests, pandas as pd
sys.path.insert(0, os.getcwd())
from structure_rules import classify_trend, check_entry_rules, detect_reversal, detect_indication, find_swings

r = requests.get('https://api.binance.us/api/v3/klines', params={'symbol':'BTCUSDT','interval':'5m','limit':80}, timeout=10)
data = r.json()
df = pd.DataFrame(data, columns=['timestamp','open','high','low','close','volume','close_time','quote_vol','trades','taker_base','taker_quote','ignore'])
for col in ['open','high','low','close','volume']:
    df[col] = df[col].astype(float)
try:
    trend = classify_trend(df)
    print('SUCCESS - BTC trend:', trend)
    sc = check_entry_rules(df, 'LONG')
    print('Entry rules:', sc)
except Exception as e:
    import traceback
    print('FAILED:', e)
    traceback.print_exc()
