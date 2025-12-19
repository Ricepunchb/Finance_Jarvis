import streamlit as st
import yfinance as yf
import pandas as pd
from streamlit_calendar import calendar
from datetime import datetime, timedelta, timezone # (FIXED) timezone, timedelta 추가
import json 
import os   
import plotly.graph_objects as go
import ollama 

# --- 파일 저장/로드 함수 ---
PORTFOLIO_FILE = "my_portfolio.json" 
MODEL = 'gemma3:12b-it-qat'     # 사용할 모델
KST = timezone(timedelta(hours=9))

def load_tickers():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return [] 
    return [] 


def save_tickers(tickers_list):
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(tickers_list, f, indent=4)


# --- AI 감성 분석 함수 ---
@st.cache_data
def analyze_news_sentiment(title, summary):
    global MODEL
    prompt = f"""
    Analyze the following stock news and provide a response in JSON format.
    The response must include:
    1. 'sentiment': 'Bullish', 'Bearish', or 'Neutral'
    2. 'score': a float between -1.0 (very bad) and 1.0 (very good)
    3. 'one_liner': a concise 1-line summary in Korean.

    News Title: {title}
    News Summary: {summary}
    """
    try:
        # 모델명이 정확한지 확인 (예: gemma2, llama3.1 등)
        response = ollama.chat(model=MODEL, messages=[
            {'role': 'system', 'content': 'You are a financial analyst expert. Respond only in valid JSON.'},
            {'role': 'user', 'content': prompt},
        ])
        result_text = response['message']['content']
        start_idx = result_text.find('{')
        end_idx = result_text.rfind('}') + 1
        return json.loads(result_text[start_idx:end_idx])
    except Exception as e:
        return {"sentiment": "N/A", "score": 0, "one_liner": "AI 분석 실패"}


# --- 데이터 로딩 함수 (캐시 사용) ---
@st.cache_data
def get_stock_data(ticker):
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        history = ticker_obj.history(period="6mo") 
        news = ticker_obj.news
        dividends = ticker_obj.dividends
        return info, history, news, dividends
    except Exception as e:
        st.error(f"{ticker} 정보 로드 실패: {e}")
        return None, None, None, None


# --- 세션 상태 초기화 ---
if 'tickers' not in st.session_state:
    st.session_state.tickers = load_tickers() 
if 'selected_ticker' not in st.session_state:
    st.session_state.selected_ticker = None


# --- 1. 사이드바: 종목 관리 ---
st.sidebar.header("종목 관리")
new_ticker = st.sidebar.text_input("종목 티커 입력", key="ticker_input")

if st.sidebar.button("종목 추가"):
    if new_ticker.upper() not in st.session_state.tickers and new_ticker:
        st.session_state.tickers.append(new_ticker.upper())
        st.session_state.selected_ticker = new_ticker.upper() 
        save_tickers(st.session_state.tickers) 
        st.rerun() 

if st.sidebar.button("목록 초기화"):
    st.session_state.tickers = []
    st.session_state.selected_ticker = None
    save_tickers(st.session_state.tickers) 
    st.rerun() 

if st.session_state.tickers:
    st.sidebar.subheader("뉴스/그래프 보기 선택")
    if st.sidebar.button("📰 전체 뉴스 보기", use_container_width=True):
        st.session_state.selected_ticker = None
        st.rerun()
    for ticker in st.session_state.tickers:
        if st.sidebar.button(f"📊 {ticker}", key=f"btn_{ticker}", use_container_width=True):
            st.session_state.selected_ticker = ticker
            st.rerun() 
else:
    st.sidebar.info("종목을 추가하세요.")
    st.stop()


# --- 2. 메인 페이지: 배당금 캘린더 ---
st.header("📅 배당금 캘린더")
st.info("과거 배당 지급일(민트색)과 예정된 배당락일(빨간색)을 표시합니다.")
calendar_events = []
all_dividends_df = [] 
for ticker in st.session_state.tickers:
    info, _, _, dividends = get_stock_data(ticker)
    if info is None: continue
    ex_div_ts = info.get('exDividendDate') 
    if ex_div_ts:
        ex_div_date = datetime.fromtimestamp(ex_div_ts, tz=timezone.utc).strftime('%Y-%m-%d')
        calendar_events.append({"title": f"[{ticker}] 배당락일", "start": ex_div_date, "allDay": True, "color": "#FF6B6B"})
    if not dividends.empty:
        for date, amount in dividends.items():
            pay_date = date.strftime('%Y-%m-%d')
            calendar_events.append({"title": f"[{ticker}] ${amount:.2f} 지급", "start": pay_date, "allDay": True, "color": "#4ECDC4"})
        temp_df = dividends.reset_index(); temp_df['Ticker'] = ticker
        temp_df.columns = ['지급일', '배당금', 'Ticker']; all_dividends_df.append(temp_df)

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


# --- 3. 메인 페이지: AI 뉴스 분석 피드 ---
st.header("🤖 AI 기반 뉴스 분석")


def display_ai_news(item):
    """뉴스 항목을 AI 분석 결과(감성 점수 포함)와 함께 표시"""
    try:
        content = item.get('content', {})
        
        # 1. 뉴스 기본 정보 추출
        title = content.get('title') or item.get('title') or '제목 없음'
        summary = content.get('summary') or item.get('summary') or ''
        provider_data = content.get('provider', {})
        publisher = provider_data.get('displayName') or item.get('publisher') or '출처 없음'
        link = content.get('canonicalUrl', {}).get('url') or item.get('link') or '#'
        
        # 2. 날짜 추출 및 5일 필터링
        pub_date_str = content.get('pubDate') or item.get('pubDate')
        if not pub_date_str: return False

        pub_date = pd.to_datetime(pub_date_str).astimezone(KST)

        now = datetime.now(KST)
        
        if now - pub_date > timedelta(days=3):
            return False

        # 3. AI 분석 실행
        with st.spinner(f'"{title[:15]}..." 분석 중'):
            ai_analysis = analyze_news_sentiment(title, summary)
        
        # 4. 점수 및 감성 데이터 추출
        sentiment = ai_analysis.get('sentiment', 'N/A')
        score = ai_analysis.get('score', 0.0) # AI가 생성한 -1.0 ~ 1.0 사이의 점수
        
        # 5. 시각화 설정 (점수에 따른 색상 강도 조절은 생략하고 기본 색상 유지)
        color = "green" if sentiment == "Bullish" else "red" if sentiment == "Bearish" else "gray"
        
        # 점수를 소수점 둘째 자리까지 표시하고 부호를 강제함 (+0.85, -0.40 등)
        badge = f":{color}[{sentiment} ({score:+.2f})]"

        # 6. 화면 출력
        display_date = pub_date.strftime('%Y-%m-%d %H:%M')
        st.markdown(f"### {badge} {title}")
        st.caption(f"**출처:** {publisher} | **날짜:** {display_date} | [뉴스 읽기]({link})")
        
        # 점수가 0.5 이상이거나 -0.5 이하인 경우 강조 표시 (선택 사항)
        if score >= 0.5:
            st.success(f"**AI 요약:** {ai_analysis.get('one_liner', '요약 불가')}")
        elif score <= -0.5:
            st.error(f"**AI 요약:** {ai_analysis.get('one_liner', '요약 불가')}")
        else:
            st.info(f"**AI 요약:** {ai_analysis.get('one_liner', '요약 불가')}")
            
        st.divider()
        return True
        
    except Exception as e:
        return False


# (NEW) 실제로 뉴스를 화면에 출력하는 실행 루프
if st.session_state.selected_ticker:
    ticker = st.session_state.selected_ticker
    st.subheader(f"'{ticker}' 최신 AI 분석")
    _, _, news, _ = get_stock_data(ticker)
    count = 0
    if news:
        for item in news:
            if display_ai_news(item): count += 1
            if count >= 5: break # 최대 5개까지만
    if count == 0: st.write("최근의 뉴스가 없습니다.")
else:
    st.subheader("전체 종목 최신 뉴스 (종목당 1개)")
    for ticker in st.session_state.tickers:
        with st.expander(f"**{ticker}** 분석 결과"):
            _, _, news, _ = get_stock_data(ticker)
            if news:
                found = False
                for item in news:
                    if display_ai_news(item):
                        found = True
                        break
                if not found: st.write("최근의 뉴스가 없습니다.")


# --- 4. 메인 페이지: 주가 차트 ---
st.divider()
st.header("📊 주가 차트")
if st.session_state.selected_ticker:
    ticker = st.session_state.selected_ticker
    info, history, _, _ = get_stock_data(ticker) 
    if info and not history.empty:
        st.subheader(f"{ticker} - {info.get('shortName', 'N/A')}")
        fig = go.Figure(data=[go.Candlestick(
            x=history.index, open=history['Open'], high=history['High'],
            low=history['Low'], close=history['Close'],
            increasing_line_color='red', decreasing_line_color='blue'
        )])
        fig.update_layout(xaxis_rangeslider_visible=False, height=500)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.error(f"'{ticker}' 데이터를 불러올 수 없습니다.")
else:
    st.info("사이드바에서 종목을 선택하면 차트가 표시됩니다.")