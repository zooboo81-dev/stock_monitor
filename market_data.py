"""交易所官方日收盤 — 用來補 yfinance 漏掉的最新一根 K

為什麼有這支（2026-11-11）：
  雲端排程跑推薦榜時，yfinance 常常還沒有「今天」的日 K（10 月有 8/12 筆、11 月 4/4 筆
  是拿前一天的收盤在算分），結果：
    - 訊號慢一天（例：11/05 推立隆電時，它當天其實已經跌 9%，系統不知道）
    - 「T+1 報酬」其實是推薦當天已經發生的漲跌，戰績統計失真
  修法：抓完 yfinance 後，一律拿證交所／櫃買中心的官方當日收盤來對，缺就補上。

資料來源：
  上市  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999
  上櫃  https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

TW_TZ = timezone(timedelta(hours=8))


def taiwan_today() -> date:
    """台灣日期（雲端主機是 UTC，而且排程常延遲到台灣隔天凌晨才跑，不能用 date.today()）"""
    return datetime.now(TW_TZ).date()


def _num(s) -> float | None:
    try:
        v = float(str(s).replace(",", "").strip())
        return v if v > 0 else None
    except (ValueError, TypeError):
        return None


_MI_CACHE: dict = {"at": 0.0, "result": None}


def _fetch_mi_index(session: requests.Session, max_back: int = 10) -> tuple[date | None, dict[str, dict], float | None]:
    """證交所 MI_INDEX：最近一個交易日的 (日期, 全市場個股 OHLCV, 加權指數收盤)。10 分鐘內重複呼叫用快取。"""
    if _MI_CACHE["result"] is not None and time.time() - _MI_CACHE["at"] < 600:
        return _MI_CACHE["result"]
    d = taiwan_today()
    for _ in range(max_back):
        if d.weekday() < 5:
            url = (f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
                   f"?date={d.strftime('%Y%m%d')}&type=ALLBUT0999&response=json")
            try:
                j = session.get(url, timeout=15).json()
            except Exception:
                j = {}
            if j.get("stat") == "OK":
                stocks: dict[str, dict] = {}
                taiex_close: float | None = None
                for t in j.get("tables", []):
                    f = t.get("fields") or []
                    if "證券代號" in f and "收盤價" in f:
                        ix = {k: f.index(k) for k in ("證券代號", "成交股數", "開盤價", "最高價", "最低價", "收盤價")}
                        for row in t.get("data", []):
                            c = _num(row[ix["收盤價"]])
                            o = _num(row[ix["開盤價"]])
                            if c is None or o is None:
                                continue  # 當日無成交
                            stocks[str(row[ix["證券代號"]]).strip()] = {
                                "Open": o, "High": _num(row[ix["最高價"]]) or c,
                                "Low": _num(row[ix["最低價"]]) or c, "Close": c,
                                "Volume": _num(row[ix["成交股數"]]) or 0.0,
                            }
                    elif "指數" in f and "收盤指數" in f and taiex_close is None:
                        for row in t.get("data", []):
                            if str(row[f.index("指數")]).strip() == "發行量加權股價指數":
                                taiex_close = _num(row[f.index("收盤指數")])
                                break
                if stocks:
                    _MI_CACHE.update(at=time.time(), result=(d, stocks, taiex_close))
                    return d, stocks, taiex_close
            time.sleep(1.2)  # 證交所限流
        d -= timedelta(days=1)
    return None, {}, None


def fetch_twse_daily(session: requests.Session, max_back: int = 10) -> tuple[date | None, dict[str, dict]]:
    """上市最近一個交易日的全市場 OHLCV。回傳 (日期, {代號: {Open,High,Low,Close,Volume}})"""
    d, stocks, _ = _fetch_mi_index(session, max_back)
    return d, stocks


def patch_index_bar(taiex_hist: pd.DataFrame, session: requests.Session | None = None) -> pd.DataFrame:
    """加權指數歷史缺最新一天時，用證交所官方收盤指數補上（開高低用收盤代替，趨勢只看收盤）。"""
    if taiex_hist is None or taiex_hist.empty:
        return taiex_hist
    try:
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = "Mozilla/5.0"
            try:
                import truststore
                truststore.inject_into_ssl()
            except ImportError:
                pass
        d, _, close = _fetch_mi_index(session)
        if d is None or not close or _last_date(taiex_hist) >= d:
            return taiex_hist
        return _append_bar(taiex_hist, d, {"Open": close, "High": close, "Low": close,
                                           "Close": close, "Volume": 0.0})
    except Exception:
        return taiex_hist


def fetch_tpex_daily(session: requests.Session) -> tuple[date | None, dict[str, dict]]:
    """上櫃最近一個交易日的全市場 OHLCV（OpenAPI 只給最新一日）"""
    try:
        data = session.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",
                           timeout=20).json()
    except Exception:
        return None, {}
    out: dict[str, dict] = {}
    d_obj: date | None = None
    for r in data or []:
        code = str(r.get("SecuritiesCompanyCode", "")).strip()
        c, o = _num(r.get("Close")), _num(r.get("Open"))
        if not code or c is None or o is None:
            continue
        out[code] = {"Open": o, "High": _num(r.get("High")) or c, "Low": _num(r.get("Low")) or c,
                     "Close": c, "Volume": _num(r.get("TradingShares")) or 0.0}
        if d_obj is None:
            roc = str(r.get("Date", ""))
            if len(roc) == 7:
                try:
                    d_obj = date(int(roc[:3]) + 1911, int(roc[3:5]), int(roc[5:7]))
                except ValueError:
                    pass
    return d_obj, out


def _last_date(df: pd.DataFrame) -> date:
    return pd.Timestamp(df.index[-1]).date()


def _append_bar(df: pd.DataFrame, d: date, bar: dict) -> pd.DataFrame:
    ts = pd.Timestamp(d)
    tz = getattr(df.index, "tz", None)
    if tz is not None:
        ts = ts.tz_localize(tz)
    row = {col: bar.get(col) for col in df.columns if col in bar}
    if "Adj Close" in df.columns:
        row["Adj Close"] = bar["Close"]
    new = pd.DataFrame([row], index=pd.DatetimeIndex([ts], name=df.index.name))
    return pd.concat([df, new.reindex(columns=df.columns)])


def patch_latest_bars(history_map: dict[str, pd.DataFrame],
                      session: requests.Session) -> tuple[dict[str, pd.DataFrame], date | None, int]:
    """把官方最新一日補進 yfinance 歷史（只補缺的，不改已有的）。

    回傳 (history_map, 官方最新交易日, 補了幾檔)。
    官方資料抓不到時原樣回傳，最新交易日改用歷史資料裡最常見的最後日期。
    """
    if not history_map:
        return history_map, None, 0
    tw_d, tw = fetch_twse_daily(session)
    tp_d, tp = fetch_tpex_daily(session)
    patched = 0
    for code, df in list(history_map.items()):
        if df is None or df.empty:
            continue
        src_d, bar = (tw_d, tw.get(code)) if code in tw else (tp_d, tp.get(code))
        if not bar or src_d is None:
            continue
        if _last_date(df) < src_d:
            try:
                history_map[code] = _append_bar(df, src_d, bar)
                patched += 1
            except Exception:
                continue
    latest = max([d for d in (tw_d, tp_d) if d is not None], default=None)
    if latest is None:
        dates = [_last_date(df) for df in history_map.values() if df is not None and not df.empty]
        latest = max(set(dates), key=dates.count) if dates else None
    return history_map, latest, patched
