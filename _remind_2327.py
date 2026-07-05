"""6/18 8:30 國巨減半提醒"""
import json, requests
cfg = json.loads(open('telegram_config.json', encoding='utf-8').read())
body = """⏰ 30 分鐘後開盤 — 國巨 2327 減半

【記住：砍 500 股，不是 1000 股】

🎯 9:00 SOP 速記
A 開高 990+ → 限價 995 砍 500 股
B 開平 980-989 → 限價 985 砍 500 股
C 開低 970-979 → 限價 975 砍 500 股
D 開低 < 970 → 市價砍 500 股

⏰ 砍完後立刻
✓ 改 portfolio.csv 2327 從 1000 → 500
✓ 設盤中 trail stop 950
✓ 漲到 1050 → trail 上調 1000
✓ 漲到 1100 → trail 1050

🚫 不要做
✗ 一時心軟改砍 200 股（要砍 500）
✗ 凪不砍等 1001 回成本
✗ 加碼攤平

💪 進可攻退可守
最差 -34K  最好 +92K
"""
r = requests.post(f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
                  json={"chat_id": cfg["chat_id"], "text": body}, timeout=10)
print(f"提醒推播: {r.status_code}")
