import streamlit as st
import yfinance as yf
import pandas as pd
from streamlit_calendar import calendar
from datetime import datetime
import json 
import os   
import plotly.graph_objects as go # (NEW) Plotly 라이브러리 임포트

# --- (NEW) 파일 저장/로드 함수 ---
PORTFOLIO_FILE = "my_portfolio.json" 

def load_tickers():
    """로컬 JSON 파일에서 종목 리스트를 불러옵니다."""
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return [] 
    return [] 

def save_tickers(tickers_list):
    """종목 리스트를 로컬 JSON 파일에 저장합니다."""
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(tickers_list, f, indent=4)

# --- 페이지 기본 설정 ---
st.set_page_config(layout="wide")
st.title("My 배당금 & 뉴스 대시보드 (데이터 저장됨)")

# --- 데이터 로딩 함수 (캐시 사용) ---
@st.cache_data
def get_stock_data(ticker):
    """yfinance로부터 주식 정보, 과거 데이터, 뉴스, 배당 정보를 가져옵니다."""
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        # (MODIFIED) 캔들 차트를 위해 1년(1y) 대신 6개월(6mo)로 변경 (선택 사항)
        # 1년은 캔들이 너무 촘촘할 수 있습니다.
        history = ticker_obj.history(period="6mo") 
        news = ticker_obj.news
        dividends = ticker_obj.dividends
        return info, history, news, dividends
    except Exception as e:
        st.error(f"{ticker} 정보 로드 실패: {e}")
        return None, None, None, None

# --- 세션 상태(Session State) 초기화 ---
if 'tickers' not in st.session_state:
    st.session_state.tickers = load_tickers() 
if 'calendar_events' not in st.session_state:
    st.session_state.calendar_events = []
if 'selected_ticker' not in st.session_state:
    st.session_state.selected_ticker = None 

# --- 1. 사이드바: 종목 관리 ---
st.sidebar.header("종목 관리")
new_ticker = st.sidebar.text_input("종목 티커 입력 (예: AAPL, MSFT)", key="ticker_input")

if st.sidebar.button("종목 추가"):
    if new_ticker.upper() not in st.session_state.tickers and new_ticker:
        st.session_state.tickers.append(new_ticker.upper())
        st.session_state.selected_ticker = new_ticker.upper() 
        save_tickers(st.session_state.tickers) 
        st.sidebar.success(f"'{new_ticker.upper()}'가 추가 및 저장되었습니다.")
        st.rerun() 
    elif not new_ticker:
        st.sidebar.warning("티커를 입력하세요.")
    else:
        st.sidebar.info(f"'{new_ticker.upper()}'는 이미 등록된 종목입니다.")

if st.sidebar.button("목록 초기화"):
    st.session_state.tickers = []
    st.session_state.calendar_events = []
    st.session_state.selected_ticker = None
    save_tickers(st.session_state.tickers) 
    st.sidebar.info("모든 종목이 삭제되었습니다.")
    st.rerun() 

if st.session_state.tickers:
    st.sidebar.subheader("뉴스/그래프 보기 선택:")
    
    if st.sidebar.button("📰 전체 뉴스 보기", key="btn_all", use_container_width=True):
        st.session_state.selected_ticker = None
        st.rerun()

    for ticker in st.session_state.tickers:
        if st.sidebar.button(f"📊 {ticker}", key=f"btn_{ticker}", use_container_width=True):
            st.session_state.selected_ticker = ticker
            st.rerun() 
else:
    st.sidebar.info("사이드바에서 종목을 추가하세요.")
    st.stop() 

# --- 캘린더용 이벤트 데이터 생성 ---
calendar_events = []
all_dividends_df = [] 
for ticker in st.session_state.tickers:
    info, _, _, dividends = get_stock_data(ticker)
    if info is None: continue
    ex_div_ts = info.get('exDividendDate') 
    if ex_div_ts:
        ex_div_date = datetime.fromtimestamp(ex_div_ts).strftime('%Y-%m-%d')
        calendar_events.append({
            "title": f"[{ticker}] 배당락일", "start": ex_div_date, "allDay": True, "color": "#FF6B6B"
        })
    if not dividends.empty:
        for date, amount in dividends.items():
            pay_date = date.strftime('%Y-%m-%d')
            calendar_events.append({
                "title": f"[{ticker}] ${amount:.2f} 지급", "start": pay_date, "allDay": True, "color": "#4ECDC4"
            })
        temp_df = dividends.reset_index(); temp_df['Ticker'] = ticker
        temp_df.columns = ['지급일', '배당금', 'Ticker']; all_dividends_df.append(temp_df)

# --- 2. 메인 페이지: 배당금 캘린더 ---
st.header("📅 배당금 캘린더")
st.info("과거 배당 지급일(민트색)과 예정된 배당락일(빨간색)을 표시합니다.")
if calendar_events:
    calendar(events=calendar_events, options={
        "headerToolbar": {"left": "prev,next today", "center": "title", "right": "dayGridMonth,timeGridWeek,listWeek"},
        "initialView": "dayGridMonth", "editable": False,
    }, key="main_calendar")
else:
    st.write("표시할 배당 정보가 없습니다.")
if all_dividends_df:
    with st.expander("모든 배당 내역 보기 (표)"):
        full_div_df = pd.concat(all_dividends_df).sort_values('지급일', ascending=False)
        st.dataframe(full_div_df[['Ticker', '지급일', '배당금']], use_container_width=True)

st.divider()

# --- 3. 메인 페이지: 최신 뉴스 피드 ---
st.header("📰 최신 뉴스 피드")

def display_news(item):
    """yfinance news item 딕셔너리를 받아 마크다운으로 이쁘게 표시"""
    try:
        content = item.get('content', {})
        if not content:
            st.warning("뉴스 데이터 형식이 예상과 다릅니다. ('content' 키 없음)")
            st.json(item)
            return

        title = content.get('title', '제목 없음')
        provider_data = content.get('provider', {})
        publisher = provider_data.get('displayName', '출처 없음')
        
        canonical_url_data = content.get('canonicalUrl', {})
        link = canonical_url_data.get('url', '#')

        if not link or link == '#':
            click_through_url_data = content.get('clickThroughUrl', {})
            link = click_through_url_data.get('url', '#')

        st.markdown(f"- **[{title}]({link})** *({publisher})*")
        
    except Exception as e:
        st.warning(f"뉴스 항목을 파싱하는 데 실패했습니다: {e}")
        st.json(item)

if st.session_state.selected_ticker:
    # --- 3a. 선택된 종목의 뉴스 ---
    ticker = st.session_state.selected_ticker
    st.subheader(f"'{ticker}' 관련 뉴스")
    info, _, news, _ = get_stock_data(ticker)
    if info and news:
        for item in news[:10]:
            display_news(item)
    elif info:
        st.write("제공된 뉴스가 없습니다.")
    else:
        st.error(f"'{ticker}' 뉴스를 불러오는 데 실패했습니다.")
else:
    # --- 3b. 전체 종목 뉴스 (Default) ---
    st.subheader("전체 등록 종목 관련 뉴스")
    st.info("사이드바에서 '전체 뉴스 보기'가 선택되었습니다. (기본값)")
    any_news_found = False
    for ticker in st.session_state.tickers:
        info, _, news, _ = get_stock_data(ticker)
        if info and news:
            any_news_found = True
            with st.expander(f"**{ticker}** 뉴스 보기 ({len(news)}개)"):
                for item in news[:5]:
                    display_news(item)
        elif info:
            pass 
        else:
            st.error(f"'{ticker}' 뉴스를 불러오는 데 실패했습니다.")
    if not any_news_found and st.session_state.tickers:
        st.write("모든 등록된 종목에서 제공된 뉴스가 없습니다.")

st.divider()

# --- 4. 메인 페이지: 종목별 주가 그래프 (MODIFIED SECTION) ---
st.header("📊 종목별 주가 그래프")
if st.session_state.selected_ticker:
    ticker = st.session_state.selected_ticker
    info, history, _, _ = get_stock_data(ticker) 
    
    if info:
        st.subheader(f"{ticker} - {info.get('shortName', 'N/A')}")
        
        # (MODIFIED) st.line_chart 대신 Plotly 캔들 차트 사용
        if not history.empty:
            # Plotly Figure 객체 생성
            fig = go.Figure(data=[go.Candlestick(
                x=history.index,
                open=history['Open'],  # 시가
                high=history['High'], # 고가
                low=history['Low'],   # 저가
                close=history['Close'], # 종가
                
                # (NEW) 사용자가 요청한 색상 설정
                increasing_line_color='red', # 상승 (오른날)
                decreasing_line_color='blue' # 하락 (내린날)
            )])
            
            # (NEW) 차트 레이아웃 설정
            fig.update_layout(
                title=f"{ticker} Candlestick Chart",
                yaxis_title="Stock Price (USD)",
                xaxis_rangeslider_visible=False # (NEW) 하단 레인지 슬라이더 숨기기
            )
            
            # (NEW) Streamlit에 Plotly 차트 표시
            st.plotly_chart(fig, use_container_width=True)
            
        else:
            st.write("주가 데이터를 불러올 수 없습니다.")
    else:
        st.error(f"'{ticker}' 종목의 상세 정보를 불러오는 데 실패했습니다.")
else:
    st.info("사이드바에서 특정 종목(예: 📊 AAPL)을 클릭하면 여기에 주가 그래프가 표시됩니다.")