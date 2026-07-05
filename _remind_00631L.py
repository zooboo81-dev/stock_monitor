"""7/16 8:30 提醒 — 00631L 槓桿 ETF 到期評估"""
import json, requests
from pathlib import Path

cfg = json.loads(Path('telegram_config.json').read_text(encoding='utf-8'))

# 讀當前 portfolio 確認還持有
pf = Path('portfolio.csv').read_text(encoding='utf-8')
if '00631L' not in pf:
    print("00631L 已不在持倉，跳過提醒")
    exit()

body = """⚠️ 00631L 元大台灣50正2 — 持有已 10 個交易日

【檢查點】
7/02 進場 38.17 × 6,000 股 = 229K

【槓桿 ETF 特性】
每日重設 → 長期複利衰減
持越久越虧（不管漲跌）

【今日必做】
1. 開儀表板看 00631L 現價
2. 若 > 40.5 → 停利落袋
3. 若 < 36.2 → 觸停損砍
4. 若在 36.2-40.5 區間
   → 評估大盤動能
   → 續多頭趨勢可再抱 3-5 日
   → 動能弱化就砍

【原則】
✗ 不要「習慣性」抱著
✓ 每 3 日重新評估
✓ 超過 20 日 = 一定砍

🎯 目標：抓短彈，不要凪"""
r = requests.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                  json={"chat_id": cfg["chat_id"], "text": body}, timeout=10)
print(f"提醒推播: {r.status_code}")
