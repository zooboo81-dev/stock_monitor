# ☁️ 雲端部署指南（GitHub Actions + Streamlit Cloud）

## 📅 排程時間對照（UTC ↔ 台灣）

| 工作 | 台灣時間 | UTC cron | 週幾 |
|---|---|---|---|
| Morning Briefing | 08:50 | `50 0 * * 1-5` | 週一至五 |
| Intraday Alert | 09:00-13:35 每 5 分 | `*/5 1-5 * * 1-5` | 週一至五 |
| TAIEX MA Tracker | 14:00 | `0 6 * * 1-5` | 週一至五 |
| Watchlist Check | 14:40 | `40 6 * * 1-5` | 週一至五 |
| Recs Snapshot | 17:30 | `30 9 * * 1-5` | 週一至五 |

⚠️ GitHub Actions cron 可能延遲 5-15 分鐘（免費帳號）

---

## 🔐 GitHub Secrets 設定（可選）

如果保留 Telegram 為緊急推播：

1. Repo → Settings → Secrets and variables → Actions
2. New repository secret：
   - `TELEGRAM_BOT_TOKEN` = 你的 bot token
   - `TELEGRAM_CHAT_ID` = 你的 chat id

**現在 telegram_notify.py 改成寫入 notifications.jsonl，不需要 Telegram Secrets**

---

## 🚀 Streamlit Cloud 部署

### Step 1：註冊
1. 開 https://share.streamlit.io
2. **Sign in with GitHub**
3. 授權存取你的 repo

### Step 2：新增 App
1. 點 **New app**
2. 填：
   - Repository：`your-username/stock-monitor`
   - Branch：`main`
   - Main file path：`app.py`
   - App URL：自訂如 `zooboo-stocks`
3. 點 **Deploy!**

### Step 3：Secrets（如果 app 需要）
1. App 頁面右下 **⋮** → **Settings**
2. Secrets 分頁
3. 貼上（TOML 格式）：
   ```toml
   # 目前 app.py 不需要 Secrets
   # 未來要保留 Telegram 才需要
   ```

### Step 4：完成！
- 你的儀表板：`https://zooboo-stocks.streamlit.app`
- 加到手機主畫面

---

## 🔍 監控 GitHub Actions 執行

### 看排程有沒有跑成功
1. Repo → **Actions** tab
2. 點任何 workflow → 看紅綠燈
3. ✅ 綠 = 成功、❌ 紅 = 失敗

### 手動觸發（測試用）
1. Actions → 選 workflow
2. 右邊 **Run workflow** → 選 branch → **Run workflow**

---

## ⚠️ 已知限制

### 1. TXF 記分板無法在雲端跑
- Shioaji SDK 需要本機 IP 授權
- **替代方案**：用 yfinance ^TWII 取代（但期貨訊號會不準）
- **保留本機**：桌機跑 scoreboard.py，push 圖檔到 GitHub

### 2. GitHub Actions 免費 tier
- **公開 repo**：無限
- **私人 repo**：2000 分鐘/月（用不到 300 分鐘）
- 每次跑約 1-2 分鐘

### 3. Streamlit Cloud 免費 tier
- App 無限個，都免費
- 但 **1 GB RAM** 上限
- 太多同時使用者會慢

---

## 📋 遷移檢查清單

- [x] .gitignore 排除機密檔
- [x] requirements.txt（純雲端相容）
- [x] 5 個 workflow yml 建好
- [x] telegram_notify.py 改寫成寫入 notifications.jsonl
- [ ] Commit + Push（GitHub Desktop）
- [ ] Streamlit Cloud 部署
- [ ] 手動觸發第一個 workflow 測試
- [ ] 確認 notifications.jsonl 被更新
- [ ] 手機瀏覽器加到主畫面
