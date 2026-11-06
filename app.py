"""Finance Jarvis 진입점 — 사이드바 메뉴 구성만 한다. 실행: uv run streamlit run app.py

Streamlit은 진입 파일명("app")을 첫 페이지 메뉴 이름으로 쓰기 때문에, st.navigation으로 이름을 직접 지정한다.
(이 방식에서는 pages/ 폴더 자동 탐색이 꺼지므로 페이지를 아래에 명시한다.)
"""
import streamlit as st
from core.config import settings

is_master = (st.config.get_option("server.port") == 8500)
is_meritz = getattr(settings, "BROKER", "kis").lower() == "meritz"

auto_trading_title = "메리츠 해외 자동매매" if is_meritz else "KIS 자동매매"
auto_trading_icon = "🇺🇸" if is_meritz else "🤖"

pages = [
    st.Page("pages/0_계좌_마스터.py", title="계좌 마스터 허브", icon="🏢", default=is_master),
    st.Page("dashboard.py", title="매매 복기·분석", icon="📈", default=not is_master),
    st.Page("pages/1_KIS_자동매매.py", title=auto_trading_title, icon=auto_trading_icon),
    st.Page("pages/2_인사이트.py", title="AI 인사이트", icon="💡"),
    st.Page("pages/3_미국장_모니터링.py", title="미국장 시그널 룸", icon="📡"),
]
st.navigation(pages).run()
