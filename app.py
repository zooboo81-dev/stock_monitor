"""股票即時監看儀表板
資料源：TWSE MIS（即時報價 ~5-20 秒延遲）+ yfinance（歷史 MA/RSI/量比）
訊號：單日跌幅、跌破均線、停利停損、爆量、RSI、大盤
通知：Windows Toast（僅新增的紅色訊號）
"""
from __future__ import annotations

# Python 3.14 SSL 對缺 SKI 的憑證會擋；改用 Windows 原生憑證庫
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import os
import urllib.parse
from datetime import datetime, time, timedelta
from pathlib import Path

import feedparser
import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from streamlit_autorefresh import st_autorefresh

from analysis import smart_money_score
from backtest import exit_reason_breakdown, run_portfolio_backtest, verdict_text as backtest_verdict
from chart import make_chart
from discover import top_picks
from events import portfolio_earnings_calendar, upcoming_events
from hot_movers import candidate_universe, find_hot_movers
from institutional import analyze as analyze_instit
from institutional import fetch_institutional_history
from macro import detect_market_regime, fetch_macro, fetch_night, macro_risk_score, macro_verdict
from sector import fetch_sector_indices

# 類別固定順序（影響顯示排序）
CATEGORY_ORDER = ["指數ETF", "半導體", "IC設計", "AI伺服器", "零組件", "傳產"]
CATEGORY_COLOR = {
    "指數ETF": "#e8f0fe",
    "半導體": "#fde7e9",
    "IC設計": "#f3e8fd",
    "AI伺服器": "#e6f4ea",
    "零組件": "#fff4e5",
    "傳產": "#f1f3f4",
}

try:
    from winotify import Notification, audio
    HAS_NOTIFY = True
except Exception:
    HAS_NOTIFY = False

# ──────────────── Config ────────────────
PORTFOLIO_FILE = Path(__file__).parent / "portfolio.csv"
REGIME_STATE_FILE = Path(__file__).parent / ".last_regime.json"
REFRESH_SEC = 30

THRESH = {
    "daily_drop": -5.0,        # 單日跌幅 ≥ 5%
    "near_limit_down": -9.0,   # 接近跌停
    "stop_profit": 30.0,       # 個人停利 +30%
    "stop_loss": -8.0,         # 個人停損 -8%
    "rsi_overbought": 80,
    "rsi_oversold": 20,
    "vol_spike_ratio": 2.0,    # 量比 ≥ 2x 5 日均量
    "index_drop": -2.0,        # 大盤單日跌幅
}

# 台股交易成本（券商顯示的「未實現損益」就是扣完這些之後的淨值）
FEES = {
    "tax_stock": 0.003,        # 一般股票證交稅 0.3%（賣出時）
    "tax_etf": 0.001,          # ETF 證交稅 0.1%（賣出時）
    "broker_fee": 0.001425,    # 證券商手續費 0.1425%（買/賣各一次，無折扣）
}


def is_etf(code: str) -> bool:
    """台股 ETF 代號多為 00 開頭（0050, 0056, 006208, 00878 ...）"""
    return code.startswith("00")


def sell_proceeds(price: float, shares: int, code: str) -> float:
    """賣出後實得淨額（扣稅+手續費），即券商畫面的「帳面收入」"""
    tax_rate = FEES["tax_etf"] if is_etf(code) else FEES["tax_stock"]
    return price * shares * (1 - tax_rate - FEES["broker_fee"])


def buy_total(price: float, shares: int) -> float:
    """買進總成本（券商畫面通常不含買進手續費，只是均價×股數）"""
    return price * shares


def taipei_now() -> datetime:
    return datetime.utcnow() + timedelta(hours=8)


def is_trading() -> bool:
    n = taipei_now()
    return n.weekday() < 5 and time(9, 0) <= n.time() <= time(13, 30)


# ──────────────── HTTP session（TWSE 需要 referer/cookie） ────────────────
@st.cache_resource
def twse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://mis.twse.com.tw/stock/index.jsp",
    })
    try:
        s.get("https://mis.twse.com.tw/stock/index.jsp", timeout=5)
    except Exception:
        pass
    return s


# ──────────────── 即時報價 ────────────────
@st.cache_data(ttl=5)
def fetch_realtime_quotes(codes_with_market: list[tuple[str, str]]) -> dict[str, dict]:
    if not codes_with_market:
        return {}
    ex_ch = "|".join(
        f"{'tse' if m == 'TSE' else 'otc'}_{c}.tw" for c, m in codes_with_market
    )
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ex_ch}&json=1&delay=0&_={int(datetime.utcnow().timestamp() * 1000)}"
    out: dict[str, dict] = {}
    try:
        r = twse_session().get(url, timeout=8)
        for d in r.json().get("msgArray", []):
            code = d.get("c")
            try:
                z = d.get("z")
                y = float(d.get("y") or 0)
                last = float(z) if z not in (None, "-", "") else y
                vol = int(d.get("v") or 0)
            except (ValueError, TypeError):
                continue
            if not last or not y:
                continue
            out[code] = {
                "last": last,
                "yclose": y,
                "change_pct": (last - y) / y * 100,
                "volume": vol,
            }
    except Exception as e:
        st.warning(f"TWSE 即時報價失敗：{e}")
    return out


@st.cache_data(ttl=5)
def fetch_index() -> dict | None:
    """加權指數 t00"""
    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0"
    try:
        r = twse_session().get(url, timeout=5)
        data = r.json().get("msgArray", [])
        if not data:
            return None
        d = data[0]
        last = float(d.get("z") or 0)
        y = float(d.get("y") or 0)
        if last and y:
            return {"last": last, "yclose": y, "change_pct": (last - y) / y * 100}
    except Exception:
        pass
    return None


# ──────────────── 歷史資料：MA / RSI / 量比 ────────────────
@st.cache_data(ttl=3600)
def fetch_history(code: str, market: str) -> pd.DataFrame:
    suffix = "TW" if market == "TSE" else "TWO"
    try:
        return yf.Ticker(f"{code}.{suffix}").history(period="6mo", auto_adjust=False)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=900)
def fetch_macro_cached() -> dict:
    """總經指標（美股盤後資料，15 分快取）"""
    try:
        return fetch_macro()
    except Exception as e:
        st.warning(f"總經資料抓取失敗：{e}")
        return {}


@st.cache_data(ttl=3600)
def fetch_hot_movers_cached(_instit_by_code: dict, lookback: int = 5, min_gain: float = 20.0) -> list:
    """飆股觀察（近 N 日漲幅 ≥ X%），1 小時快取"""
    try:
        universe = candidate_universe(_instit_by_code, min_lots_abs=200)
        return find_hot_movers(universe, lookback_days=lookback,
                              min_gain_pct=min_gain, top_n=15)
    except Exception as e:
        st.warning(f"飆股掃描失敗：{e}")
        return []


@st.cache_data(ttl=1800)
def fetch_sectors_cached() -> tuple[list, object]:
    """台股分類指數（每日收盤後資料，30 分快取）"""
    try:
        return fetch_sector_indices(twse_session())
    except Exception as e:
        st.warning(f"分類指數抓取失敗：{e}")
        return [], None


@st.cache_data(ttl=43200)
def fetch_market_regime_cached() -> dict:
    """市場狀態（多頭/警戒/熊市），12 小時快取"""
    try:
        taiex = yf.Ticker("^TWII").history(period="2y", auto_adjust=False)
        return detect_market_regime(taiex)
    except Exception as e:
        return {"regime": "unknown", "emoji": "⚪", "label": "判斷失敗",
                "advice": str(e), "banner_type": None}


@st.cache_data(ttl=120)
def fetch_night_cached() -> dict:
    """夜盤指標（美股期貨即時，2 分快取）"""
    try:
        return fetch_night()
    except Exception as e:
        st.warning(f"夜盤資料抓取失敗：{e}")
        return {}


@st.cache_data(ttl=86400)
def fetch_history_2y(codes_market_tuple: tuple) -> dict:
    """抓 2 年歷史（給回測用，每日快取）"""
    out = {}
    for code, market in codes_market_tuple:
        suffix = "TW" if market == "TSE" else "TWO"
        try:
            h = yf.Ticker(f"{code}.{suffix}").history(period="2y", auto_adjust=False)
            if len(h) > 100:
                out[code] = h
        except Exception:
            continue
    return out


@st.cache_data(ttl=300)
def fetch_top_picks_cached(portfolio_codes_tuple: tuple, _instit_by_code: dict) -> tuple[list, str]:
    """每日推薦標的。5 分鐘快取。優先讀盤後 14:35 排程算好的檔，秒開不卡。
    回傳 (推薦清單, 計算時間)。
    """
    import datetime as _dt
    from pathlib import Path as _P
    import json as _json

    # 1) 優先讀檔（recs_history.jsonl 排程已算好）
    hist_file = _P("data/recs_history.jsonl")
    today_iso = _dt.date.today().isoformat()
    if hist_file.exists():
        try:
            today_picks = []
            for line in hist_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("snapshot_date") == today_iso and r.get("code") not in portfolio_codes_tuple:
                    # 轉成 top_picks 格式
                    today_picks.append({
                        "code": r["code"], "name": r.get("name", ""),
                        "score": r["score"], "verdict": "偏多（可逢低加碼）",
                        "verdict_emoji": "🟢",
                        "attention_level": 1 if r["score"] >= 4 else 0,
                        "attention_reasons": [],
                        "breakdown": [],
                        "top_reasons": [],
                        "price": r.get("entry_price", 0),
                        "chg_pct": 0,
                        "instit": None,
                        "backtest_ev": r.get("backtest_ev", 0),
                        "backtest_n": r.get("backtest_n", 0),
                        "backtest_wr": r.get("backtest_wr", 0),
                        "atr14": r.get("atr14"),  # 7/06 新增
                        "dim_scores": r.get("dim_scores"),   # 優化 B
                        "strategy": {                        # 優化 C
                            "type": r.get("strategy_type"),
                            "label": r.get("strategy_label"),
                        } if r.get("strategy_type") else None,
                    })
            if today_picks:
                # 排序：評分 → 勝率 → EV
                def _wr(p):
                    w = p.get("backtest_wr", 0)
                    return w * 100 if w < 1 else w
                today_picks.sort(key=lambda x: (-x["score"], -_wr(x), -(x.get("backtest_ev") or 0)))
                today_picks = today_picks[:5]

                # ★ 即時補上真實 price + chg_pct（snapshot 的 entry_price 是昨日收盤，stale）
                try:
                    _codes = [p["code"] for p in today_picks]
                    _sess = twse_session()
                    _ex_ch = "|".join([f"tse_{c}.tw" for c in _codes] + [f"otc_{c}.tw" for c in _codes])
                    _url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={_ex_ch}&json=1&delay=0"
                    _r = _sess.get(_url, timeout=8)
                    _quotes = {}
                    for _d in _r.json().get("msgArray", []):
                        _c = _d.get("c")
                        if not _c or _c in _quotes:
                            continue
                        _z = _d.get("z"); _y = _d.get("y")
                        try:
                            _px = float(_z) if _z not in (None, "-", "") else float(_y or 0)
                            _ycl = float(_y) if _y not in (None, "-", "") else _px
                            if _px > 0:
                                _quotes[_c] = (_px, (_px - _ycl) / _ycl * 100 if _ycl else 0)
                        except (ValueError, TypeError):
                            continue
                    for _p in today_picks:
                        if _p["code"] in _quotes:
                            _px, _chg = _quotes[_p["code"]]
                            _p["price"] = _px
                            _p["chg_pct"] = _chg
                except Exception:
                    pass  # 失敗就用 snapshot 的 stale 值

                return today_picks, f"{today_iso}（即時報價）"
        except Exception:
            pass

    # 2) Fallback：即時跑 discover（首次或檔案沒今天資料）
    def _taiwan_now_str():
        # 雲端 server 是 UTC，需要 +8 轉台灣時間
        return (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime("%H:%M:%S")

    # ★ 7/06 修：雲端偵測 — 雲端跳過即時計算（會卡住轉圈）
    # 用 OS 判斷最可靠：桌機=Windows、Streamlit Cloud=Linux
    import platform as _pf
    _is_cloud = _pf.system() != "Windows"
    if _is_cloud:
        return [], _taiwan_now_str()  # 雲端直接回空，避免卡死

    try:
        picks = top_picks(
            set(portfolio_codes_tuple),
            _instit_by_code,
            twse_session(),
            top_n=5,
        )
        return picks, _taiwan_now_str()
    except Exception as e:
        st.warning(f"推薦標的計算失敗：{e}")
        return [], _taiwan_now_str()


@st.cache_data(ttl=1800)
def fetch_institutional_all() -> tuple[dict, list]:
    """三大法人最近 10 日資料（30 分快取，盤後 17:00 才會更新）"""
    try:
        by_code, dates = fetch_institutional_history(twse_session(), days=10)
        return by_code, dates
    except Exception as e:
        st.warning(f"三大法人資料抓取失敗：{e}")
        return {}, []


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - 100 / (1 + rs)


def compute_indicators(hist: pd.DataFrame) -> dict:
    if hist.empty:
        return {}
    # yfinance 有時最後一根 K 為 NaN（盤中資料未結算），先濾掉
    h = hist.dropna(subset=["Close"])
    if len(h) < 20:
        return {}
    close = h["Close"]
    vol = h["Volume"]
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1] if len(close) >= 60 else None
    rsi_val = rsi(close).iloc[-1]
    vol_avg5 = vol.rolling(5).mean().iloc[-1]
    return {
        "ma20": float(ma20) if pd.notna(ma20) else None,
        "ma60": float(ma60) if ma60 is not None and pd.notna(ma60) else None,
        "rsi": float(rsi_val) if pd.notna(rsi_val) else None,
        "vol_avg5": float(vol_avg5) if pd.notna(vol_avg5) else None,
    }


# ──────────────── 訊號 ────────────────
def check_signals(row, ind: dict, q: dict) -> list[tuple[str, str]]:
    sigs: list[tuple[str, str]] = []
    chg = q["change_pct"]
    last = q["last"]
    pnl_pct = (last - row["cost"]) / row["cost"] * 100

    if chg <= THRESH["near_limit_down"]:
        sigs.append(("🔴", f"接近跌停 {chg:+.2f}%"))
    elif chg <= THRESH["daily_drop"]:
        sigs.append(("🔴", f"單日大跌 {chg:+.2f}%"))

    if ind.get("ma20") and last < ind["ma20"]:
        sigs.append(("🟡", f"跌破 20MA ({ind['ma20']:.1f})"))
    if ind.get("ma60") and last < ind["ma60"]:
        sigs.append(("🟡", f"跌破 60MA ({ind['ma60']:.1f})"))

    if pnl_pct >= THRESH["stop_profit"]:
        sigs.append(("🟢", f"達停利 {pnl_pct:+.1f}%"))
    if pnl_pct <= THRESH["stop_loss"]:
        sigs.append(("🔴", f"觸停損 {pnl_pct:+.1f}%"))

    va5 = ind.get("vol_avg5")
    if va5 and q["volume"] >= va5 * THRESH["vol_spike_ratio"]:
        sigs.append(("🟡", f"爆量 {q['volume'] / va5:.1f}x"))

    r = ind.get("rsi")
    if pd.notna(r):
        if r >= THRESH["rsi_overbought"]:
            sigs.append(("🟡", f"RSI 過熱 {r:.0f}"))
        elif r <= THRESH["rsi_oversold"]:
            sigs.append(("🟡", f"RSI 超賣 {r:.0f}"))

    return sigs


# ──────────────── 桌面通知 ────────────────
def desktop_notify(title: str, msg: str) -> None:
    if not HAS_NOTIFY:
        return
    try:
        toast = Notification(
            app_id="股票即時監看",
            title=title,
            msg=msg,
            duration="short",
        )
        toast.set_audio(audio.Default, loop=False)
        toast.show()
    except Exception:
        pass


# ──────────────── 新聞 ────────────────
@st.cache_data(ttl=600)
def fetch_news(query: str, limit: int = 3) -> list[dict]:
    q = urllib.parse.quote(f"{query} 股")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        feed = feedparser.parse(url)
        return [
            {"title": e.title, "link": e.link, "published": e.get("published", "")}
            for e in feed.entries[:limit]
        ]
    except Exception:
        return []


# ──────────────── UI ────────────────
st.set_page_config(page_title="股票即時監看", page_icon="📈", layout="wide")
st.title("📈 股票即時監看")

st_autorefresh(interval=REFRESH_SEC * 1000, key="autorefresh")

if not PORTFOLIO_FILE.exists():
    st.error(f"找不到 {PORTFOLIO_FILE}")
    st.stop()

raw_df = pd.read_csv(PORTFOLIO_FILE, dtype={"code": str})

# ──────────────── 側邊欄（先渲染，讓本次刷新就生效） ────────────────
with st.sidebar:
    st.header("💰 交易成本")
    st.caption("券商手續費若有折扣請調整（影響淨損益計算）")
    fee_pct = st.number_input("手續費率 (%)", value=FEES["broker_fee"] * 100,
                              step=0.005, format="%.4f")
    FEES["broker_fee"] = fee_pct / 100
    st.caption("• 一般股票證交稅：0.3%（賣出）")
    st.caption("• ETF 證交稅：0.1%（賣出）")

    st.divider()
    st.header("⚙️ 訊號閾值")
    THRESH["daily_drop"] = st.number_input("單日跌幅警示 (%)", value=THRESH["daily_drop"], step=0.5)
    THRESH["stop_profit"] = st.number_input("停利線 (%)", value=THRESH["stop_profit"], step=1.0)
    THRESH["stop_loss"] = st.number_input("停損線 (%)", value=THRESH["stop_loss"], step=0.5)
    THRESH["vol_spike_ratio"] = st.number_input("爆量倍率", value=THRESH["vol_spike_ratio"], step=0.5)
    THRESH["rsi_overbought"] = st.number_input("RSI 過熱", value=int(THRESH["rsi_overbought"]), step=5)
    THRESH["rsi_oversold"] = st.number_input("RSI 超賣", value=int(THRESH["rsi_oversold"]), step=5)
    THRESH["index_drop"] = st.number_input("大盤警示 (%)", value=THRESH["index_drop"], step=0.5)

    st.divider()
    st.header("📱 顯示模式")
    LITE_MODE = st.toggle(
        "🚀 極簡模式（手機推薦）",
        value=False,
        help="關掉所有 K 線圖、回測圖、新聞 — 載入速度快 5 倍",
    )
    st.caption("極簡模式只保留：總資產、TXF 訊號、加碼建議、推薦榜、持倉表")

    st.divider()
    st.caption(f"自動刷新：每 {REFRESH_SEC} 秒")
    st.caption(f"桌面通知：{'✅ 已啟用' if HAS_NOTIFY else '❌ winotify 未安裝'}")
    st.caption(f"持倉檔：{PORTFOLIO_FILE.name}")

# 狀態列
c_time, c_status, c_btn = st.columns([2, 2, 1])
c_time.caption(f"🕒 {taipei_now().strftime('%Y-%m-%d %H:%M:%S')} (台北)")
c_status.caption("🟢 盤中即時" if is_trading() else "⚪ 非交易時段（顯示盤後或上次收盤）")
if c_btn.button("🔄 立即刷新"):
    st.cache_data.clear()
    st.rerun()


# ──────────────── 🔔 通知中心 ────────────────
def _load_notifications():
    from pathlib import Path as _P
    import json as _json
    from datetime import datetime as _dtn, timedelta as _tdn
    nf = _P("data/notifications.jsonl")
    if not nf.exists():
        return []
    # 7/07 升級：支援未來排程通知，只顯示 ts <= 台灣時間現在
    _tw_now = (_dtn.utcnow() + _tdn(hours=8)).isoformat(timespec="seconds")
    out = []
    for line in nf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
            # 未來排程通知（ts > 現在）先隱藏，時間到才顯示
            if rec.get("ts", "") > _tw_now:
                continue
            out.append(rec)
        except Exception:
            continue
    return sorted(out, key=lambda x: x.get("ts", ""), reverse=True)


def _save_notifications(records):
    from pathlib import Path as _P
    import json as _json
    nf = _P("data/notifications.jsonl")
    nf.write_text(
        "\n".join(_json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


_all_notifs = _load_notifications()
_unread = [n for n in _all_notifs if not n.get("read", False)]
_recent_24h = []
try:
    import datetime as _dt
    _cutoff = _dt.datetime.now() - _dt.timedelta(hours=24)
    for _n in _all_notifs:
        try:
            _nts = _dt.datetime.fromisoformat(_n.get("ts", ""))
            if _nts >= _cutoff:
                _recent_24h.append(_n)
        except Exception:
            pass
except Exception:
    pass

# 頂部：未讀提示 + 展開按鈕
if _unread:
    with st.container():
        _cn1, _cn2 = st.columns([5, 1])
        _cn1.markdown(
            f"<div style='background:#fff3e0; border-left:6px solid #ff8f00; "
            f"padding:8px 14px; border-radius:6px; color:#1a1a1a; font-size:13px;'>"
            f"🔔 <b>{len(_unread)} 則未讀通知</b>　"
            f"<span style='color:#666'>最新：{_unread[0].get('title', '')} "
            f"（{_unread[0].get('ts', '')[-8:]}）</span>"
            f"</div>",
            unsafe_allow_html=True,
        )
        if _cn2.button("✅ 全部標為已讀", key="mark_all_read"):
            for _n in _all_notifs:
                _n["read"] = True
            _save_notifications(_all_notifs)
            st.rerun()

# 折疊區顯示所有通知
with st.expander(
    f"🔔 通知中心（{len(_all_notifs)} 則 / {len(_unread)} 未讀）— 點開查看",
    expanded=False,
):
    if not _all_notifs:
        st.caption("目前沒有通知")
    else:
        # 分未讀/已讀顯示
        _to_show = _unread if _unread else _all_notifs[:20]
        for _n in _to_show[:30]:
            _read = _n.get("read", False)
            _bg = "#f5f5f5" if _read else "#fff3e0"
            _border = "#bbb" if _read else "#ff8f00"
            _title = _n.get("title", "")
            _body = _n.get("body", "").replace("\n", "<br>")
            _ts = _n.get("ts", "")
            st.markdown(
                f"<div style='background:{_bg}; border-left:4px solid {_border}; "
                f"padding:8px 12px; border-radius:4px; margin-bottom:6px; "
                f"color:#1a1a1a; font-size:12px;'>"
                f"<div style='display:flex; justify-content:space-between'>"
                f"<b>{'✅ ' if _read else '🔔 '}{_title}</b>"
                f"<span style='color:#666; font-size:11px'>{_ts}</span>"
                f"</div>"
                f"<div style='margin-top:4px; color:#333'>{_body}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
        if len(_all_notifs) > 30:
            st.caption(f"（僅顯示最新 30 則，共 {len(_all_notifs)} 則）")

# 未讀 → 用 st.toast 彈提醒（Streamlit 1.28+）
try:
    if _unread and len(_unread) <= 3:
        for _n in _unread[:3]:
            st.toast(f"🔔 {_n.get('title', '')}", icon="🔔")
except Exception:
    pass


# ──────────────── TXF 訊號 + 總資產 BANNER ────────────────
def _read_txf_signal():
    """讀台指期 scoreboard log 最新訊號（訊號優先）"""
    from pathlib import Path as _P
    import re as _re
    txf_log = _P("C:/Users/zoobo/txf-backtest/data/scoreboard_log.txt")
    if not txf_log.exists():
        return None
    pat = _re.compile(r"(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}\s+資料(\d{4}-\d{2}-\d{2})\s+(【[^】]+】)\s+收([\d,]+)")
    priority = {"【做多訊號】": 3, "【做空訊號】": 3, "【出場訊號】": 3,
                "【偏多】": 1, "【偏空】": 1, "【觀望】": 1}
    by_date = {}
    try:
        for line in txf_log.read_text(encoding="utf-8").splitlines():
            m = pat.match(line)
            if not m: continue
            data_date = m.group(2); verdict = m.group(3); close = m.group(4).replace(",", "")
            prio = priority.get(verdict, 0)
            existing = by_date.get(data_date)
            if existing is None or prio >= existing[2]:
                by_date[data_date] = (verdict, close, prio)
        if not by_date:
            return None
        latest = max(by_date.keys())
        v, c, _ = by_date[latest]
        return {"date": latest, "verdict": v, "close": int(c)}
    except Exception:
        return None


def _read_cash():
    """讀現金檔"""
    from pathlib import Path as _P
    import json as _json
    f = _P("data/cash.json")
    if not f.exists():
        return 0, None
    try:
        d = _json.loads(f.read_text(encoding="utf-8"))
        return int(d.get("cash_twd", 0)), d.get("last_updated", "")
    except Exception:
        return 0, None


# TXF banner
_txf = _read_txf_signal()
if _txf:
    _txf_styles = {
        "【做多訊號】": ("#e6f4ea", "#2ca02c", "🟢", "可進場做多"),
        "【做空訊號】": ("#fde7e9", "#d62728", "🔴", "可進場做空"),
        "【出場訊號】": ("#fff8e1", "#ff8f00", "🟠", "已持有部位平倉、不新進場"),
        "【偏多】": ("#e3f2fd", "#1976d2", "🔵", "趨勢偏多但無進場訊號"),
        "【偏空】": ("#e3f2fd", "#1976d2", "🔵", "趨勢偏空但無進場訊號"),
        "【觀望】": ("#f5f5f5", "#666", "⚪", "趨勢不明"),
    }
    _bg, _bd, _emo, _action = _txf_styles.get(_txf["verdict"], ("#f5f5f5", "#666", "⚪", ""))
    st.markdown(
        f"<div style='background:{_bg}; border-left:6px solid {_bd}; "
        f"padding:10px 16px; border-radius:6px; margin:8px 0; color:#1a1a1a;'>"
        f"<span style='font-size:14px; color:#666'>📊 台指期日 K 訊號</span> &nbsp;"
        f"<span style='font-size:22px; font-weight:800; color:{_bd}'>{_emo} {_txf['verdict']}</span> &nbsp;"
        f"<span style='font-size:13px; color:#444'>資料 {_txf['date']} ｜ 收 {_txf['close']:,} ｜ <b>{_action}</b></span>"
        f"</div>",
        unsafe_allow_html=True,
    )

# 總資產 banner（含現金）
_cash, _cash_updated = _read_cash()


# ──────────────── TAIEX MA 抄底訊號 banner ────────────────
def _read_taiex_ma_state():
    """讀 TAIEX MA tracker 狀態"""
    from pathlib import Path as _P
    import json as _json
    f = _P("data/taiex_ma_state.json")
    if not f.exists():
        return None
    try:
        return _json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


_ma_state = _read_taiex_ma_state()
if _ma_state:
    # 雲端無 TXF 時，TAIEX MA 就是進場閘門
    _ma_gate_hint = "" if _txf else " ｜ <b style='color:#1976d2'>此為進場閘門（雲端無 TXF）</b>"
    st.markdown(
        f"<div style='background:{_ma_state['bg']}; border-left:6px solid {_ma_state['color']}; "
        f"padding:8px 14px; border-radius:6px; margin:8px 0; color:#1a1a1a; font-size:12px;'>"
        f"<span style='color:#666'>📉 TAIEX 抄底訊號</span> &nbsp;"
        f"<span style='font-size:16px; font-weight:800; color:{_ma_state['color']}'>{_ma_state['level']}</span> &nbsp;"
        f"<span style='color:#444'>{_ma_state['label']} ｜ "
        f"距 20MA <b>{_ma_state['dist_ma20_pct']:+.2f}%</b> ｜ "
        f"60MA <b>{_ma_state['dist_ma60_pct']:+.2f}%</b> ｜ "
        f"200MA <b>{_ma_state['dist_ma200_pct']:+.2f}%</b>{_ma_gate_hint}</span><br>"
        f"<span style='color:#555; font-style:italic'>{_ma_state['action']}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


# 抓報價
codes_market = list(raw_df[["code", "market"]].drop_duplicates().itertuples(index=False, name=None))
quotes = fetch_realtime_quotes(codes_market)
idx_info = fetch_index()

# 聚合：同股不同批次 → 加權平均（保留 category + stop_loss）
def agg_portfolio(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "category" not in df.columns:
        df["category"] = "未分類"
    if "stop_loss" not in df.columns:
        df["stop_loss"] = None
    g = df.groupby(["code", "name", "market", "category"], as_index=False).apply(
        lambda x: pd.Series({
            "shares": int(x["shares"].sum()),
            "cost": (x["shares"] * x["cost"]).sum() / x["shares"].sum(),
            "stop_loss": pd.to_numeric(x["stop_loss"], errors="coerce").dropna().iloc[0]
            if pd.to_numeric(x["stop_loss"], errors="coerce").notna().any() else None,
        })
    ).reset_index(drop=True)
    return g


agg = agg_portfolio(raw_df)

# 三大法人資料（盤後資料，30 分快取）
instit_by_code, instit_dates = fetch_institutional_all()

# 總經 + 夜盤指標 + 市場狀態
macro_data = fetch_macro_cached()
night_data = fetch_night_cached()
all_macro = {**macro_data, **night_data}
macro_score, macro_notes = macro_risk_score(all_macro)
macro_emo, macro_label = macro_verdict(macro_score)
market_regime = fetch_market_regime_cached()

# 市況翻盤偵測 → 桌面通知（跨 session 持久化）
import json as _json
def _load_last_regime() -> str | None:
    try:
        return _json.loads(REGIME_STATE_FILE.read_text(encoding="utf-8")).get("regime")
    except Exception:
        return None


def _save_regime(regime: str) -> None:
    try:
        REGIME_STATE_FILE.write_text(
            _json.dumps({"regime": regime, "ts": taipei_now().isoformat()}),
            encoding="utf-8",
        )
    except Exception:
        pass


_current_regime = market_regime["regime"]
_last_regime = _load_last_regime()
if _last_regime and _last_regime != _current_regime and _current_regime != "unknown":
    regime_label = market_regime["label"]
    msg = (
        f"加權指數 {market_regime.get('last', 0):,.0f}（vs 200MA {market_regime.get('deviation', 0):+.1f}%）"
        f" — {market_regime.get('advice', '')}"
    )
    desktop_notify(f"{market_regime['emoji']} 市況變化：{_last_regime} → {regime_label}", msg)
_save_regime(_current_regime)


def _macro_metric(col, data: dict, fmt: str = "{:.2f}"):
    """渲染一個總經/夜盤指標卡"""
    level = data.get("level", "normal")
    color_map = {"panic": "🔴🔴", "warn": "🔴", "calm": "🟢", "bull": "🟢", "normal": "⚪"}
    badge = color_map.get(level, "⚪")
    col.metric(
        f"{badge} {data['name']}",
        fmt.format(data["last"]),
        f"{data['chg_pct']:+.2f}%",
        delta_color="inverse" if level in ("panic", "warn") else "normal",
        help=data.get("desc"),
    )

# 逐檔處理：計算指標、損益、主力評分
rows: list[dict] = []
all_signals: list[tuple[str, str, str]] = []
stock_details: dict[str, dict] = {}  # 給主力分析區用：{code: {hist, score_data, instit}}
for _, r in agg.iterrows():
    # 7/07 修：跳過 shares=0 的 placeholder 部位（如未買的 income）
    if int(r.get("shares", 0) or 0) <= 0:
        continue
    q = quotes.get(r["code"])
    hist = fetch_history(r["code"], r["market"])
    ind = compute_indicators(hist)

    # 三大法人分析
    instit_hist = instit_by_code.get(r["code"], [])
    instit_info = analyze_instit(instit_hist) if instit_hist else None

    # 主力評分
    score_data = smart_money_score(hist, instit_info)

    stock_details[r["code"]] = {
        "name": r["name"],
        "category": r["category"],
        "hist": hist,
        "score": score_data,
        "instit_hist": instit_hist,
        "instit_info": instit_info,
    }

    if q is None:
        _sl = r.get("stop_loss")
        rows.append({
            "類別": r["category"],
            "代號": r["code"], "股名": r["name"], "股數": int(r["shares"]),
            "成本": round(r["cost"], 2),
            "停損價": float(_sl) if _sl is not None and pd.notna(_sl) else None,
            "Trail 停損": None, "階梯": "未啟動",
            "現價": None, "漲跌%": None,
            "投資成本": int(buy_total(r["cost"], r["shares"])),
            "帳面收入": None, "損益": None, "損益%": None,
            "主力評分": score_data["score"], "建議": score_data["verdict_emoji"],
            "訊號": "⚠ 無報價",
        })
        continue

    cost_total = buy_total(r["cost"], r["shares"])
    market_val = sell_proceeds(q["last"], r["shares"], r["code"])
    pnl = market_val - cost_total
    pnl_pct = pnl / cost_total * 100
    sigs = check_signals(r, ind, q)
    for emoji, txt in sigs:
        all_signals.append((emoji, r["name"], txt))
    # 主力訊號 → 加進緊急訊號區（強訊號才提示）
    if score_data["score"] >= 6:
        all_signals.append(("🟢", r["name"], f"主力評分 +{score_data['score']} 強烈加碼"))
    elif score_data["score"] <= -6:
        all_signals.append(("🔴", r["name"], f"主力評分 {score_data['score']} 強烈減碼"))
    # 法人連續買賣超強訊號
    if instit_info:
        cd = instit_info["consecutive_days"]
        if cd >= 5:
            all_signals.append(("🟢", r["name"], f"外資+投信連續 {cd} 日買超"))
        elif cd <= -5:
            all_signals.append(("🔴", r["name"], f"外資+投信連續 {abs(cd)} 日賣超"))

    # 停損價警示（套用 Trailing Stop）
    sl = r.get("stop_loss")
    fixed_sl = float(sl) if sl is not None and pd.notna(sl) else 0.0

    # Trailing Stop：取 peak 算 trail 停損，與固定停損取較高者
    trail_tier = "未啟動"
    trail_sl = 0.0
    eff_sl = fixed_sl
    try:
        from trailing_stop import compute_trail_stop, effective_stop, load_state
        _ts = load_state()
        if r["code"] in _ts:
            _peak = _ts[r["code"]]["peak"]
            trail_sl, trail_tier = compute_trail_stop(float(r["cost"]), _peak)
            eff_sl = effective_stop(fixed_sl, trail_sl)
    except Exception:
        pass

    if eff_sl > 0 and q["last"] <= eff_sl:
        loss_pct = (q["last"] - r["cost"]) / r["cost"] * 100
        tier_lbl = f" [{trail_tier}]" if trail_tier != "未啟動" else ""
        all_signals.append((
            "🔴", r["name"],
            f"⛔ 跌破生效停損 {eff_sl:.2f}{tier_lbl}（現價 {q['last']:.2f}，損益 {loss_pct:+.1f}%）— 紀律檢查",
        ))

    sl_val = r.get("stop_loss")
    rows.append({
        "類別": r["category"],
        "代號": r["code"],
        "股名": r["name"],
        "股數": int(r["shares"]),
        "成本": round(r["cost"], 2),
        "停損價": float(sl_val) if sl_val is not None and pd.notna(sl_val) else None,
        "Trail 停損": round(eff_sl, 2) if eff_sl > 0 else None,
        "階梯": trail_tier,
        "現價": q["last"],
        "漲跌%": round(q["change_pct"], 2),
        "投資成本": int(cost_total),
        "帳面收入": int(market_val),
        "損益": int(pnl),
        "損益%": round(pnl_pct, 2),
        "主力評分": score_data["score"],
        "建議": score_data["verdict_emoji"],
        "訊號": " ".join(e for e, _ in sigs) if sigs else "",
    })

df = pd.DataFrame(rows)

# 按類別固定順序排序（同類擺一起，不會跳）
df["__cat_order"] = df["類別"].map(
    {c: i for i, c in enumerate(CATEGORY_ORDER)}
).fillna(99).astype(int)
df = df.sort_values(["__cat_order", "代號"]).drop(columns="__cat_order").reset_index(drop=True)

# 總覽計算（先算數字，UI 等下再用小字顯示）
total_cost = float(df["投資成本"].dropna().sum())
total_val = float(df["帳面收入"].dropna().sum())
total_pnl = total_val - total_cost
total_pct = total_pnl / total_cost * 100 if total_cost else 0.0

# 💰 總資產 banner（持股市值 + 現金）
_total_assets = total_val + _cash
_stock_pct = total_val / _total_assets * 100 if _total_assets else 0
_cash_pct = _cash / _total_assets * 100 if _total_assets else 0
# 動態現金姿勢判斷
if _cash_pct >= 70:
    _stance_emo, _stance_label, _stance_color = "🛡️", "極度防禦", "#2ca02c"
elif _cash_pct >= 50:
    _stance_emo, _stance_label, _stance_color = "🔵", "謹慎", "#1976d2"
elif _cash_pct >= 30:
    _stance_emo, _stance_label, _stance_color = "🟡", "平衡", "#f9a825"
elif _cash_pct >= 15:
    _stance_emo, _stance_label, _stance_color = "🟠", "進攻", "#ff8f00"
else:
    _stance_emo, _stance_label, _stance_color = "🔴", "全壓", "#d62728"

# ═══════════════ 🎯 今日決策快照（Action Bar）═══════════════
# 7/07 升級：一屏濃縮 = 資產 + 決策紅綠燈 + 需處理數
# 決策燈號 — 綜合 TXF + TAIEX MA
_decision_emo = "⏸️"
_decision_txt = "等訊號"
_decision_color = "#666"
_decision_bg = "#f5f5f5"
if _txf and _txf.get("verdict") == "【做多訊號】":
    _decision_emo = "🟢"
    _decision_txt = "可進場做多"
    _decision_color = "#1a5d2e"
    _decision_bg = "#e6f4ea"
elif _txf and _txf.get("verdict") == "【做空訊號】":
    _decision_emo = "🔴"
    _decision_txt = "不新進場（做空訊號）"
    _decision_color = "#b71c1c"
    _decision_bg = "#fde7e9"
elif _ma_state and "抄底反彈確認" in _ma_state.get("level", ""):
    _decision_emo = "🟢"
    _decision_txt = "可進場（TAIEX 反彈確認）"
    _decision_color = "#1a5d2e"
    _decision_bg = "#e6f4ea"
elif _ma_state and ("🔴" in _ma_state.get("level", "") or "跌破" in _ma_state.get("level", "")):
    _decision_emo = "🔴"
    _decision_txt = "避免進場（大盤空頭）"
    _decision_color = "#b71c1c"
    _decision_bg = "#fde7e9"
else:
    _decision_emo = "⏸️"
    _decision_txt = "等訊號（大盤未給進場號誌）"

# 損益 delta
_pnl_delta_str = f"{total_pnl:+,.0f} ({total_pct:+.2f}%)"
_pnl_color = "#d62728" if total_pnl >= 0 else "#2ca02c"

st.markdown(
    f"""
<div style='background:linear-gradient(135deg, #fafafa, #fff); border:2px solid #ddd;
            border-radius:12px; padding:14px 18px; margin:10px 0; color:#1a1a1a;'>
  <div style='display:flex; justify-content:space-between; align-items:baseline; margin-bottom:8px'>
    <div>
      <div style='color:#666; font-size:11px'>💼 總資產</div>
      <div style='font-size:26px; font-weight:800; color:#111'>{_total_assets:,.0f}</div>
      <div style='color:{_pnl_color}; font-size:13px; font-weight:600'>{_pnl_delta_str} 未實現損益</div>
    </div>
    <div style='text-align:right'>
      <div style='color:#666; font-size:11px'>{_stance_emo} {_stance_label}</div>
      <div style='font-size:13px; color:#444; margin-top:4px'>
        現金 <b>{_cash_pct:.0f}%</b> ｜ 持股 <b>{_stock_pct:.0f}%</b>
      </div>
    </div>
  </div>
  <div style='background:{_decision_bg}; border:1px solid {_decision_color}; border-radius:6px;
              padding:8px 12px; margin-top:8px; text-align:center'>
    <span style='color:#666; font-size:11px'>🎯 今日決策</span>
    <span style='font-size:16px; font-weight:800; color:{_decision_color}; margin:0 8px'>
      {_decision_emo} {_decision_txt}
    </span>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

with st.expander("💰 資產明細 — 點開查看", expanded=False):
    _a1, _a2, _a3, _a4 = st.columns(4)
    _a1.metric("🏆 總資產", f"{_total_assets:,.0f}")
    _a2.metric("💵 現金", f"{_cash:,.0f}", f"{_cash_pct:.1f}%")
    _a3.metric("📈 持股市值", f"{total_val:,.0f}", f"{_stock_pct:.1f}%")
    _a4.metric(f"{_stance_emo} 投資姿勢", _stance_label, f"現金 {_cash_pct:.0f}%")
    st.caption(
        f"💡 現金更新於 {_cash_updated or 'N/A'} — "
        f"修改 `data/cash.json` 立即反映 ｜ "
        f"參考姿勢：≥70% 防禦、50-70% 謹慎、30-50% 平衡、15-30% 進攻、<15% 全壓"
    )

# ═══════════════ 📊 資產配置監控（7/07 新增）═══════════════
try:
    import json as _aj
    _atarget_f = Path("data/allocation_targets.json")
    if _atarget_f.exists() and not df.empty:
        _atargets = _aj.loads(_atarget_f.read_text(encoding="utf-8")).get("targets", {})
        # 計算目前各層資產（用市值）
        _actual = {"swing": 0, "core": 0, "income": 0, "cash": _cash}
        # 從 raw_df 讀 hold_type，跟 df（持股市值）合併
        _hold_map = dict(zip(raw_df["code"].astype(str),
                              raw_df["hold_type"].fillna("swing") if "hold_type" in raw_df.columns else ["swing"]*len(raw_df)))
        for _, _r in df.iterrows():
            _code = str(_r["代號"])
            _val = _r.get("帳面收入") or 0
            if pd.isna(_val) or _val <= 0: continue
            _ht = _hold_map.get(_code, "swing")
            if _ht not in _actual:
                _ht = "swing"
            _actual[_ht] += float(_val)

        _target_total = sum(v["target_twd"] for v in _atargets.values())
        _actual_total = sum(_actual.values())

        with st.expander("📊 資產配置監控 — 點開查看", expanded=False):
            st.caption(f"目標總資產：{_target_total:,.0f} ｜ 實際總資產：{_actual_total:,.0f} "
                       f"({(_actual_total/_target_total*100):.1f}% 完成)")
            for _key in ["swing", "core", "income", "cash"]:
                _cfg = _atargets.get(_key, {})
                _label = _cfg.get("label", _key)
                _target = _cfg.get("target_twd", 0)
                _color = _cfg.get("color", "#666")
                _val = _actual.get(_key, 0)
                _pct_actual = (_val / _target_total * 100) if _target_total else 0
                _pct_target = (_target / _target_total * 100) if _target_total else 0
                _fill_ratio = min(100, (_val / _target * 100)) if _target else 0
                _drift = _pct_actual - _pct_target
                _drift_emo = "✅" if abs(_drift) <= 5 else ("⚠️" if abs(_drift) <= 15 else "🚨")
                _diff = _val - _target

                _diff_color = "#d62728" if _diff < 0 else "#2ca02c"
                st.markdown(
                    f"<div style='background:#fafafa; border:1px solid #e0e0e0; border-radius:6px; "
                    f"padding:10px 14px; margin:6px 0; color:#1a1a1a'>"
                    f"<div style='display:flex; justify-content:space-between; align-items:baseline; margin-bottom:5px'>"
                    f"<span style='font-weight:700; font-size:14px'>{_label}</span>"
                    f"<span style='font-size:11px; color:#666'>"
                    f"實際 <b style='color:#111'>{_val:,.0f}</b> "
                    f"／ 目標 {_target:,.0f} "
                    f"（差 <b style='color:{_diff_color}'>{_diff:+,.0f}</b>）</span>"
                    f"</div>"
                    f"<div style='background:#eee; height:14px; border-radius:3px; overflow:hidden; margin-bottom:3px'>"
                    f"<div style='background:{_color}; width:{_fill_ratio:.1f}%; height:100%'></div>"
                    f"</div>"
                    f"<div style='display:flex; justify-content:space-between; font-size:11px; color:#666'>"
                    f"<span>{_fill_ratio:.1f}% 完成</span>"
                    f"<span>{_drift_emo} 佔比 {_pct_actual:.1f}% / 目標 {_pct_target:.1f}% (drift {_drift:+.1f}%)</span>"
                    f"</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )
            st.caption("💡 drift 顏色說明：✅ ±5% 健康  ｜  ⚠️ ±15% 需注意  ｜  🚨 >15% 需 rebalance")
except Exception as _e:
    pass  # 配置監控失敗不影響其他功能

# 套用總經修正到每檔評分（給排行榜用）
def _combined_score(stock_s: int) -> int:
    macro_adj = max(-2, min(2, macro_score // 2 if macro_score else 0))
    return max(-10, min(10, stock_s + macro_adj))


def _verdict_from_combined(c: int) -> tuple[str, str]:
    if c >= 6:
        return "🟢🟢", "強烈加碼"
    if c >= 3:
        return "🟢", "偏多（可逢低加碼）"
    if c == 2:
        return "🟡", "接近加碼門檻"
    if c >= -1:
        return "⚪", "中性觀望"
    if c == -2:
        return "🟡", "接近減碼門檻"
    if c >= -5:
        return "🔴", "偏空（可逢高減碼）"
    return "🔴🔴", "強烈減碼"


# ──────────────── 樣式輔助（提前定義，給後面 tabs / 持倉表共用）────────────────
def color_pnl(v):
    if pd.isna(v):
        return ""
    if v > 0:
        return "color: #d62728; font-weight: 600"
    if v < 0:
        return "color: #2ca02c; font-weight: 600"
    return ""


def color_score(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ""
    if v >= 6:
        return "background-color: #d62728; color: white; font-weight: 700; text-align: center"
    if v >= 3:
        return "color: #d62728; font-weight: 700"
    if v <= -6:
        return "background-color: #2ca02c; color: white; font-weight: 700; text-align: center"
    if v <= -3:
        return "color: #2ca02c; font-weight: 700"
    return ""


def color_category(v):
    bg = CATEGORY_COLOR.get(v, "")
    return f"background-color: {bg}; color: #1a1a1a; font-weight: 600" if bg else ""


# ════════════════════════════════════════════════════════════
# 📌 迷你狀態列：TAIEX | 損益 | 投資成本 | 帳面收入 | 總經
# ════════════════════════════════════════════════════════════
pnl_color = "#d62728" if total_pnl >= 0 else "#2ca02c"
idx_text = (
    f"<b>TAIEX</b> {idx_info['last']:,.0f} "
    f"<span style='color:{'#d62728' if idx_info['change_pct']>=0 else '#2ca02c'}'>"
    f"({idx_info['change_pct']:+.2f}%)</span>"
    if idx_info else "<b>TAIEX</b> —"
)
regime_bg_color = {"bull": "#e6f4ea", "warning": "#fff8e1",
                   "bear": "#fde7e9", "unknown": "#f5f5f5"}.get(market_regime["regime"], "#f5f5f5")
regime_dev = f" ({market_regime['deviation']:+.1f}% vs 200MA)" if "deviation" in market_regime else ""

mini_html = f"""
<div style='background:#f0f2f6; padding:8px 14px; border-radius:8px;
            font-size:13px; color:#333; display:flex; gap:24px; flex-wrap:wrap;
            margin-bottom:14px;'>
  <span style='background:{regime_bg_color}; color:#1a1a1a; padding:2px 10px;
               border-radius:4px; font-weight:700'>
    {market_regime['emoji']} {market_regime['label']}{regime_dev}
  </span>
  <span>{idx_text}</span>
  <span><b>未實現損益</b>
    <span style='color:{pnl_color}; font-weight:600'>{int(total_pnl):+,}</span>
    <span style='color:{pnl_color}'>({total_pct:+.2f}%)</span></span>
  <span><b>投資成本</b> {int(total_cost):,}</span>
  <span><b>帳面收入</b> {int(total_val):,}</span>
  <span><b>{macro_emo} 總經</b> {macro_score:+d}/±5 {macro_label}</span>
  <span><b>檔數</b> {len(df)}</span>
</div>
"""
st.markdown(mini_html, unsafe_allow_html=True)

# ════════════════════════════════════════════════════════════
# 🎯 加碼 / 減碼建議榜 ← 主視覺，第一眼看的就是這個
# ════════════════════════════════════════════════════════════
st.subheader("🎯 加碼 / 減碼建議")


def _regime_banner(context: str = "") -> None:
    """根據市況顯示警告。context 用於補上區段名（例如「推薦標的」）"""
    if market_regime["regime"] == "bear":
        st.error(
            f"🔴 **熊市模式** — TAIEX {market_regime['last']:,.0f} 跌破 200MA "
            f"({market_regime['ma200']:,.0f}, {market_regime['deviation']:+.1f}%)。\n\n"
            f"**回測證實**：本系統在 2022 熊市，期望值 −2.04%/筆、勝率 37.5%、連敗 11 次。"
            f"{('**請忽略下方' + context + '**，' ) if context else ''}建議空手或減碼。"
        )
    elif market_regime["regime"] == "warning":
        st.warning(
            f"🟡 **警戒模式** — TAIEX {market_regime['last']:,.0f} 在 200MA "
            f"({market_regime['ma200']:,.0f}) 附近震盪 ({market_regime['deviation']:+.1f}%)。"
            f"建議**減半部位**謹慎進場。"
        )


_regime_banner("加碼建議")

# 收集每檔的綜合評分 + 屬性
ranked = []
for code, d in stock_details.items():
    s = d["score"]
    combined = _combined_score(s["score"])
    # 找出最強的 2 個 breakdown 理由
    bd = sorted(s["breakdown"], key=lambda x: -abs(x[1]))[:2]
    reasons = "、".join(f"{cat}{pts:+d}" for cat, pts, _ in bd) if bd else "（資料不足）"
    detail_reasons = "; ".join(why for _, _, why in bd) if bd else ""
    # 取現價
    row = df[df["代號"] == code]
    price = float(row["現價"].iloc[0]) if len(row) and pd.notna(row["現價"].iloc[0]) else None
    chg = float(row["漲跌%"].iloc[0]) if len(row) and pd.notna(row["漲跌%"].iloc[0]) else None
    comb_emo, comb_verdict = _verdict_from_combined(combined)

    # 持倉過熱檢查（6/17 國巨教訓 — 持倉評分高但極度過熱，加碼=災難）
    overheat_flags = []
    hist_d = d.get("hist")
    if hist_d is not None and len(hist_d) >= 60:
        try:
            last_p = float(hist_d["Close"].iloc[-1])
            ma60_p = float(hist_d["Close"].tail(60).mean())
            dist_ma60 = (last_p - ma60_p) / ma60_p * 100
            if dist_ma60 > 30:
                overheat_flags.append(f"距60MA +{dist_ma60:.0f}%")
            if len(hist_d) >= 30:
                ret_30d = (last_p - float(hist_d["Close"].iloc[-30])) / float(hist_d["Close"].iloc[-30]) * 100
                if ret_30d > 40:
                    overheat_flags.append(f"30日 +{ret_30d:.0f}%")
            high_60d_p = float(hist_d["Close"].tail(60).max())
            dist_high = (last_p - high_60d_p) / high_60d_p * 100
            if dist_high > -3:
                overheat_flags.append(f"近60日高")
        except Exception:
            pass

    ranked.append({
        "code": code, "name": d["name"], "category": d["category"],
        "stock_score": s["score"], "combined": combined,
        "verdict": comb_verdict, "emoji": comb_emo,
        "attention": s.get("attention_level", 0),
        "attention_reasons": s.get("attention_reasons", []),
        "reasons_short": reasons,
        "reasons_detail": detail_reasons,
        "price": price, "chg": chg,
        "overheat_flags": overheat_flags,
    })

ranked.sort(key=lambda x: -x["combined"])

# 分組：加碼 / 減碼 / 邊緣（±2）/ 中性
buy_list = [r for r in ranked if r["combined"] >= 3]
sell_list = [r for r in ranked if r["combined"] <= -3]
edge_buy_list = [r for r in ranked if r["combined"] == 2]    # 接近加碼門檻
edge_sell_list = [r for r in ranked if r["combined"] == -2]  # 接近減碼門檻
neutral_list = [r for r in ranked if -2 < r["combined"] < 2]


def _stock_card(s: dict, side: str) -> str:
    """側=buy/sell/edge_buy/edge_sell/neutral；回傳 HTML 卡片"""
    if side == "buy":
        bg = "#fde7e9"; border = "#d62728"; main_color = "#d62728"
    elif side == "sell":
        bg = "#e6f4ea"; border = "#2ca02c"; main_color = "#2ca02c"
    elif side == "edge_buy":
        bg = "#fff8e1"; border = "#f9a825"; main_color = "#e65100"  # 黃底偏紅
    elif side == "edge_sell":
        bg = "#fff8e1"; border = "#f9a825"; main_color = "#558b2f"  # 黃底偏綠
    else:
        bg = "#f5f5f5"; border = "#bbb"; main_color = "#666"
    # 特別關注標記
    star = ""
    star_tip = ""
    if s["attention"] == 2:
        star = "⭐⭐"
        star_tip = "（" + "; ".join(s["attention_reasons"]) + "）"
    elif s["attention"] == 1:
        star = "⭐"
        star_tip = "（" + "; ".join(s["attention_reasons"]) + "）"
    price_str = f"{s['price']:.2f}" if s['price'] is not None else "—"
    chg_str = ""
    if s["chg"] is not None:
        chg_color = "#d62728" if s["chg"] >= 0 else "#2ca02c"
        chg_str = f"<span style='color:{chg_color}; font-size:12px'>({s['chg']:+.2f}%)</span>"
    # 過熱警告 banner（持倉版過熱檢查 — 即使評分高也警示）
    overheat_html = ""
    flags = s.get("overheat_flags", [])
    if flags:
        flag_str = " ｜ ".join(flags)
        if side == "buy":
            warn = "⚠️ 過熱：評分高但已透支，**不適合加碼**，考慮減碼鎖利"
        else:
            warn = "⚠️ 過熱訊號：" + flag_str
        overheat_html = (
            f"<div style='background:#fde7e9; border:1px solid #d62728; "
            f"color:#b71c1c; padding:6px 10px; border-radius:4px; "
            f"font-size:11px; font-weight:700; margin-top:6px;'>"
            f"🔥 {warn}<br><span style='font-weight:500; font-size:10px'>{flag_str}</span>"
            f"</div>"
        )

    return f"""
<div style='background:{bg}; border-left:4px solid {border}; padding:10px 12px;
            border-radius:6px; margin-bottom:8px; color:#1a1a1a;'>
  <div style='display:flex; justify-content:space-between; align-items:baseline'>
    <div>
      <span style='font-size:16px; font-weight:700; color:#111'>{s['name']}</span>
      <span style='color:#666; font-size:12px; margin-left:6px'>{s['code']} · {s['category']}</span>
      <span style='margin-left:6px' title='{star_tip}'>{star}</span>
    </div>
    <div style='font-size:22px; font-weight:800; color:{main_color}'>{s['combined']:+d}</div>
  </div>
  <div style='font-size:12px; color:#444; margin-top:2px'>
    {s['emoji']} {s['verdict']}　|　{price_str} {chg_str}
  </div>
  <div style='font-size:11px; color:#555; margin-top:4px; font-style:italic'>
    {s['reasons_detail'] or s['reasons_short']}
  </div>
  {overheat_html}
</div>
"""


col_buy, col_sell = st.columns(2)
with col_buy:
    st.markdown(f"#### 🟢 加碼候選 ({len(buy_list)})")
    if not buy_list:
        st.caption("（目前無加碼候選）")
    else:
        for s in buy_list:
            st.markdown(_stock_card(s, "buy"), unsafe_allow_html=True)
    # 邊緣警示：差 1 分就進加碼
    if edge_buy_list:
        st.caption(f"⚠️ 接近加碼門檻（綜合 +2，差 1 分）")
        for s in edge_buy_list:
            st.markdown(_stock_card(s, "edge_buy"), unsafe_allow_html=True)

with col_sell:
    st.markdown(f"#### 🔴 減碼候選 ({len(sell_list)})")
    if not sell_list:
        st.caption("（目前無減碼候選）")
    else:
        for s in sell_list:
            st.markdown(_stock_card(s, "sell"), unsafe_allow_html=True)
    # 邊緣警示：差 1 分就進減碼
    if edge_sell_list:
        st.caption(f"⚠️ 接近減碼門檻（綜合 -2，差 1 分）")
        for s in edge_sell_list:
            st.markdown(_stock_card(s, "edge_sell"), unsafe_allow_html=True)

# 中性區（折疊）
if neutral_list:
    with st.expander(f"⚪ 中性觀望 ({len(neutral_list)})", expanded=False):
        cols = st.columns(2)
        for i, s in enumerate(neutral_list):
            with cols[i % 2]:
                st.markdown(_stock_card(s, "neutral"), unsafe_allow_html=True)

# ════════════════════════════════════════════════════════════
# 🔥 今日推薦標的（非持倉，盤後資料 + 技術面挑出 5 檔）
# ════════════════════════════════════════════════════════════
_hdr_l, _hdr_r = st.columns([4, 1])
with _hdr_l:
    st.subheader("🔥 今日推薦加碼標的（非你的持倉）")
with _hdr_r:
    if st.button("🔄 強制重算", help="清快取重新計算評分（解 cache 過時問題）"):
        fetch_top_picks_cached.clear()
        st.rerun()

st.caption(
    "**已強化**：法人金額前 100 → 跑系統評分（**過熱保護**：距 60MA < +30%、30 日 < +40%、60 日新高 > +3%）→ "
    "**強制 2 年回測，剔除負期望值** → 取前 5。"
    "排除 ETF / 金融 / 電信 / 你已持有 / 30 天內賣出 / 單日漲幅 > 9.5% 的標的。"
)

_regime_banner("推薦標的")

if market_regime["regime"] == "bear":
    st.caption("熊市模式下不執行推薦計算（省資源 + 避免誤導）")
    top_picks_data = []
    _calc_time = ""
else:
    portfolio_codes_set = set(stock_details.keys())
    top_picks_data, _calc_time = fetch_top_picks_cached(
        tuple(sorted(portfolio_codes_set)), instit_by_code
    )
    # 7/06 升級：score>=4 才推薦（回測資料顯示 score 3 勝率不夠）
    _pre_filter_n = len(top_picks_data)
    top_picks_data = [p for p in top_picks_data if p.get("score", 0) >= 4]
    _filtered_n = _pre_filter_n - len(top_picks_data)
    if _filtered_n > 0:
        st.caption(f"🎯 已過濾 {_filtered_n} 檔 score<4（保守派：只推薦最強）")
    # 警戒期只取前 3（降低不確定性高時的選擇困難）
    if market_regime["regime"] == "warning":
        top_picks_data = top_picks_data[:3]
        st.caption("⚠️ 警戒模式：只顯示評分最強的前 3 檔")

if _calc_time:
    import datetime as _dt
    # 雲端 server UTC → 台灣 UTC+8
    _now = _dt.datetime.utcnow() + _dt.timedelta(hours=8)
    try:
        # 若 _calc_time 帶「（即時報價）」等後綴，先切出時間
        _calc_hms = _calc_time.split("（")[0].strip() if "（" in _calc_time else _calc_time
        _calc_dt = _dt.datetime.combine(_now.date(),
                                         _dt.datetime.strptime(_calc_hms, "%H:%M:%S").time())
        _mins = max(0, int((_now - _calc_dt).total_seconds() // 60))
        _fresh = "🟢" if _mins < 2 else ("🟡" if _mins < 5 else "🔴")
        st.caption(f"{_fresh} 評分計算於 **{_calc_time}**（{_mins} 分鐘前）— 超過 5 分按右上「強制重算」")
    except Exception:
        st.caption(f"評分計算時間: {_calc_time}")

if not top_picks_data:
    if market_regime["regime"] != "bear":
        # 診斷：法人資料是否有抓到
        _instit_n = len(instit_by_code) if instit_by_code else 0
        if _instit_n == 0:
            st.warning(
                "⚠️ 雲端抓不到 TWSE 法人資料（可能 IP 被限流）\n\n"
                "**變通方案**：\n"
                "1. 等每天 17:30 排程跑完後推薦會存進 recs_history.jsonl\n"
                "2. 之後打開手機都會直接讀檔（不用即時抓）\n"
                "3. 桌機打開網頁不受影響（本機能抓 TWSE）"
            )
        else:
            # 7/07 升級：空榜 = 明確結論，不是錯誤
            st.markdown(
                """
                <div style='background:#e8f5e9; border:2px solid #2ca02c; border-radius:8px;
                            padding:16px; margin:8px 0; color:#1a1a1a; text-align:center'>
                  <div style='font-size:20px; font-weight:700; color:#1a5d2e; margin-bottom:8px'>
                    🈳 系統今日未新增推薦
                  </div>
                  <div style='font-size:13px; color:#555; margin-bottom:12px'>
                    ✅ <b>這是預期行為</b>，代表沒有 score≥4 的黑馬<br>
                    或大盤閘門擋著（TXF/TAIEX 未給進場訊號）
                  </div>
                  <div style='background:white; border-radius:6px; padding:10px; text-align:left; font-size:13px'>
                    <b style='color:#1a5d2e'>👉 今日建議動作</b><br>
                    • 💰 專心看持倉風控（下方持倉表）<br>
                    • 👀 檢查觀察名單追蹤中的股票<br>
                    • 📊 大盤 <b>不動 = 最好的動作</b>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            # 顯示昨日推薦追蹤（給用戶看到過去表現）
            try:
                import json as _yj
                from pathlib import Path as _yp
                from datetime import date as _yd, timedelta as _ytd
                _hist_f = _yp("data/recs_history.jsonl")
                if _hist_f.exists():
                    _y_target = None
                    for _off in range(1, 8):
                        _y_target = (_yd.today() - _ytd(days=_off)).isoformat()
                        _y_recs = []
                        for _line in _hist_f.read_text(encoding="utf-8").splitlines():
                            _line = _line.strip()
                            if not _line: continue
                            try:
                                _r = _yj.loads(_line)
                                if _r.get("snapshot_date") == _y_target:
                                    _y_recs.append(_r)
                            except: pass
                        if _y_recs:
                            break
                    if _y_recs:
                        with st.expander(f"📊 上一次推薦（{_y_target}，{len(_y_recs)} 檔）— 表現追蹤", expanded=False):
                            for _r in _y_recs[:5]:
                                _t = _r.get("t5_return_pct") or _r.get("t1_return_pct")
                                _t_str = f"{_t:+.2f}%" if _t is not None else "追蹤中"
                                _t_color = "#d62728" if (_t or 0) > 0 else ("#2ca02c" if (_t or 0) < 0 else "#666")
                                _strat_lbl = _r.get("strategy_label", "")
                                st.markdown(
                                    f"<div style='padding:6px 10px; margin:3px 0; background:#f9f9f9; border-radius:4px; font-size:12px'>"
                                    f"<b>{_r['code']} {_r.get('name','')}</b> "
                                    f"score {_r.get('score',0):+d} {_strat_lbl} → "
                                    f"<span style='color:{_t_color}; font-weight:700'>{_t_str}</span>"
                                    f"</div>",
                                    unsafe_allow_html=True,
                                )
            except Exception:
                pass
else:
    # 進場決策評估函式（6/23 升級：直接給 GO/STOP 燈號 + 進場 SOP）
    # 讀 TXF 訊號（6/24 升級：偏多禁令 — Level 1 保守紀律派）
    # 7/06 升級：雲端無 TXF 時 fallback 用 TAIEX MA 當閘門
    _txf_for_entry = _txf  # 從上方已讀的 _txf banner
    _txf_verdict_str = (_txf_for_entry or {}).get("verdict", "")
    _has_txf = bool(_txf_for_entry)

    if _has_txf:
        _is_buy_signal = _txf_verdict_str == "【做多訊號】"
        _is_block_state = _txf_verdict_str in ("【偏多】", "【偏空】", "【觀望】", "【出場訊號】", "【做空訊號】")
        _gate_wait_text = f"TXF {_txf_verdict_str.strip('【】')}，等做多訊號"
    else:
        # 雲端 fallback：用 TAIEX MA 當閘門（保守版）
        # 只有📈抄底反彈確認才放行，其他一律擋 — 手機保守派
        _ma_level = (_ma_state or {}).get("level", "")
        _is_buy_signal = "抄底反彈確認" in _ma_level
        _is_block_state = bool(_ma_level) and not _is_buy_signal
        if not _ma_level:
            # 連 TAIEX MA state 都沒有 → 更保守 → 擋
            _is_block_state = True
            _gate_wait_text = "雲端無 TXF 訊號，桌機確認後再進"
        elif "正常" in _ma_level:
            _gate_wait_text = "TAIEX ⚪正常但無反彈訊號，等 TXF 做多訊號"
        else:
            _ma_label_clean = _ma_level.strip("🔴🟡🟢⚪📈⚠️ ")
            _gate_wait_text = f"大盤 {_ma_label_clean}，等訊號改善"

    # 6/30 升級：抄底反彈確認 → 解除偏多禁令（不必等 TXF）
    _rebound_signal = _ma_state and _ma_state.get("is_rebound_signal", False)
    if _rebound_signal:
        _is_block_state = False  # 抄底反彈確認 = 視同做多訊號

    def _entry_verdict(p):
        """根據今日漲跌 + K 線型態 + 評分 + TXF 訊號綜合判斷"""
        chg = p.get("chg_pct", 0)
        score = p.get("score", 0)
        breakdown = p.get("breakdown") or []
        # 抓 K 線警告型態
        bad_k = []
        good_k = []
        for cat, pts, why in breakdown:
            if cat == "K線":
                if pts < 0:
                    bad_k.append(why)
                elif pts > 0:
                    good_k.append(why)
        warnings = []
        if chg <= -5:
            warnings.append(f"⚠️ 今日暴跌 {chg:+.2f}%")
        elif chg >= 9:
            warnings.append(f"⚠️ 漲停接近 {chg:+.2f}%")
        elif chg <= -3:
            warnings.append(f"⚠️ 今日下跌 {chg:+.2f}%")
        for bk in bad_k:
            warnings.append(f"⚠️ {bk}")
        # 決定燈號
        if warnings:
            verdict = "STOP"
            v_color = "#d62728"
            v_bg = "#fde7e9"
            v_emo = "🔴"
            v_text = "不建議今日進場"
        # ★ TXF 偏多禁令（Level 1）— 只在【做多訊號】時才放行
        elif _is_block_state:
            verdict = "WAIT"
            v_color = "#1976d2"
            v_bg = "#e3f2fd"
            v_emo = "🔵"
            v_text = _gate_wait_text
        elif score >= 4 and -2 < chg < 5 and not bad_k:
            verdict = "GO"
            v_color = "#1a5d2e"
            v_bg = "#e6f4ea"
            v_emo = "🟢"
            v_text = "今日可進場 ✅"
        elif score >= 3 and -3 < chg < 6:
            verdict = "GO"
            v_color = "#558b2f"
            v_bg = "#e6f4ea"
            v_emo = "🟢"
            v_text = "今日可進場（條件 OK）"
        else:
            verdict = "WAIT"
            v_color = "#e65100"
            v_bg = "#fff8e1"
            v_emo = "🟡"
            v_text = "等等看（條件邊緣）"
        return {
            "verdict": verdict, "color": v_color, "bg": v_bg,
            "emo": v_emo, "text": v_text, "warnings": warnings,
            "good_k": good_k,
        }

    def _entry_sop(price, score, atr14=None):
        """產出簡易進場 SOP（限價、停損、停利、建議部位）
        7/06 升級：停損改用 ATR 動態計算（規格書優化 A）
        """
        if not price:
            return None
        # 限價 = 現價 +0.3%（避免市價殺出）
        limit_buy = price * 1.003
        # ★ ATR 動態停損（1.5 × ATR14）— 波動大股票停損放寬、波動小收緊
        # 保底：ATR 算出的停損跌幅不能超過 -10%（避免爛股放太寬）
        # 保底：ATR 算出的停損跌幅不能少於 -4%（避免熱股停太緊）
        if atr14 and atr14 > 0:
            atr_stop = price - 1.5 * atr14
            atr_pct = (atr_stop - price) / price * 100  # 負值
            if atr_pct < -10:
                stop_loss = price * 0.90
                stop_pct = -10.0
                stop_method = "ATR 過寬 → 硬上限 -10%"
            elif atr_pct > -4:
                stop_loss = price * 0.96
                stop_pct = -4.0
                stop_method = "ATR 過緊 → 硬下限 -4%"
            else:
                stop_loss = atr_stop
                stop_pct = atr_pct
                stop_method = f"1.5×ATR ({atr14:.2f})"
        else:
            stop_loss = price * 0.93   # ATR 缺 → 舊 -7%
            stop_pct = -7.0
            stop_method = "固定 -7%（無 ATR）"
        take_profit = price * 1.11  # +11%
        # 部位建議（依股價分級）
        if price >= 1000:
            pos_lots = "100 股（約 10 萬內）"
        elif price >= 500:
            pos_lots = "200-500 股（20-30 萬）"
        elif price >= 100:
            pos_lots = "1000-3000 股（10-30 萬）"
        else:
            pos_lots = "5000-10000 股（5-30 萬）"
        return {
            "limit_buy": limit_buy,
            "stop_loss": stop_loss,
            "stop_pct": stop_pct,
            "stop_method": stop_method,
            "take_profit": take_profit,
            "pos_lots": pos_lots,
        }

    pick_cols = st.columns(min(5, len(top_picks_data)))
    for i, p in enumerate(top_picks_data):
        with pick_cols[i % len(pick_cols)]:
            chg_color = "#d62728" if p["chg_pct"] >= 0 else "#2ca02c"
            score = p["score"]
            if score >= 6:
                badge_bg = "#d62728"; badge_color = "white"
            elif score >= 3:
                badge_bg = "#fde7e9"; badge_color = "#d62728"
            else:
                badge_bg = "#f5f5f5"; badge_color = "#666"
            star = "⭐⭐" if p["attention_level"] == 2 else ("⭐" if p["attention_level"] == 1 else "")
            instit_str = ""
            if p["instit"]:
                cd = p["instit"]["consecutive_days"]
                sum5 = p["instit"]["sum_recent"]
                instit_str = f"法人連{abs(cd)}日{'買' if cd >= 0 else '賣'}超 · 5日累計{sum5:+,}張"
            reasons_html = "<br>".join(
                f"<span style='color:#888'>• {cat} {pts:+d}</span> {why}"
                for cat, pts, why in p["top_reasons"]
            )

            # 進場決策
            ev_ = _entry_verdict(p)
            sop = _entry_sop(p.get("price", 0), score, p.get("atr14"))

            warnings_html = ""
            if ev_["warnings"]:
                warnings_html = (
                    "<div style='background:#fde7e9; border:1px solid #d62728; "
                    "color:#b71c1c; padding:4px 8px; border-radius:3px; "
                    "font-size:11px; margin-top:6px; font-weight:600;'>"
                    + "<br>".join(ev_["warnings"]) + "</div>"
                )

            sop_html = ""
            if sop and ev_["verdict"] == "GO":
                sop_html = f"""
<div style='background:#e8f5e9; border:1px solid #2ca02c; border-radius:4px;
            padding:6px 8px; margin-top:6px; font-size:11px; color:#1a1a1a;'>
  <div style='font-weight:700; color:#1a5d2e; margin-bottom:3px'>📋 進場建議</div>
  <div>限價買 <b>{sop['limit_buy']:.2f}</b>　部位 {sop['pos_lots']}</div>
  <div style='color:#d62728'>停損 <b>{sop['stop_loss']:.2f}</b>（{sop['stop_pct']:+.1f}%，{sop['stop_method']}）</div>
  <div style='color:#2ca02c'>停利 <b>{sop['take_profit']:.2f}</b>（+11% trail）</div>
</div>
"""

            verdict_html = f"""
<div style='background:{ev_["bg"]}; border:2px solid {ev_["color"]};
            border-radius:4px; padding:5px 8px; margin-top:8px;
            text-align:center; font-weight:700; font-size:13px; color:{ev_["color"]}'>
  {ev_["emo"]} {ev_["text"]}
</div>
"""

            # 7/06 優化 C：策略標籤
            strategy_html = ""
            _strat = p.get("strategy")
            if _strat and _strat.get("label"):
                _conf = _strat.get("confidence", 0)
                _strat_bg = "#e3f2fd" if _conf >= 80 else "#f5f5f5"
                _strat_bd = "#1976d2" if _conf >= 80 else "#999"
                strategy_html = (
                    f"<div style='background:{_strat_bg}; border:1px solid {_strat_bd}; "
                    f"border-radius:3px; padding:3px 6px; margin-top:4px; "
                    f"font-size:11px; color:#1a1a1a; text-align:center; font-weight:600'>"
                    f"{_strat['label']} <span style='color:#666; font-weight:400'>"
                    f"({_conf}% 信心)</span></div>"
                )

            # 7/06 優化 B：5 維度分數條
            dim_html = ""
            _dims = p.get("dim_scores")
            if _dims:
                _dim_labels = [
                    ("trend", "趨勢", "#1976d2"),
                    ("momentum", "動能", "#e65100"),
                    ("volume", "量能", "#7b1fa2"),
                    ("chip", "籌碼", "#2e7d32"),
                    ("risk", "風險", "#c62828"),
                ]
                _bars = ""
                for key, label, color in _dim_labels:
                    v = _dims.get(key, 50)
                    _bars += (
                        f"<div style='display:flex; align-items:center; font-size:10px; "
                        f"margin-bottom:2px; color:#333'>"
                        f"<span style='width:32px'>{label}</span>"
                        f"<div style='flex:1; background:#eee; height:8px; border-radius:2px; "
                        f"margin-right:4px; overflow:hidden'>"
                        f"<div style='background:{color}; width:{v}%; height:100%'></div>"
                        f"</div>"
                        f"<span style='width:22px; text-align:right; font-weight:600'>{v}</span>"
                        f"</div>"
                    )
                dim_html = (
                    f"<div style='background:#fafafa; padding:5px 6px; margin-top:4px; "
                    f"border-radius:3px; border:1px solid #e0e0e0'>"
                    f"<div style='font-size:10px; color:#666; margin-bottom:3px; font-weight:600'>"
                    f"📊 5 維度分數（加權總分 <b>{_dims.get('total_weighted', 50)}</b>/100）</div>"
                    f"{_bars}</div>"
                )

            card_html = f"""
<div style='background:#fff8e1; border:2px solid #f9a825; border-radius:8px;
            padding:12px; margin-bottom:8px; height:100%; color:#1a1a1a;'>
  <div style='display:flex; justify-content:space-between; align-items:baseline'>
    <span style='font-size:16px; font-weight:700; color:#111'>{p['name']} {star}</span>
    <span style='background:{badge_bg}; color:{badge_color}; padding:2px 8px;
                  border-radius:10px; font-weight:700; font-size:13px'>
      {score:+d}
    </span>
  </div>
  <div style='color:#666; font-size:11px; margin-top:2px'>{p['code']}</div>
  {strategy_html}
  <div style='font-size:13px; margin-top:6px; color:#333'>
    {p['verdict_emoji']} {p['verdict']}
  </div>
  <div style='font-size:14px; margin-top:4px; color:#111'>
    <b>{p['price']:.2f}</b>
    <span style='color:{chg_color}; font-size:12px; font-weight:600'>({p['chg_pct']:+.2f}%)</span>
  </div>
  {verdict_html}
  {warnings_html}
  {sop_html}
  {dim_html}
  <div style='font-size:11px; color:#555; margin-top:4px'>{instit_str}</div>
  <div style='font-size:11px; margin-top:3px; padding:3px 6px; border-radius:3px;
              background:{ '#e6f4ea' if (p.get('backtest_ev') or 0) > 1 else '#fff8e1' };
              color:{ '#1a5d2e' if (p.get('backtest_ev') or 0) > 1 else '#7a4f01' }'>
    🧪 回測 EV <b>{p.get('backtest_ev') if p.get('backtest_ev') is not None else 0:+.2f}%</b>/筆
    （{p.get('backtest_n', 0)} 筆樣本，勝率 {p.get('backtest_wr', 0):.0f}%）
  </div>
  <hr style='margin:6px 0; border:none; border-top:1px dashed #ccc'>
  <div style='font-size:11px; line-height:1.5; color:#444'>
    {reasons_html}
  </div>
</div>
"""
            st.markdown(card_html, unsafe_allow_html=True)

# ════════════════════════════════════════════════════════════
# 📅 行事曆 + 板塊熱度（3 tabs）
# ════════════════════════════════════════════════════════════
st.subheader("📁 深入資訊")
st.caption("💡 手機優化：改用摺疊區塊，點展開才載入內容")
# 7/07 升級：Tabs → Expanders（手機 UX 大改善）
tab_events = st.expander("⏰ 經濟事件（FOMC / CPI / 財報等對你部位有影響的日期）", expanded=False)
tab_earnings = st.expander("📊 個股財報日（未來 30 天）", expanded=False)
tab_sectors = st.expander("🌡️ 板塊熱度（今日 8 大類股表現）", expanded=False)
tab_hot = st.expander("🚀 飆股觀察（3 日內 +20% 以上）", expanded=False)
tab_recs = st.expander("📈 推薦表現追蹤（近 30 天推薦的實際報酬）", expanded=False)
tab_watch = st.expander("👀 觀察名單（你自訂追蹤的股票）", expanded=False)
tab_trades = st.expander("📸 交易截圖記錄（手機隨拍隨傳）", expanded=False)

# ════════════════════════════════════════════════════════════
# 📸 交易截圖上傳（7/07 新增）
# ════════════════════════════════════════════════════════════
with tab_trades:
    import platform as _pf_ts
    _is_cloud_ts = _pf_ts.system() != "Windows"

    st.caption(
        "💡 **推薦流程**：券商 App 下單 → 截圖 → 這裡上傳並填代號 → 送出<br>"
        f"⚠️ {'**雲端版**：截圖只在此 session 顯示，重跑會消失（要永久存請用 Tailscale 桌機版）' if _is_cloud_ts else '**桌機版**：截圖永久存 `data/trades/` 資料夾'}",
        unsafe_allow_html=True,
    )

    _trades_dir = Path("data/trades")
    _trades_dir.mkdir(exist_ok=True)
    _trade_records_f = Path("data/trade_records.jsonl")

    # ── 上傳表單 ──
    with st.form(key="trade_upload_form", clear_on_submit=True):
        _tu_col1, _tu_col2 = st.columns([2, 1])
        _t_code = _tu_col1.text_input("股票代號", placeholder="例如 2337")
        _t_action = _tu_col2.selectbox("動作", ["買", "賣", "加碼", "減碼"])
        _tu_col3, _tu_col4 = st.columns(2)
        _t_shares = _tu_col3.number_input("股數", min_value=0, step=1000, value=0)
        _t_price = _tu_col4.number_input("成交價", min_value=0.0, step=0.05, format="%.2f", value=0.0)
        _t_note = st.text_area("備註（進場理由 / 出場原因 / 心情）", height=68, placeholder="例：跌破停損砍倉、跟推薦榜進場…")
        _t_image = st.file_uploader("📸 截圖檔案", type=["png", "jpg", "jpeg"], accept_multiple_files=False)
        _t_submit = st.form_submit_button("💾 儲存交易記錄", type="primary", use_container_width=True)

    if _t_submit:
        if not _t_code:
            st.error("❌ 請填股票代號")
        elif not _t_image:
            st.error("❌ 請上傳截圖")
        else:
            import datetime as _tdt
            import json as _tjson
            _tw_now_dt = _tdt.datetime.utcnow() + _tdt.timedelta(hours=8)
            _ts_str = _tw_now_dt.strftime("%Y-%m-%d_%H%M%S")
            _ext = _t_image.name.split(".")[-1].lower()
            _filename = f"{_ts_str}_{_t_code}_{_t_action}.{_ext}"
            _filepath = _trades_dir / _filename
            _filepath.write_bytes(_t_image.getbuffer())

            _record = {
                "ts": _tw_now_dt.isoformat(timespec="seconds"),
                "code": _t_code,
                "action": _t_action,
                "shares": int(_t_shares) if _t_shares else None,
                "price": float(_t_price) if _t_price else None,
                "note": _t_note or "",
                "image": _filename,
            }
            with open(_trade_records_f, "a", encoding="utf-8") as _tf:
                _tf.write(_tjson.dumps(_record, ensure_ascii=False) + "\n")

            st.success(f"✅ 已儲存：{_filename}")
            if _is_cloud_ts:
                st.info("⚠️ 雲端 session 重啟後檔案會消失。要永久存請開桌機 Tailscale 版重新上傳。")
            st.rerun()

    st.divider()

    # ── 顯示最近 10 筆 ──
    st.markdown("### 📋 最近交易記錄")
    if not _trade_records_f.exists():
        st.info("尚無交易記錄")
    else:
        import json as _tjson2
        _records = []
        for _line in _trade_records_f.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line: continue
            try:
                _records.append(_tjson2.loads(_line))
            except: pass
        _records.sort(key=lambda x: x.get("ts", ""), reverse=True)
        _records = _records[:10]

        if not _records:
            st.info("尚無交易記錄")
        else:
            for _r in _records:
                _action_color = {"買": "#d62728", "加碼": "#d62728", "賣": "#2ca02c", "減碼": "#2ca02c"}.get(_r.get("action"), "#666")
                _price_str = f"@ {_r['price']:.2f}" if _r.get("price") else ""
                _shares_str = f"{_r['shares']:,} 股" if _r.get("shares") else ""
                _img_path = _trades_dir / _r["image"] if _r.get("image") else None
                with st.container(border=True):
                    _c1, _c2 = st.columns([1, 2])
                    with _c1:
                        if _img_path and _img_path.exists():
                            st.image(str(_img_path), use_container_width=True)
                        else:
                            st.caption("⚠️ 截圖已消失（雲端 session 已重跑）")
                    with _c2:
                        st.markdown(
                            f"<div style='font-weight:700; font-size:16px'>"
                            f"<span style='color:{_action_color}'>{_r.get('action','')}</span> "
                            f"<b>{_r.get('code','')}</b> {_shares_str} {_price_str}"
                            f"</div>"
                            f"<div style='color:#666; font-size:11px'>{_r.get('ts','')}</div>",
                            unsafe_allow_html=True,
                        )
                        if _r.get("note"):
                            st.caption(f"📝 {_r['note']}")

# 預先抓事件，緊急訊號區會用到
upcoming = upcoming_events(30)

with tab_events:
    st.caption("FOMC、CPI、Nvidia/台積電財報等對你部位有影響的日期。可手動編輯 `events.json` 增減。")
    if not upcoming:
        st.info("未來 30 天內無重大事件")
    else:
        impact_color = {"high": "#d62728", "medium": "#f9a825", "low": "#888"}
        cat_emoji = {"fed": "🏦", "cpi": "📈", "earnings": "💰", "policy": "📋", "macro": "🌍"}
        ev_html = "<div style='display:flex; flex-direction:column; gap:6px'>"
        for e in upcoming:
            color = impact_color.get(e["impact"], "#888")
            emo = cat_emoji.get(e["category"], "📌")
            note = f"<br><span style='color:#888; font-size:11px'>{e.get('note','')}</span>" if e.get("note") else ""
            urgency = "⚠️ " if e["days_left"] <= 3 else ""
            ev_html += (
                f"<div style='background:#fff; border-left:4px solid {color}; "
                f"padding:8px 12px; border-radius:4px; color:#1a1a1a'>"
                f"<span style='color:#666; font-size:12px'>{e['date']} (D-{e['days_left']})</span> "
                f"<span style='font-weight:700; margin-left:8px'>{urgency}{emo} {e['name']}</span>"
                f"<span style='background:{color}; color:white; padding:1px 6px; "
                f"border-radius:3px; font-size:10px; margin-left:8px'>{e['impact'].upper()}</span>"
                f"{note}"
                f"</div>"
            )
        ev_html += "</div>"
        st.markdown(ev_html, unsafe_allow_html=True)

with tab_earnings:
    st.caption("台股月營收每月 10 日公布前月 / 季報截止日：5/15、8/14、11/14、3/31")
    earnings_data = portfolio_earnings_calendar(sorted(agg["code"].tolist()))
    name_map = dict(zip(agg["code"], agg["name"]))
    er_rows = []
    for r in earnings_data:
        er_rows.append({
            "代號": r["code"],
            "股名": name_map.get(r["code"], r["code"]),
            "下次月營收日": r["next_revenue_date"],
            "距離(天)": r["next_revenue_days"],
            f"下次季報({r['next_quarterly_name']})": r["next_quarterly_date"],
            "季報距離(天)": r["next_quarterly_days"],
        })
    if er_rows:
        er_df = pd.DataFrame(er_rows)
        st.dataframe(er_df, use_container_width=True, hide_index=True)
        st.caption("💡 月營收前一週可關注「自結」新聞，季報前後波動會放大")

with tab_sectors:
    sectors_data, sectors_date = fetch_sectors_cached()
    if not sectors_data:
        st.warning("分類指數抓取失敗（可能非交易日或 TWSE API 暫時無資料）")
    else:
        # 過濾掉槓桿/反向（不是真實板塊）
        clean = [s for s in sectors_data if "槓桿" not in s["name"] and "反向" not in s["name"]]
        clean.sort(key=lambda x: -x["change_pct"])
        st.caption(f"資料日：{sectors_date}　共 {len(clean)} 類　（漲幅由高到低，幫你看資金在哪）")
        # 視覺：用 4 欄並排，依漲跌上色
        ncols = 4
        cols = st.columns(ncols)
        for i, s in enumerate(clean):
            name_clean = s["name"].replace("類指數", "")
            chg = s["change_pct"]
            if chg >= 3:
                bg = "#d62728"; fg = "white"
            elif chg >= 1:
                bg = "#fde7e9"; fg = "#d62728"
            elif chg >= -1:
                bg = "#f5f5f5"; fg = "#444"
            elif chg >= -3:
                bg = "#e6f4ea"; fg = "#2ca02c"
            else:
                bg = "#2ca02c"; fg = "white"
            with cols[i % ncols]:
                st.markdown(
                    f"<div style='background:{bg}; color:{fg}; padding:8px 10px; "
                    f"border-radius:6px; margin-bottom:6px; text-align:center; font-weight:600'>"
                    f"<div style='font-size:13px'>{name_clean}</div>"
                    f"<div style='font-size:18px'>{chg:+.2f}%</div>"
                    f"</div>",
                    unsafe_allow_html=True,
                )

with tab_hot:
    st.caption(
        "近 5 個交易日累計漲幅 ≥ 20% 的飆股觀察。"
        "**這不是推薦** — 系統不會叫你追高。用途是「題材偵測」+「事後檢討為何沒抓到」。"
    )
    hot_lookback = st.selectbox("回看天數", [3, 5, 10, 20], index=1, key="hot_lb")
    hot_min = st.selectbox("最低漲幅(%)", [10, 15, 20, 30, 50], index=2, key="hot_min")
    hot_data = fetch_hot_movers_cached(instit_by_code, hot_lookback, float(hot_min))
    if not hot_data:
        st.info("近期無符合條件的飆股")
    else:
        from discover import fetch_stock_names as _names
        hot_names = _names([h["code"] for h in hot_data], twse_session())
        hot_rows = []
        for h in hot_data:
            instit_hist = instit_by_code.get(h["code"], [])
            ins_info = analyze_instit(instit_hist) if instit_hist else None
            cd = ins_info["consecutive_days"] if ins_info else 0
            sum5 = ins_info["sum_recent"] if ins_info else 0
            warn = "⚠️ 法人賣超中" if cd <= -3 else ("🟢 法人連買" if cd >= 3 else "")
            hot_rows.append({
                "代號": h["code"],
                "股名": hot_names.get(h["code"], h["code"]),
                f"{hot_lookback}日前": round(h["price_then"], 2),
                "現價": round(h["price"], 2),
                f"{hot_lookback}日漲%": round(h["gain_pct"], 1),
                "今日%": round(h["today_chg"], 2),
                "法人連N日": cd,
                "5日法人累計(張)": sum5,
                "提示": warn,
            })
        hot_df = pd.DataFrame(hot_rows)
        hot_styled = hot_df.style.format({
            f"{hot_lookback}日前": "{:.2f}",
            "現價": "{:.2f}",
            f"{hot_lookback}日漲%": "{:+.1f}",
            "今日%": "{:+.2f}",
            "法人連N日": "{:+d}",
            "5日法人累計(張)": "{:+,d}",
        })
        try:
            hot_styled = hot_styled.map(color_pnl, subset=[f"{hot_lookback}日漲%", "今日%"])
        except AttributeError:
            hot_styled = hot_styled.applymap(color_pnl, subset=[f"{hot_lookback}日漲%", "今日%"])
        st.dataframe(hot_styled, use_container_width=True, hide_index=True)
        st.caption(
            "💡 **怎麼用**：① 看哪些題材在飆（多檔同類型 = 類股輪動）"
            " ② 追高風險高，但可加入觀察名單，等回檔再進"
            " ③「法人賣超中」+ 飆漲 = 散戶推升，主力出貨，**絕對不要追**"
        )

with tab_recs:
    st.caption(
        "📈 系統推薦的真實表現追蹤。"
        "每日 14:35 自動 snapshot 推薦榜，追蹤 T+1/T+5/T+10/T+20 報酬。"
        "**用途**：量化系統真實 alpha vs 大盤。"
    )
    import json as _json
    from pathlib import Path as _Path
    _hist_file = _Path("data/recs_history.jsonl")
    if not _hist_file.exists():
        st.info("尚無追蹤資料 — 排程明天 14:35 開始累積")
    else:
        _recs = []
        for _line in _hist_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line:
                try:
                    _recs.append(_json.loads(_line))
                except Exception:
                    pass
        if not _recs:
            st.info("追蹤資料是空的")
        else:
            # 統計摘要
            _completed = [r for r in _recs if r.get("status") == "completed"]
            _tracking = [r for r in _recs if r.get("status") == "tracking"]
            _c1, _c2, _c3, _c4 = st.columns(4)
            _c1.metric("總追蹤", len(_recs))
            _c2.metric("追蹤中", len(_tracking))
            _c3.metric("已完成 (T+20)", len(_completed))
            # 整體 T+1 平均
            _t1_valid = [r["t1_return_pct"] for r in _recs if r.get("t1_return_pct") is not None]
            if _t1_valid:
                _c4.metric("T+1 平均", f"{sum(_t1_valid)/len(_t1_valid):+.1f}%")

            # 各時間框架表現
            st.markdown("##### 📊 各時間框架平均表現")
            _stats = []
            for _t in [1, 5, 10, 20]:
                _key = f"t{_t}_return_pct"
                _valid = [r[_key] for r in _recs if r.get(_key) is not None]
                if _valid:
                    _avg = sum(_valid) / len(_valid)
                    _wr = sum(1 for v in _valid if v > 0) / len(_valid) * 100
                    _best = max(_valid)
                    _worst = min(_valid)
                    _stats.append({
                        "時間框架": f"T+{_t}",
                        "樣本": len(_valid),
                        "平均報酬%": round(_avg, 2),
                        "勝率%": round(_wr, 0),
                        "最佳%": round(_best, 2),
                        "最差%": round(_worst, 2),
                    })
                else:
                    _stats.append({"時間框架": f"T+{_t}", "樣本": 0,
                                  "平均報酬%": None, "勝率%": None,
                                  "最佳%": None, "最差%": None})
            _stats_df = pd.DataFrame(_stats)
            _stats_styled = _stats_df.style.format({
                "平均報酬%": "{:+.2f}", "勝率%": "{:.0f}",
                "最佳%": "{:+.2f}", "最差%": "{:+.2f}",
            }, na_rep="-")
            try:
                _stats_styled = _stats_styled.map(color_pnl, subset=["平均報酬%", "最佳%", "最差%"])
            except AttributeError:
                _stats_styled = _stats_styled.applymap(color_pnl, subset=["平均報酬%", "最佳%", "最差%"])
            st.dataframe(_stats_styled, use_container_width=True, hide_index=True)

            # 最近 20 筆明細
            st.markdown("##### 📋 最近 20 筆推薦明細")
            _recent = sorted(_recs, key=lambda r: (r["snapshot_date"], r.get("rank", 99)), reverse=True)[:20]
            _detail_rows = []
            for r in _recent:
                _detail_rows.append({
                    "日期": r["snapshot_date"],
                    "代號": r["code"],
                    "股名": r.get("name", ""),
                    "評分": r["score"],
                    "EV%": r.get("backtest_ev"),
                    "進場價": r["entry_price"],
                    "T+1%": r.get("t1_return_pct"),
                    "T+5%": r.get("t5_return_pct"),
                    "T+10%": r.get("t10_return_pct"),
                    "T+20%": r.get("t20_return_pct"),
                    "最大獲利%": r.get("max_gain_pct"),
                    "最大回撤%": r.get("max_drawdown_pct"),
                    "狀態": r["status"],
                })
            _detail_df = pd.DataFrame(_detail_rows)
            _detail_styled = _detail_df.style.format({
                "EV%": "{:+.2f}", "進場價": "{:.2f}",
                "T+1%": "{:+.2f}", "T+5%": "{:+.2f}",
                "T+10%": "{:+.2f}", "T+20%": "{:+.2f}",
                "最大獲利%": "{:+.2f}", "最大回撤%": "{:+.2f}",
                "評分": "{:+d}",
            }, na_rep="-")
            try:
                _detail_styled = _detail_styled.map(
                    color_pnl, subset=["T+1%", "T+5%", "T+10%", "T+20%", "最大獲利%", "最大回撤%"]
                )
            except AttributeError:
                _detail_styled = _detail_styled.applymap(
                    color_pnl, subset=["T+1%", "T+5%", "T+10%", "T+20%", "最大獲利%", "最大回撤%"]
                )
            st.dataframe(_detail_styled, use_container_width=True, hide_index=True, height=500)
            st.caption(
                "💡 **怎麼解讀**：① T+1 勝率高 = 隔日跳空效應強"
                " ② T+20 才是真實能力（噪音洗掉）"
                " ③ 最大回撤大 = 進場後波動大，需嚴格停損"
                " ④ 1 個月後可看「系統推薦 vs TAIEX 同期 alpha」"
            )

with tab_watch:
    st.caption(
        "👀 觀察名單 — 未達進場條件但有潛力的標的，每日 14:40 排程自動檢查。"
        "任一觸發條件達成 → Telegram 推播「觀察名單觸發」。"
        "修改 `data/watchlist.json` 增減標的。"
    )
    import json as _jj
    from pathlib import Path as _PP
    _wl_file = _PP("data/watchlist.json")
    if not _wl_file.exists():
        st.info("觀察名單尚未建立 — 編輯 data/watchlist.json 新增標的")
    else:
        try:
            _wl = _jj.loads(_wl_file.read_text(encoding="utf-8"))
            _stocks = {k: v for k, v in _wl.items() if not k.startswith("_")}
            if not _stocks:
                st.info("觀察名單為空")
            else:
                for _code, _info in _stocks.items():
                    with st.expander(f"⭐ {_code} {_info.get('name', _code)} — {_info.get('reason', '')[:40]}", expanded=True):
                        _c1, _c2 = st.columns([1, 2])
                        _c1.metric("加入日期", _info.get("added_date", "-"))
                        _c1.metric("加入時價", f"{_info.get('current_price', 0):.2f}")
                        _c1.caption(f"市場：{_info.get('market', '-')}")
                        _c2.markdown(f"**加入理由**：{_info.get('reason', '')}")
                        _tc = _info.get("trigger_condition", {})
                        if _tc.get("any_of"):
                            _c2.markdown("**觸發條件（任一達成）**：")
                            for _cond in _tc["any_of"]:
                                _c2.markdown(f"- {_cond}")
                        if _info.get("notes"):
                            _c2.caption(f"📝 {_info['notes']}")
        except Exception as e:
            st.warning(f"讀取失敗: {e}")

# ════════════════════════════════════════════════════════════
# 🚨 緊急訊號 + 特別關注
# ════════════════════════════════════════════════════════════
st.subheader("🚨 緊急訊號")
big_sigs: list[tuple[str, str, str]] = []
if idx_info and idx_info["change_pct"] <= THRESH["index_drop"]:
    big_sigs.append(("🔴", "大盤", f"加權指數 {idx_info['change_pct']:+.2f}%（總經風險）"))
big_sigs.extend(macro_notes)

# 高影響事件 3 日內 → 推進緊急訊號區
for ev in upcoming:
    if ev["impact"] == "high" and ev["days_left"] <= 3:
        big_sigs.append((
            "🔴", "行事曆",
            f"D-{ev['days_left']} {ev['name']}（{ev['date']}）— 提前準備倉位",
        ))
display_sigs = big_sigs + all_signals

# 額外把 ⭐⭐ 特別關注的也加進來
for r in ranked:
    if r["attention"] == 2:
        emo = "🟢" if r["combined"] > 0 else "🔴" if r["combined"] < 0 else "🟡"
        reason = "; ".join(r["attention_reasons"])
        display_sigs.append((emo, f"⭐⭐ {r['name']}", reason))

if not display_sigs:
    st.success("目前無異常訊號 ✓")
else:
    red_sigs = [s for s in display_sigs if s[0] == "🔴"]
    yellow_sigs = [s for s in display_sigs if s[0] == "🟡"]
    green_sigs = [s for s in display_sigs if s[0] == "🟢"]
    sig_cols = st.columns(3)
    for col, sigs, header_bg in [
        (sig_cols[0], red_sigs, "#fde7e9"),
        (sig_cols[1], yellow_sigs, "#fff8e1"),
        (sig_cols[2], green_sigs, "#e6f4ea"),
    ]:
        emo = sigs[0][0] if sigs else ("🔴" if col is sig_cols[0]
                                       else "🟡" if col is sig_cols[1] else "🟢")
        with col:
            st.markdown(
                f"<div style='background:{header_bg}; color:#1a1a1a; padding:4px 10px; "
                f"border-radius:4px; font-weight:600; margin-bottom:6px'>"
                f"{emo} {len(sigs)} 項</div>",
                unsafe_allow_html=True,
            )
            if not sigs:
                st.caption("（無）")
            else:
                for _, name, txt in sigs:
                    st.markdown(f"**{name}** — {txt}")

# 桌面通知：只通知本次新增的紅色訊號
if "notified" not in st.session_state:
    st.session_state.notified = set()
critical_now = {(name, txt) for emoji, name, txt in display_sigs if emoji == "🔴"}
new_critical = critical_now - st.session_state.notified
for name, txt in new_critical:
    desktop_notify(f"🔴 {name}", txt)
st.session_state.notified = critical_now

# ════════════════════════════════════════════════════════════
# 🌐 總經 + 夜盤儀表板（折疊）
# ════════════════════════════════════════════════════════════
with st.expander("🌐 總經 + 夜盤指標（點開查看）", expanded=False):
    st.caption("**日盤**：S&P500 隱含波動率、費城半導體、美元指數（美股盤後資料）")
    macro_cols = st.columns(3)
    if "VIX" in macro_data:
        _macro_metric(macro_cols[0], macro_data["VIX"])
    if "SOX" in macro_data:
        _macro_metric(macro_cols[1], macro_data["SOX"], fmt="{:,.0f}")
    if "DXY" in macro_data:
        _macro_metric(macro_cols[2], macro_data["DXY"])

    st.caption("**夜盤**：美股期貨 + 台積電 ADR（24 小時即時，反映歐美盤對台股隔天開盤預期）")
    night_cols = st.columns(3)
    if "ES" in night_data:
        _macro_metric(night_cols[0], night_data["ES"], fmt="{:,.0f}")
    if "NQ" in night_data:
        _macro_metric(night_cols[1], night_data["NQ"], fmt="{:,.0f}")
    if "TSM" in night_data:
        _macro_metric(night_cols[2], night_data["TSM"])

# ════════════════════════════════════════════════════════════
# 📊 持倉明細 + 類別配置（折疊）
# ════════════════════════════════════════════════════════════
with st.expander("📊 持倉明細 + 類別配置（點開查看）", expanded=False):
    # ★ 槓桿/反向 ETF 警告 banner
    _leveraged_etfs = {
        "00631L": "元大台灣50正2",
        "00632R": "元大台灣50反1",
        "00633L": "富邦上証正2",
        "00634R": "富邦上証反1",
        "00637L": "元大滬深300正2",
        "00638R": "元大滬深300反1",
        "00640L": "富邦日本正2",
        "00655L": "國泰中國A50正2",
        "00656R": "國泰中國A50反1",
        "00666R": "富邦VIX",
        "00670L": "富邦NASDAQ正2",
        "00669R": "國泰美國道瓊反1",
        "00676L": "富邦S&P500正2",
        "00677U": "富邦VIX短期",
    }
    _held_leveraged = []
    for _code in df["代號"].dropna().tolist():
        if _code in _leveraged_etfs:
            _held_leveraged.append((_code, _leveraged_etfs[_code]))
    if _held_leveraged:
        _msg_items = "、".join(f"{c} {n}" for c, n in _held_leveraged)
        st.markdown(
            f"<div style='background:#fff8e1; border:2px solid #f9a825; "
            f"border-radius:6px; padding:10px 14px; margin-bottom:12px; color:#1a1a1a;'>"
            f"<div style='font-weight:700; color:#e65100; font-size:14px'>"
            f"⚠️ 槓桿 / 反向 ETF 警告</div>"
            f"<div style='font-size:12px; margin-top:4px'>"
            f"持有：<b>{_msg_items}</b><br>"
            f"這類 ETF <b>每日重設</b>，長期持有會複利衰減。<br>"
            f"<b>建議持有 ≤ 10 個交易日</b>，超過每 3 日檢查一次。"
            f"</div></div>",
            unsafe_allow_html=True,
        )

    # 類別小計
    st.markdown("##### 🗂 類別配置")
    cat_summary = (
        df.dropna(subset=["投資成本"])
        .groupby("類別", as_index=False)
        .agg(投資成本=("投資成本", "sum"), 帳面收入=("帳面收入", "sum"),
             檔數=("代號", "count"))
    )
    cat_summary["損益"] = cat_summary["帳面收入"] - cat_summary["投資成本"]
    cat_summary["損益%"] = cat_summary["損益"] / cat_summary["投資成本"] * 100
    cat_summary["佔比%"] = cat_summary["投資成本"] / cat_summary["投資成本"].sum() * 100
    cat_summary["__order"] = cat_summary["類別"].map(
        {c: i for i, c in enumerate(CATEGORY_ORDER)}
    ).fillna(99).astype(int)
    cat_summary = cat_summary.sort_values("__order").drop(columns="__order").reset_index(drop=True)

    cat_styled = (
        cat_summary.style
        .format({
            "投資成本": "{:,.0f}", "帳面收入": "{:,.0f}",
            "損益": "{:+,.0f}", "損益%": "{:+.2f}", "佔比%": "{:.1f}",
        })
    )
    try:
        cat_styled = cat_styled.map(color_pnl, subset=["損益", "損益%"])
        cat_styled = cat_styled.map(color_category, subset=["類別"])
    except AttributeError:
        cat_styled = cat_styled.applymap(color_pnl, subset=["損益", "損益%"])
        cat_styled = cat_styled.applymap(color_category, subset=["類別"])
    st.dataframe(cat_styled, use_container_width=True, hide_index=True)

    # 持倉明細
    st.markdown("##### 📊 持倉明細")
    styled = (
        df.style
        .format({
            "成本": "{:.2f}",
            "停損價": "{:.2f}",
            "Trail 停損": "{:.2f}",
            "現價": "{:.2f}",
            "漲跌%": "{:+.2f}",
            "投資成本": "{:,.0f}",
            "帳面收入": "{:,.0f}",
            "損益": "{:+,.0f}",
            "損益%": "{:+.2f}",
            "主力評分": "{:+d}",
        }, na_rep="-")
    )
    try:
        styled = styled.map(color_pnl, subset=["漲跌%", "損益", "損益%"])
        styled = styled.map(color_score, subset=["主力評分"])
        styled = styled.map(color_category, subset=["類別"])
    except AttributeError:
        styled = styled.applymap(color_pnl, subset=["漲跌%", "損益", "損益%"])
        styled = styled.applymap(color_score, subset=["主力評分"])
        styled = styled.applymap(color_category, subset=["類別"])
    st.dataframe(styled, use_container_width=True, height=620, hide_index=True)

# ──────────────── 極簡模式關鍵分界線 ────────────────
# 以下都是「重」section（含 plotly 圖、回測、新聞），手機載入最慢
# 極簡模式直接 stop()，下方完全不渲染 → 速度快 5x
if LITE_MODE:
    st.info("🚀 極簡模式 — K 線圖、策略回測、新聞 已隱藏。關閉左側 toggle 可看完整版。")
    st.stop()


# ──────────────── 主力動向分析（K線 + 評分明細 + 法人） ────────────────
st.subheader("🎯 主力動向分析")
st.caption(
    "綜合 MACD / KD / 均線 / K線型態 / 量價 / 三大法人 / 乖離率，"
    "計算主力評分（-10 ~ +10），輔助判斷加碼或減碼。"
)

# 預設選有訊號或評分最強烈的那檔
default_code = None
if stock_details:
    sorted_codes = sorted(stock_details.keys(),
                          key=lambda c: abs(stock_details[c]["score"].get("score", 0)),
                          reverse=True)
    default_code = sorted_codes[0]

def _att_marker(level: int) -> str:
    return {0: "", 1: "⭐", 2: "⭐⭐"}.get(level, "")


stock_options = {
    f'{_att_marker(d["score"].get("attention_level", 0))}{d["score"]["verdict_emoji"]} '
    f'{code} {d["name"]} '
    f'(評分 {d["score"]["score"]:+d}: {d["score"]["verdict"]})': code
    for code, d in stock_details.items()
}
selected_label = st.selectbox(
    "選擇個股查看詳細分析",
    list(stock_options.keys()),
    index=list(stock_options.values()).index(default_code) if default_code else 0,
)
selected_code = stock_options[selected_label]
sel = stock_details[selected_code]

# 評分卡 + 結論（綜合個股評分 + 總經修正）
stock_s = sel["score"]["score"]
# 總經當作 ±2 的修正（不會完全推翻個股訊號，但會降級/升級）
macro_adj = max(-2, min(2, macro_score // 2 if macro_score else 0))
combined = max(-10, min(10, stock_s + macro_adj))

if combined >= 6:
    final_verdict, final_emo = "強烈加碼", "🟢🟢"
elif combined >= 3:
    final_verdict, final_emo = "偏多（可逢低加碼）", "🟢"
elif combined >= -2:
    final_verdict, final_emo = "中性觀望", "⚪"
elif combined >= -5:
    final_verdict, final_emo = "偏空（可逢高減碼）", "🔴"
else:
    final_verdict, final_emo = "強烈減碼", "🔴🔴"

# 熊市時：把加碼類建議改為暫停（不影響減碼）
if market_regime["regime"] == "bear" and combined >= 3:
    final_verdict = f"⚠️ 熊市暫停加碼建議（系統評分 {combined:+d}）"
    final_emo = "🔴"
elif market_regime["regime"] == "warning" and combined >= 3:
    final_verdict = f"🟡 警戒：減半部位（系統評分 {combined:+d}）"
    final_emo = "🟡"

# 特別關注標記
attn = sel["score"].get("attention_level", 0)
attn_reasons = sel["score"].get("attention_reasons", [])
if attn == 2:
    st.error(f"⭐⭐ **特別關注**：{'; '.join(attn_reasons)}")
elif attn == 1:
    st.warning(f"⭐ **注意**：{'; '.join(attn_reasons)}")

col_a, col_b, col_c = st.columns([1, 1, 2])
col_a.metric(
    "綜合評分",
    f"{combined:+d} / ±10",
    f"個股 {stock_s:+d}　總經 {macro_adj:+d}",
)
col_b.metric("建議", f"{final_emo} {final_verdict}")
if sel["instit_info"]:
    cd = sel["instit_info"]["consecutive_days"]
    latest = sel["instit_info"]["latest_net"]
    sum5 = sel["instit_info"]["sum_recent"]
    col_c.metric(
        "三大法人（最近一日 / 5日累計）",
        f"{latest:+,} 張",
        f"連續 {'買' if cd >= 0 else '賣'}超 {abs(cd)} 日，5 日累計 {sum5:+,}",
        delta_color="normal" if cd >= 0 else "inverse",
    )
else:
    col_c.caption("⚠️ 此股無三大法人資料（可能是非交易日或上櫃資料延遲）")

# 評分明細
if sel["score"]["breakdown"]:
    with st.expander("📋 評分明細", expanded=True):
        bd_df = pd.DataFrame(
            sel["score"]["breakdown"], columns=["類別", "分數", "說明"]
        )
        bd_df["分數"] = bd_df["分數"].apply(lambda v: f"{v:+d}")
        st.dataframe(bd_df, use_container_width=True, hide_index=True)

# K線圖
fig = make_chart(sel["hist"], name=f"{selected_code} {sel['name']}", days=80)
if fig is not None:
    st.plotly_chart(fig, use_container_width=True)
else:
    st.warning("歷史資料不足，無法繪製 K線圖")

# 三大法人歷史柱狀圖
if sel["instit_hist"] and instit_dates:
    import plotly.graph_objects as _go
    instit_df = pd.DataFrame({
        "日期": instit_dates[-len(sel["instit_hist"]):],
        "淨買賣超(張)": sel["instit_hist"],
    })
    inst_colors = ["#d62728" if v >= 0 else "#2ca02c" for v in instit_df["淨買賣超(張)"]]
    fig2 = _go.Figure(data=[
        _go.Bar(x=instit_df["日期"], y=instit_df["淨買賣超(張)"],
                marker_color=inst_colors,
                text=[f"{v:+,}" for v in instit_df["淨買賣超(張)"]],
                textposition="outside"),
    ])
    fig2.update_layout(
        title=f"三大法人 近 {len(sel['instit_hist'])} 日淨買賣超（張）",
        height=280, margin=dict(l=40, r=40, t=50, b=40),
        showlegend=False,
    )
    fig2.add_hline(y=0, line_color="gray", line_width=1)
    st.plotly_chart(fig2, use_container_width=True)

# ════════════════════════════════════════════════════════════
# 🧪 策略回測（按鈕觸發，避免每次刷新都跑）
# ════════════════════════════════════════════════════════════
st.subheader("🧪 策略回測")
st.caption(
    "回測過去 2 年，看「評分 ≥ 閾值時買進、固定持有 N 日後賣出」的真實勝率/期望值。"
    "已扣除來回交易成本（一般股 0.585%、ETF 0.385%）。**不含三大法人因子**（歷史抓取成本太高）。"
)

bt_c1, bt_c2, bt_c3 = st.columns([1, 1, 1])
bt_threshold = bt_c1.selectbox("評分閾值", [3, 4, 5, 6], index=0, key="bt_thr")
bt_period = bt_c2.selectbox(
    "回測區間",
    ["2024-2025 多頭", "2022 熊市", "2018-2019 震盪", "近 2 年"],
    index=3, key="bt_period",
)
bt_mode = bt_c3.selectbox(
    "出場模式",
    ["V0 固定持有", "V1 +市場濾鏡", "V2 +濾鏡+智能出場"],
    index=2, key="bt_mode",
)

# V0/V1 用固定持有；V2 用智能出場
if bt_mode == "V2 +濾鏡+智能出場":
    bt_e1, bt_e2, bt_e3, bt_e4 = st.columns(4)
    bt_stop_loss = bt_e1.number_input("停損 %", value=-5.0, step=1.0, key="bt_sl")
    bt_take_profit = bt_e2.number_input("停利 %", value=10.0, step=1.0, key="bt_tp")
    bt_score_exit = bt_e3.number_input("趨勢反轉閾值", value=-2, step=1, key="bt_se")
    bt_max_hold = bt_e4.number_input("最大持有日", value=30, step=5, key="bt_mh")
    bt_hold_days_setting = 10  # 不會被用到
else:
    bt_hold_days_setting = st.selectbox("持有天數", [5, 10, 20], index=1, key="bt_hold")
    bt_stop_loss, bt_take_profit, bt_score_exit, bt_max_hold = -5.0, 10.0, -2, 30

run_bt = st.button("🚀 跑回測", type="primary")

if run_bt:
    period_ranges = {
        "2024-2025 多頭": ("2024-01-01", "2025-12-31"),
        "2022 熊市":     ("2022-01-01", "2022-12-31"),
        "2018-2019 震盪": ("2018-01-01", "2019-12-31"),
        "近 2 年":       (None, None),
    }
    bt_start, bt_end = period_ranges[bt_period]

    with st.spinner(f"抓歷史 + 計算中（{bt_mode}，約 15-60 秒）…"):
        codes_market_for_bt = tuple(
            (c, m) for c, m in raw_df[["code", "market"]].drop_duplicates().itertuples(index=False, name=None)
        )
        # V0/V1/V2 需要不同年份的歷史
        if bt_period in ("2018-2019 震盪", "2022 熊市"):
            bt_hists = {}
            for c, m in codes_market_for_bt:
                suffix = "TW" if m == "TSE" else "TWO"
                try:
                    h = yf.Ticker(f"{c}.{suffix}").history(start="2017-01-01", end="2026-12-31", auto_adjust=False)
                    if len(h) > 100:
                        bt_hists[c] = h
                except Exception:
                    continue
        else:
            bt_hists = fetch_history_2y(codes_market_for_bt)

        # 抓 TAIEX 給市場濾鏡
        bt_taiex = None
        if bt_mode != "V0 固定持有":
            try:
                bt_taiex = yf.Ticker("^TWII").history(start="2017-01-01", auto_adjust=False)
            except Exception:
                bt_taiex = None

        use_filter = bt_mode != "V0 固定持有"
        use_smart = bt_mode == "V2 +濾鏡+智能出場"
        all_trades, overall, per_stock = run_portfolio_backtest(
            bt_hists,
            signal_threshold=bt_threshold,
            hold_days=bt_hold_days_setting,
            start_date=bt_start, end_date=bt_end,
            taiex_hist=bt_taiex,
            use_market_filter=use_filter,
            use_smart_exit=use_smart,
            stop_loss_pct=bt_stop_loss,
            take_profit_pct=bt_take_profit,
            score_exit_threshold=int(bt_score_exit),
            max_hold_days=int(bt_max_hold),
        )

    # 結論卡
    emo, txt = backtest_verdict(overall)
    if overall["total_trades"] < 30:
        st.warning(f"⚠️ 樣本數只有 {overall['total_trades']} 筆，統計信賴度不足，請斟酌參考")
    bt_v1, bt_v2, bt_v3, bt_v4 = st.columns(4)
    bt_v1.metric("總交易筆數", f"{overall['total_trades']}")
    bt_v2.metric("勝率", f"{overall['win_rate']:.1f}%")
    bt_v3.metric(
        "期望值 / 筆",
        f"{overall['expectancy']:+.2f}%",
        delta_color="normal" if overall["expectancy"] > 0 else "inverse",
    )
    bt_v4.metric("系統判定", f"{emo} {txt}")

    # 詳細統計
    detail_c1, detail_c2 = st.columns(2)
    with detail_c1:
        st.markdown("**📈 報酬統計**")
        st.markdown(f"- 平均賺（贏的時候）：**{overall['avg_win']:+.2f}%**")
        st.markdown(f"- 平均賠（輸的時候）：**{overall['avg_loss']:+.2f}%**")
        pf = overall.get("profit_factor")
        st.markdown(f"- 獲利因子（總賺/總賠）：**{pf if pf else '∞'}**")
        st.markdown(f"- 最佳單筆：{overall['best_trade']:+.2f}%　最差單筆：{overall['worst_trade']:+.2f}%")
    with detail_c2:
        st.markdown("**⚠️ 風險統計**")
        st.markdown(f"- 中位數報酬：{overall['median_return']:+.2f}%（一半的交易高於這個）")
        st.markdown(f"- 累計加總（線性報酬）：{overall['total_sum_pct']:+.1f}%")
        st.markdown(f"- 最大回撤（累計）：**{overall['max_drawdown_pct']:.1f}%**")
        st.markdown(f"- 最大連敗次數：**{overall['max_consecutive_losses']} 次**（心理壓力測試）")

    # 解讀提示
    if overall["total_trades"] > 0:
        ev = overall["expectancy"]
        pf = overall.get("profit_factor") or 0
        if ev > 1.5 and pf > 1.5:
            st.success(
                "✅ **過去 2 年這套有 edge**：期望值 +1.5% 以上、獲利因子 >1.5。但要注意——"
                "過去 2 年是 AI/半導體大多頭，趨勢系統在多頭裡會偏高。建議再跑一次「2018-2019」"
                "或「2022 熊市」做穩健性測試。"
            )
        elif ev > 0:
            st.info("ℹ️ **微弱邊緣**：勉強打平或略有 edge，但樣本數和市場循環的影響很大，謹慎使用。")
        else:
            st.error("❌ **這套不行**：期望值為負，照做長期會賠。需要重新調整評分權重或加入新因子。")

    # 出場原因拆解（V2 才有意義）
    if bt_mode == "V2 +濾鏡+智能出場" and all_trades:
        st.markdown("**🚪 出場原因拆解**")
        reason_breakdown = exit_reason_breakdown(all_trades)
        reason_zh = {
            "stop_loss": "停損", "take_profit": "停利",
            "trend_reverse": "趨勢反轉", "time_out": "持有期滿",
        }
        reason_rows = []
        for reason, stat in sorted(reason_breakdown.items(), key=lambda x: -x[1]["count"]):
            reason_rows.append({
                "出場原因": reason_zh.get(reason, reason),
                "筆數": stat["count"],
                "佔比%": stat["count"] / len(all_trades) * 100,
                "勝率%": stat["win_rate"],
                "平均報酬%": stat["avg_return"],
            })
        if reason_rows:
            r_df = pd.DataFrame(reason_rows)
            r_styled = r_df.style.format({
                "佔比%": "{:.1f}", "勝率%": "{:.1f}", "平均報酬%": "{:+.2f}",
            })
            try:
                r_styled = r_styled.map(color_pnl, subset=["平均報酬%"])
            except AttributeError:
                r_styled = r_styled.applymap(color_pnl, subset=["平均報酬%"])
            st.dataframe(r_styled, use_container_width=True, hide_index=True)

    # 個股拆解
    st.markdown("**🔍 個股拆解（看哪檔最賺、哪檔拖後腿）**")
    per_rows = []
    for code, stats in per_stock.items():
        if stats["total_trades"] == 0:
            continue
        per_rows.append({
            "代號": code,
            "股名": stock_details.get(code, {}).get("name", code),
            "交易數": stats["total_trades"],
            "勝率%": stats["win_rate"],
            "平均賺%": stats["avg_win"],
            "平均賠%": stats["avg_loss"],
            "期望值%": stats["expectancy"],
            "最佳%": stats["best_trade"],
            "最差%": stats["worst_trade"],
            "最大連敗": stats["max_consecutive_losses"],
        })
    if per_rows:
        per_df = pd.DataFrame(per_rows).sort_values("期望值%", ascending=False)
        per_styled = per_df.style.format({
            "勝率%": "{:.0f}%",
            "平均賺%": "{:+.2f}",
            "平均賠%": "{:+.2f}",
            "期望值%": "{:+.2f}",
            "最佳%": "{:+.2f}",
            "最差%": "{:+.2f}",
        })
        try:
            per_styled = per_styled.map(color_pnl, subset=["期望值%"])
        except AttributeError:
            per_styled = per_styled.applymap(color_pnl, subset=["期望值%"])
        st.dataframe(per_styled, use_container_width=True, hide_index=True)

    # 累計報酬曲線
    if all_trades:
        import plotly.graph_objects as _go
        eq_df = pd.DataFrame(all_trades).sort_values("exit_date").reset_index(drop=True)
        eq_df["累計報酬%"] = eq_df["return_pct"].cumsum()
        eq_df["回撤%"] = eq_df["累計報酬%"] - eq_df["累計報酬%"].cummax()
        eq_fig = _go.Figure()
        eq_fig.add_trace(_go.Scatter(
            x=list(range(1, len(eq_df) + 1)), y=eq_df["累計報酬%"],
            mode="lines", name="累計加總報酬%",
            line=dict(color="#d62728", width=2),
        ))
        eq_fig.add_trace(_go.Scatter(
            x=list(range(1, len(eq_df) + 1)), y=eq_df["回撤%"],
            mode="lines", name="回撤%",
            line=dict(color="#2ca02c", width=1, dash="dot"),
            fill="tozeroy", fillcolor="rgba(44,160,44,0.1)",
        ))
        eq_fig.update_layout(
            title="累計加總報酬曲線（每筆同金額，非複利）",
            xaxis_title="第 N 筆交易",
            yaxis_title="累計報酬 (%)",
            height=350, margin=dict(l=40, r=40, t=60, b=40),
            hovermode="x unified",
        )
        st.plotly_chart(eq_fig, use_container_width=True)

    # 誠實提醒
    with st.expander("📖 怎麼看這份報告（一定要讀）", expanded=False):
        st.markdown("""
**勝率不是重點，期望值才是。**
- 勝率 60% 賺賠比 1:1 → 期望值正
- 勝率 40% 賺賠比 1:3 → 期望值也正
- **長期靠期望值賺錢，不是猜得很準**

**這份回測有以下限制：**
1. **過去 2 年市況偏多頭**，趨勢系統在多頭裡天生偏好。真實 edge 要在熊市/震盪市也驗證
2. **沒含三大法人因子**，實際線上系統會比這個強一些
3. **假設能買在收盤、賣在收盤**，實際有滑點 0.1-0.3%
4. **未考慮個股下市/處置/減資**，存活者偏差
5. **樣本數**：每檔約 5-20 次訊號，建議 30+ 樣本才有統計意義

**判定標準：**
- 期望值 > +1.5% 且 獲利因子 > 1.5 → 系統有真實 edge
- 期望值 +0.5% ~ +1.5% → 微弱 edge，可參考但別重壓
- 期望值 < 0 → 不要照做
        """)

# ──────────────── 新聞 ────────────────
st.subheader("📰 個股新聞快訊")
st.caption("優先顯示有訊號的個股；無訊號時顯示前 6 檔")

signal_names = list(dict.fromkeys(name for _, name, _ in all_signals))
focus = signal_names if signal_names else df["股名"].dropna().tolist()[:6]
focus = focus[:6]

if not focus:
    st.caption("無資料")
else:
    cols = st.columns(min(3, len(focus)))
    for i, name in enumerate(focus):
        with cols[i % len(cols)]:
            st.markdown(f"**{name}**")
            items = fetch_news(name, limit=3)
            if not items:
                st.caption("（暫無新聞）")
            for it in items:
                st.markdown(f"- [{it['title'][:50]}]({it['link']})")

