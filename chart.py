"""K線圖（蠟燭 + 均線 + 成交量 + MACD + KD），plotly 互動圖。"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from analysis import kd_taiwan, macd


def make_chart(hist: pd.DataFrame, name: str = "", days: int = 60):
    """回傳 plotly Figure：4 個子圖（蠟燭+均線、量、MACD、KD）。"""
    h = hist.dropna(subset=["Close"]).tail(days).copy()
    if h.empty:
        return None

    # 計算指標
    h["MA5"] = hist["Close"].rolling(5).mean().reindex(h.index)
    h["MA20"] = hist["Close"].rolling(20).mean().reindex(h.index)
    h["MA60"] = hist["Close"].rolling(60).mean().reindex(h.index)

    dif, dem, osc = macd(hist["Close"])
    dif, dem, osc = dif.reindex(h.index), dem.reindex(h.index), osc.reindex(h.index)
    k, d = kd_taiwan(hist)
    k, d = k.reindex(h.index), d.reindex(h.index)

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[0.50, 0.15, 0.20, 0.15],
        vertical_spacing=0.03,
        subplot_titles=("K線 + 均線", "成交量", "MACD", "KD"),
    )

    # ── 1. 蠟燭 + 均線（台股慣例：紅漲綠跌） ──
    fig.add_trace(
        go.Candlestick(
            x=h.index, open=h["Open"], high=h["High"], low=h["Low"], close=h["Close"],
            increasing_line_color="#d62728", decreasing_line_color="#2ca02c",
            increasing_fillcolor="#d62728", decreasing_fillcolor="#2ca02c",
            name="K線", showlegend=False,
        ),
        row=1, col=1,
    )
    for ma_col, color in [("MA5", "#ff7f0e"), ("MA20", "#1f77b4"), ("MA60", "#9467bd")]:
        fig.add_trace(
            go.Scatter(x=h.index, y=h[ma_col], mode="lines",
                       line=dict(width=1.2, color=color), name=ma_col),
            row=1, col=1,
        )

    # ── 2. 成交量（依漲跌上色） ──
    colors = ["#d62728" if c >= o else "#2ca02c"
              for c, o in zip(h["Close"], h["Open"])]
    fig.add_trace(
        go.Bar(x=h.index, y=h["Volume"], marker_color=colors,
               name="成交量", showlegend=False, opacity=0.7),
        row=2, col=1,
    )

    # ── 3. MACD ──
    osc_colors = ["#d62728" if v >= 0 else "#2ca02c" for v in osc]
    fig.add_trace(
        go.Bar(x=h.index, y=osc, marker_color=osc_colors, name="OSC", showlegend=False),
        row=3, col=1,
    )
    fig.add_trace(
        go.Scatter(x=h.index, y=dif, mode="lines", line=dict(color="#d62728", width=1.2), name="DIF"),
        row=3, col=1,
    )
    fig.add_trace(
        go.Scatter(x=h.index, y=dem, mode="lines", line=dict(color="#1f77b4", width=1.2), name="MACD"),
        row=3, col=1,
    )

    # ── 4. KD ──
    fig.add_trace(
        go.Scatter(x=h.index, y=k, mode="lines", line=dict(color="#d62728", width=1.2), name="K"),
        row=4, col=1,
    )
    fig.add_trace(
        go.Scatter(x=h.index, y=d, mode="lines", line=dict(color="#1f77b4", width=1.2), name="D"),
        row=4, col=1,
    )
    # 80 / 20 參考線
    fig.add_hline(y=80, line_dash="dot", line_color="rgba(214,39,40,0.4)", row=4, col=1)
    fig.add_hline(y=20, line_dash="dot", line_color="rgba(44,160,44,0.4)", row=4, col=1)

    fig.update_layout(
        title=name,
        height=720,
        showlegend=True,
        legend=dict(orientation="h", y=1.02, yanchor="bottom"),
        xaxis_rangeslider_visible=False,
        margin=dict(l=40, r=40, t=60, b=40),
        hovermode="x unified",
    )
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])  # 跳過週末
    return fig
