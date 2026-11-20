"""儀表板「📉 抄底雷達」區塊（2026-11-20）

取代原本一行的「📉 TAIEX 抄底訊號」，一個區塊看完：
  ① 見底計分卡 X/8（bottom_scorecard.py 每晚寫 data/bottom_scorecard.json）
  ② 跌幅階梯：即時加權指數 vs 空頭彈藥梯（bear_trigger_policy）＋資料包分批價
  ③ 8 項見底條件燈號＋參考的台指／美股 VIX
  ④ 原本的均線抄底燈號（taiex_ma_state）
  ⑤ 展開：規則、回測、歷史大跌對照、融資維持率口徑、事件時間軸
只讀檔、不連網；即時指數由 app.py 傳進來。
"""
from __future__ import annotations

import html
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
CARD_FILE = BASE / "data" / "bottom_scorecard.json"
REF_FILE = BASE / "data" / "bottom_reference.json"
BT_FILE = BASE / "data" / "bottom_backtest_summary.json"

LIT_STYLE = {   # 記號、字色、底色、說明
    "yes": ("●", "#1b7f3b", "#e6f4ea", "完全符合"),
    "partial": ("◐", "#a65f00", "#fff4dc", "初步符合"),
    "no": ("○", "#757575", "#f7f7f7", "未符合"),
    "na": ("·", "#9e9e9e", "#fafafa", "沒資料"),
}


def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _md(iso: str | None) -> str:
    return f"{iso[5:7]}/{iso[8:10]}" if iso and len(iso) >= 10 else "--/--"


def _pct(x, digits=1) -> str:
    return "—" if x is None else f"{x:+.{digits}f}%"


# ---------------------------------------------------------------- ② 跌幅階梯
def _ladder(card: dict, live: dict | None) -> tuple[str, float | None]:
    tx = card.get("taiex") or {}
    peak = tx.get("peak")
    price, price_tag = tx.get("close"), f"收盤 {_md(tx.get('date'))}"
    if live and live.get("last"):
        price, price_tag = live["last"], f"即時（今日 {live.get('change_pct', 0):+.2f}%）"
    if not peak or not price:
        return "", None
    dd = (price / peak - 1) * 100

    bear = card.get("bear") or {}
    levels = bear.get("levels") or []
    marks: dict[float, set] = {}
    for lv in levels:
        marks.setdefault(abs(lv["drop_pct"]), set()).add("彈藥")
    for p in card.get("tranches_pct") or []:
        marks.setdefault(abs(p), set()).add("分批")
    span = max([40.0] + [m + 5 for m in marks])
    pos = min(max(-dd, 0), span) / span * 100

    ticks = []
    for m in sorted(marks):
        left = m / span * 100
        hit = -dd >= m
        c = "#b71c1c" if hit else "#9e9e9e"
        tag = "".join(t for t, k in (("彈", "彈藥"), ("批", "分批")) if k in marks[m])
        ticks.append(
            f"<div style='position:absolute;left:{left:.2f}%;top:0;height:14px;border-left:2px solid {c}'></div>"
            f"<div style='position:absolute;left:{left:.2f}%;top:16px;transform:translateX(-50%);font-size:10px;"
            f"color:{c};white-space:nowrap;text-align:center;line-height:1.2'>-{m:g}%<br>{tag}</div>")
    bar = (
        "<div style='position:relative;height:42px;margin:4px 14px 2px'>"
        "<div style='position:absolute;left:0;right:0;top:3px;height:8px;background:#eceff1;border-radius:4px'></div>"
        f"<div style='position:absolute;left:0;width:{pos:.2f}%;top:3px;height:8px;"
        "background:linear-gradient(90deg,#ffcdd2,#c62828);border-radius:4px'></div>"
        + "".join(ticks) +
        f"<div style='position:absolute;left:{pos:.2f}%;top:-1px;width:16px;height:16px;margin-left:-8px;"
        "background:#1a1a1a;border:2px solid #fff;border-radius:50%;box-shadow:0 0 0 1px #1a1a1a'></div>"
        "</div>")

    nxt = [m for m in sorted(marks) if m > -dd]
    nxt_txt = ""
    if nxt:
        lvl = peak * (1 - nxt[0] / 100)
        nxt_txt = f"下一關 -{nxt[0]:g}% ＝ <b>{lvl:,.0f}</b>（再跌 {(lvl / price - 1) * 100:.1f}%）"
    else:
        nxt_txt = "已跌破所有關卡"

    ammo = ""
    if bear.get("reserve_twd"):
        dep = bear.get("deployed_twd") or 0
        ammo = f"🎒 空頭彈藥 已投 <b>{dep / 1e4:,.0f}</b> / {bear['reserve_twd'] / 1e4:,.0f} 萬"
        reached = max([i for i, lv in enumerate(levels) if -dd >= abs(lv["drop_pct"])], default=-1)
        if reached > (bear.get("deploy_idx") if bear.get("deploy_idx") is not None else -1):
            lv = levels[reached]
            ammo += (f" ｜ <span style='color:#b71c1c'>現價已跌破 {_e(lv['label'])}：雲端用<b>收盤價</b>判斷，"
                     f"收盤後推播「加碼到累積 {lv['cum_deploy_twd'] / 1e4:,.0f} 萬」</span>")

    head = (f"<div style='font-size:12px;color:#333'>📏 <b>跌幅階梯</b>　加權 <b>{price:,.0f}</b>"
            f"<span style='color:#777'>（{_e(price_tag)}）</span>　距收盤高點 {peak:,.0f} "
            f"<b style='color:#b71c1c;font-size:15px'>{dd:+.1f}%</b></div>")
    ref = _read(REF_FILE)
    pk_i = ref.get("package_peak_intraday")
    pkg_txt = ""
    if pk_i and card.get("tranches_pct"):
        pkg_txt = ("（資料包用盤中高點 " + f"{pk_i:,.0f} 算："
                   + "／".join(f"-{p:g}% {pk_i * (1 - p / 100):,.0f}" for p in card["tranches_pct"]) + "）")
    sup = ref.get("support_levels") or []
    sup_txt = ""
    if sup:
        sup_txt = ("<div style='font-size:11px;color:#555;margin-top:2px'>🧱 資料包支撐參考："
                   + "、".join(f"{_e(s['label'])} {s['value']:,.0f}"
                              + ("（已跌破）" if price < s["value"] else f"（距現價 {(s['value'] / price - 1) * 100:+.1f}%）")
                              for s in sup) + "</div>")
    foot = (f"<div style='font-size:10.5px;color:#888;margin-top:2px'>彈＝空頭彈藥梯（收盤價觸發推播）　"
            f"批＝資料包小部位分批價{_e(pkg_txt)}；刻度都用收盤高點算</div>"
            f"<div style='font-size:11.5px;color:#444;margin-top:2px'>{nxt_txt}　{ammo}</div>{sup_txt}")
    return head + bar + foot, dd


# ---------------------------------------------------------------- ③ 指標燈號
def _indicator_cell(i: dict) -> str:
    mark, fg, bg, word = LIT_STYLE.get(i.get("lit", "na"), LIT_STYLE["na"])
    ref = "" if i.get("core") else "<span style='font-size:10px;color:#999'>（參考）</span>"
    return (
        f"<div title='{_e(i.get('rule'))}' style='background:{bg};border-radius:6px;padding:6px 8px;"
        f"border-left:4px solid {fg};min-width:0'>"
        f"<div style='display:flex;justify-content:space-between;gap:6px;align-items:baseline'>"
        f"<span style='font-size:12.5px;font-weight:700;color:#1a1a1a'>"
        f"<span style='color:{fg}'>{mark}</span> {_e(i.get('name'))}{ref}</span>"
        f"<span style='font-size:12.5px;font-weight:700;color:{fg};white-space:nowrap'>{_e(i.get('value'))}</span></div>"
        f"<div style='font-size:10.5px;color:#666;margin-top:2px;line-height:1.35'>"
        f"{word}｜{_md(i.get('as_of'))}｜{_e(i.get('detail'))}</div></div>")


def _spark(hist: list[dict], mx: float, th: float) -> str:
    if not hist:
        return ""
    bars = []
    for h in hist[-12:]:
        s = h.get("score", 0)
        hgt = 3 + s / mx * 25
        if h.get("active") is False:          # 那天還沒跌 10%：灰色（跟回測圖一樣）
            c = "#e0e0e0"
        else:
            c = "#1b7f3b" if s >= th else "#f0a020" if s >= 2 else "#b0bec5"
        bars.append(f"<div title='{_md(h['date'])}：{s:g} 分' style='width:7px;height:{hgt:.0f}px;"
                    f"background:{c};border-radius:2px'></div>")
    return ("<div style='display:flex;align-items:flex-end;gap:2px;height:30px' "
            "title='近 12 個交易日分數'>" + "".join(bars) + "</div>")


def _overlay_ma_state(card: dict, ma: dict | None) -> dict:
    """彈藥進度、最新收盤以 taiex_ma_state 為準（它是空頭彈藥梯的唯一寫入者，比計分卡快照新）。
    11/20 審查：-25% 推播送出後，計分卡要到隔天凌晨才重算，不覆蓋的話畫面會還寫『已投 84 萬』。"""
    if not ma:
        return card
    card = dict(card)
    bear = dict(card.get("bear") or {})
    if ma.get("bear_deploy_idx") is not None:
        bear["deploy_idx"] = ma["bear_deploy_idx"]
    if ma.get("bear_deployed_twd") is not None:
        bear["deployed_twd"] = ma["bear_deployed_twd"]
    card["bear"] = bear
    tx = dict(card.get("taiex") or {})
    if ma.get("close") and ma.get("date") and (not tx.get("date") or ma["date"] > tx["date"]):
        tx["date"], tx["close"] = ma["date"], ma["close"]
        tx["peak"] = max(tx.get("peak") or 0, ma.get("peak_120d") or 0, ma["close"])
        tx["dist_peak_pct"] = (tx["close"] / tx["peak"] - 1) * 100
    card["taiex"] = tx
    return card


# ---------------------------------------------------------------- 主區塊
def render(st, ma_state: dict | None, live: dict | None, has_txf: bool = True) -> bool:
    """畫抄底雷達。沒有計分卡檔時回傳 False（app.py 會退回原本的一行燈號）"""
    card = _read(CARD_FILE)
    if not card.get("indicators"):
        return False
    card = _overlay_ma_state(card, ma_state)
    score, mx, th = card.get("score", 0), card.get("max", 8), card.get("threshold", 4)
    active = card.get("active", True)
    reached = card.get("reached", active and score >= th)
    if not active:                       # 沒跌深：分數只供參考 → 灰色，別跟「達門檻」同色
        chip_c, chip_bg = "#757575", "#eeeeee"
    elif reached:
        chip_c, chip_bg = "#1b7f3b", "#e6f4ea"
    elif score >= 2:
        chip_c, chip_bg = "#a65f00", "#fff4dc"
    else:
        chip_c, chip_bg = "#546e7a", "#eceff1"

    ladder_html, _ = _ladder(card, live)
    core = [i for i in card["indicators"] if i.get("core")]
    ref = [i for i in card["indicators"] if not i.get("core")]
    lit_names = [i["name"] for i in core if i.get("lit") in ("yes", "partial")]

    ma_line = ""
    if ma_state:
        gate = "" if has_txf else "｜<b style='color:#1976d2'>此為進場閘門（雲端無 TXF）</b>"
        ma_line = (
            f"<div style='font-size:11.5px;color:#444;margin-top:8px;padding-top:6px;border-top:1px dashed #ddd'>"
            f"📐 均線燈號 <b style='color:{ma_state.get('color', '#666')}'>{_e(ma_state.get('level'))}</b>"
            f"（{_e(ma_state.get('label'))}）距 20MA <b>{ma_state.get('dist_ma20_pct', 0):+.2f}%</b>｜"
            f"60MA <b>{ma_state.get('dist_ma60_pct', 0):+.2f}%</b>｜200MA <b>{ma_state.get('dist_ma200_pct', 0):+.2f}%</b>"
            f"{gate}<br><i style='color:#666'>{_e(ma_state.get('action'))}</i></div>")

    st.markdown(
        f"<div style='background:#fff;border:1px solid #e0e0e0;border-left:6px solid {chip_c};"
        f"border-radius:8px;padding:10px 14px;margin:8px 0;color:#1a1a1a'>"
        # 標題列
        f"<div style='display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px'>"
        f"<div style='display:flex;align-items:center;gap:10px;flex-wrap:wrap'>"
        f"<span style='font-size:15px;font-weight:800'>📉 抄底雷達</span>"
        f"<span style='background:{chip_bg};color:{chip_c};border:1px solid {chip_c};border-radius:14px;"
        f"padding:2px 10px;font-size:15px;font-weight:800'>🎯 {score:g} / {mx}</span>"
        f"<span style='font-size:12px;color:{chip_c};font-weight:700'>{_e(card.get('verdict'))}</span></div>"
        f"<div style='display:flex;align-items:center;gap:8px'>{_spark(card.get('score_history', []), mx, th)}"
        f"<span style='font-size:10.5px;color:#888'>指標資料 {_md(card.get('as_of'))}<br>每晚 22:20 後更新</span></div>"
        f"</div>"
        # 跌幅階梯
        f"<div style='margin-top:8px'>{ladder_html}</div>"
        # 8 項燈號
        f"<div style='font-size:12px;color:#333;margin:8px 0 4px'>🔦 <b>8 項見底條件</b>"
        f"<span style='color:#777'>（● 1 分 ◐ 0.5 分；每項規則在下方「點開」）"
        f"{'　亮：' + '、'.join(lit_names) if lit_names else ''}</span></div>"
        f"<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:6px'>"
        + "".join(_indicator_cell(i) for i in core) +
        "</div>"
        f"<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:6px;margin-top:6px'>"
        + "".join(_indicator_cell(i) for i in ref) +
        "</div>"
        f"{ma_line}"
        f"</div>",
        unsafe_allow_html=True,
    )
    with st.expander("📚 抄底雷達：規則、歷史回測、四次大跌對照（點開）", expanded=False):
        _details(st, card)
    return True


# ---------------------------------------------------------------- ⑤ 展開內容
def _details(st, card: dict) -> None:
    import pandas as pd

    ref = _read(REF_FILE)
    bt = _read(BT_FILE)
    th = card.get("threshold", 4)

    st.markdown(f"**怎麼判讀**：8 項核心條件，完全符合 1 分、初步符合 0.5 分。資料包規則：**累計 ≥ {th} 分才放大部位**，"
                f"之前只用小部位、照跌幅 {'／'.join(f'-{p}%' for p in card.get('tranches_pct', []))} 分批。"
                "台指 VIX、美股 VIX 只當參考（本波是台股局部崩跌，美股 VIX 不一定同步）。")
    st.dataframe(pd.DataFrame([{
        "條件": i["name"] + ("" if i.get("core") else "（參考）"),
        "現在": i.get("value"),
        "資料日": i.get("as_of"),
        "燈號": LIT_STYLE.get(i.get("lit", "na"))[0] + " " + LIT_STYLE.get(i.get("lit", "na"))[3],
        "見底規則": i.get("rule"),
        "說明": i.get("detail"),
    } for i in card.get("indicators", [])]), hide_index=True, use_container_width=True)

    # 回測
    st.markdown("##### 🧪 回測：同一套規則放回過去三次大跌（逐日只用當天以前的資料）")
    evs = bt.get("events") or []
    bth = bt.get("threshold", th)       # 回測當時的門檻（改過 policy 但還沒重跑 --replay 時會不同）
    if bth != th:
        st.warning(f"回測是用 {bth:g} 分門檻跑的，現在規則是 {th:g} 分：請在本機重跑 "
                   "`py -3 bottom_scorecard.py --replay` 更新回測。")
    th = bth
    if not evs:
        st.caption("回測資料還沒產生（`py -3 bottom_scorecard.py --backfill ...` 後再 `--replay`）。")
    else:
        def _hit(x):
            if not x:
                return "沒出現"
            s = f"{_md(x['date'])} 收 {x['close']:,.0f}"
            if x.get("further_drop_pct") is not None:
                s += f"，之後再跌 {x['further_drop_pct']:.1f}%"
            if x.get("fwd20_pct") is not None:
                s += f"，20 日 {x['fwd20_pct']:+.1f}%"
            if x.get("fwd60_pct") is not None:
                s += f"，60 日 {x['fwd60_pct']:+.1f}%"
            return s
        rows = []
        for ev in evs:
            rows.append({
                "事件": ev["label"],
                "大底（收盤）": (f"{_md(ev['bottom_date'])} {ev['bottom_close']:,.0f}（{ev['bottom_dd_pct']:+.1f}%）"
                              if ev.get("bottom_date") else "進行中"),
                "大底當天分數": (f"{ev['score_on_bottom']:g}" if ev.get("score_on_bottom") is not None else "—"),
                "最高分": (f"{ev['max_score']:g}（{_md(ev.get('max_score_date'))}）"
                         if ev.get("max_score") is not None else "—（沒跌到門檻）"),
                "第一次 ≥2 分": _hit(ev.get("first_2")),
                f"第一次 ≥{th:g} 分": _hit(ev.get(f"first_{th:g}")),
                "純價格 -20%": _hit(ev.get("price_minus20")),
                "純價格 -25%": _hit(ev.get("price_minus25")),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        if bt.get("conclusion"):
            st.markdown(bt["conclusion"])
        opts = [ev for ev in evs if ev.get("daily")]
        if opts:
            try:
                import plotly.graph_objects as go
                pick = st.selectbox("看某一次大跌的每日分數", [ev["label"] for ev in opts],
                                    index=len(opts) - 1, key="bottom_bt_pick")
                ev = next(e for e in opts if e["label"] == pick)
                dfx = pd.DataFrame(ev["daily"])
                colors = ["#e0e0e0" if not a else "#1b7f3b" if s >= th else "#f0a020" if s >= 2 else "#90a4ae"
                          for s, a in zip(dfx["score"], dfx["active"])]
                fig = go.Figure()
                fig.add_trace(go.Bar(x=dfx["date"], y=dfx["score"], name="計分（左軸；灰＝還沒跌 10%）",
                                     marker_color=colors))
                fig.add_trace(go.Scatter(x=dfx["date"], y=dfx["close"], name="加權收盤（右軸）", yaxis="y2",
                                         line=dict(color="#263238", width=1.8)))
                if ev.get("bottom_date"):
                    fig.add_vline(x=ev["bottom_date"], line_dash="dot", line_color="#c62828")
                fig.add_hline(y=th, line_dash="dash", line_color="#1b7f3b")
                fig.update_layout(height=320, margin=dict(l=40, r=55, t=40, b=30), bargap=0.15,
                                  yaxis=dict(title="分數", range=[0, card.get("max", 8)]),
                                  yaxis2=dict(overlaying="y", side="right", showgrid=False, tickformat=","),
                                  legend=dict(orientation="h", y=1.15, x=0))
                st.plotly_chart(fig, use_container_width=True)
                st.caption(f"紅色虛線＝收盤大底；綠色虛線＝{th} 分門檻。")
            except Exception as e:  # noqa: BLE001
                st.caption(f"走勢圖畫不出來：{e}")
        st.caption(f"回測產生於 {bt.get('generated_at', '')[:16]}；逐日明細在本機 data_export/bottom_backtest_daily_scores.csv")

    # 歷史大跌
    if ref.get("drawdowns"):
        st.markdown("##### 📉 四次大跌對照（盤中高低點；已用加權日 K 核對）")
        st.dataframe(pd.DataFrame([{
            "波段": d["label"], "高點": f"{d['high_date']} {d['high']:,.0f}",
            "低點": f"{d['low_date']} {d['low']:,.0f}", "盤中跌幅": f"{d['dd_pct']:.2f}%",
            "收盤跌幅": f"{d['close_dd_pct']:.2f}%", "備註": d.get("note", ""),
        } for d in ref["drawdowns"]]), hide_index=True, use_container_width=True)
    if ref.get("margin_own_measure"):
        st.markdown("##### 🧮 融資維持率：口徑不同不能混著比")
        c1, c2 = st.columns(2)
        with c1:
            st.caption("自家算法（計分卡用這個）")
            st.dataframe(pd.DataFrame([{
                "日期": m["date"], "維持率": f"{m['value']:.1f}%",
                "是否大底": "✅ 大底" if m.get("is_bottom") else m.get("note", ""),
            } for m in ref["margin_own_measure"]]), hide_index=True, use_container_width=True)
        with c2:
            st.caption("新聞／其他口徑（只供參考）")
            st.dataframe(pd.DataFrame([{
                "日期": m["date"], "維持率": f"{m['value']:g}%", "口徑": m["measure"], "備註": m.get("note", ""),
            } for m in ref.get("margin_other_measures", [])]), hide_index=True, use_container_width=True)
        st.caption(ref.get("_margin_note", ""))
    if ref.get("vix_refs"):
        st.markdown("##### 😱 恐慌指數歷史關鍵值")
        st.dataframe(pd.DataFrame([{"日期": v["date"], "指數": v["index"], "數值": v["value"], "備註": v["note"]}
                                   for v in ref["vix_refs"]]), hide_index=True, use_container_width=True)
    if ref.get("events_2026"):
        st.markdown("##### 🗓️ 2026 本波時間軸")
        st.markdown("\n".join(f"- **{_e(x['date'])}**　{_e(x['event'])}" for x in ref["events_2026"]))
    if ref.get("lessons"):
        st.markdown("##### 💡 從過去學到的")
        st.markdown("\n".join(f"- {_e(x)}" for x in ref["lessons"]))
    st.caption("規則與門檻：data/bottom_policy.json（記事本可改）｜" + ref.get("sources_note", "")
               + "｜指標只是歷史對照，不保證見底。")
