# ui/theme.py
"""차트 색/레이아웃 토큰. 값은 dataviz 스킬의 검증된 기준 팔레트를 그대로 쓴다(임의 변경 금지).

- 손익 극성은 diverging(파랑↔빨강 + 중립 회색). 기본은 한국 관례(상승=빨강, 하락=파랑)이고 사이드바에서 반전할 수 있다.
  부호(+/−)는 항상 숫자에 함께 표기해 색만으로 의미를 전달하지 않는다.
- 종목 구분(categorical)은 고정 순서 8슬롯. 종목→색은 세션 동안 고정(필터로 종목 수가 바뀌어도 색이 바뀌지 않는다).
  8개를 넘으면 "기타" 회색으로 접는다. 새 색을 생성하지 않는다.
"""
from typing import Dict, List

import plotly.graph_objects as go
import streamlit as st

LIGHT = {"grid": "#e1e0d9", "axis": "#c3c2b7", "muted": "#898781", "text2": "#52514e", "neutral": "#f0efec", "surface": "#fcfcfb"}
DARK = {"grid": "#2c2c2a", "axis": "#383835", "muted": "#898781", "text2": "#c3c2b7", "neutral": "#383835", "surface": "#1a1a19"}

BLUE = {"light": "#2a78d6", "dark": "#3987e5"}
RED = {"light": "#e34948", "dark": "#e66767"}
CATEGORICAL = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
}
OTHER_GRAY = "#898781"
# 1색상 2톤 (전→후 비교: dumbbell). 라이트/다크 모두 표면 대비 2:1 이상인 단계.
BEFORE_AFTER = {"light": ("#86b6ef", "#184f95"), "dark": ("#6da7ec", "#3987e5")}


def mode() -> str:
    try:
        return "dark" if st.context.theme.type == "dark" else "light"
    except Exception:  # noqa: BLE001 — 구버전/헤드리스에서는 라이트로
        return "light"


def tokens() -> Dict[str, str]:
    return DARK if mode() == "dark" else LIGHT


def gain_loss() -> Dict[str, str]:
    """{"gain": hex, "loss": hex, "neutral": hex}. 사이드바 토글(`korean_colors`)에 따라 극성이 바뀐다."""
    m = mode()
    korean = st.session_state.get("korean_colors", True)
    gain, loss = (RED[m], BLUE[m]) if korean else (BLUE[m], RED[m])
    return {"gain": gain, "loss": loss, "neutral": tokens()["neutral"]}


def diverging_colorscale() -> List[List]:
    c = gain_loss()
    return [[0.0, c["loss"]], [0.5, c["neutral"]], [1.0, c["gain"]]]


def accent() -> str:
    return BLUE[mode()]


def symbol_color(symbol: str) -> str:
    """종목→카테고리 색. 처음 요청된 순서대로 슬롯을 배정하고 세션 동안 고정한다."""
    assigned: Dict[str, int] = st.session_state.setdefault("_symbol_slots", {})
    if symbol not in assigned:
        assigned[symbol] = len(assigned)
    slot = assigned[symbol]
    palette = CATEGORICAL[mode()]
    return palette[slot] if slot < len(palette) else OTHER_GRAY


def style(fig: go.Figure, height: int = 340, hovermode: str = "closest", legend: bool = True) -> go.Figure:
    t = tokens()
    fig.update_layout(
        height=height, margin=dict(t=28, b=8, l=8, r=8), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="system-ui, -apple-system, 'Segoe UI', sans-serif", size=12, color=t["text2"]),
        hovermode=hovermode, showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="left", x=0, font=dict(size=12)),
        hoverlabel=dict(font_size=12),
    )
    fig.update_xaxes(showgrid=False, linecolor=t["axis"], tickfont=dict(color=t["muted"]), zeroline=False, automargin=True)
    fig.update_yaxes(gridcolor=t["grid"], gridwidth=1, zeroline=True, zerolinecolor=t["axis"], zerolinewidth=1,
                     tickfont=dict(color=t["muted"]), linecolor="rgba(0,0,0,0)", automargin=True)
    return fig
