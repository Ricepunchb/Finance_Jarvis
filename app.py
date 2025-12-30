import streamlit as st
import yfinance as yf
import pandas as pd
from streamlit_calendar import calendar
from datetime import datetime, timedelta, timezone
import json 
import os   
import plotly.graph_objects as go
import ollama 

# --- 0. 기본 설정 및 전역 변수 ---
st.set_page_config(layout="wide", page_title="Finance Jarvis Pro")

# 사용 모델 설정 (터미널에서 ollama list로 확인된 모델명 사용)
MODEL = "gemma3:12b-it-qat" 

KST = timezone(timedelta(hours=9)) # 한국 표준시
PORTFOLIO_FILE = "my_portfolio.json" 

# --- 1. 유틸리티 함수 ---
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

@st.cache_data
def get_stock_data(ticker):
    try:
        ticker_obj = yf.Ticker(ticker)
        info = ticker_obj.info
        # 지표 계산을 위해 데이터 기간을 넉넉히 1년으로 설정
        history = ticker_obj.history(period="1y") 
        news = ticker_obj.news
        dividends = ticker_obj.dividends
        return info, history, news, dividends
    except Exception as e:
        print(f"[{ticker}] 데이터 로드 실패: {e}")
        return None, None, None, None

# --- 2. AI 분석 엔진 ---
@st.cache_data
def analyze_news_sentiment(title, summary):
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
        response = ollama.chat(model=MODEL, messages=[
            {'role': 'system', 'content': 'You are a financial analyst expert. Respond only in valid JSON.'},
            {'role': 'user', 'content': prompt},
        ])
        result_text = response['message']['content']
        start_idx = result_text.find('{')
        end_idx = result_text.rfind('}') + 1
        return json.loads(result_text[start_idx:end_idx])
    except Exception as e:
        print(f"AI Error ({MODEL}): {e}")
        return {"sentiment": "Neutral", "score": 0, "one_liner": "AI 분석 일시 불가"}

# --- 3. 기술적 지표 계산 엔진 (6대 지표) ---
def calculate_comprehensive_signals(df):
    """
    6가지 기술적 지표를 계산하고 매매 시그널을 생성합니다.
    1. MACD: 골든/데드 크로스
    2. RSI: 과매수/과매도 (30/70)
    3. Bollinger Bands: 하단 터치(Buy) / 상단 터치(Sell)
    4. CCI: 과매수/과매도 (±100)
    5. Stochastic: 과매수/과매도 (20/80)
    6. Momentum: 추세 방향 (0 기준)
    """
    if len(df) < 30: return None, None
    
    df = df.copy()
    
    # 1. MACD (12, 26, 9)
    df['EMA12'] = df['Close'].ewm(span=12, adjust=False).mean()
    df['EMA26'] = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = df['EMA12'] - df['EMA26']
    df['Signal_Line'] = df['MACD'].ewm(span=9, adjust=False).mean()
    df['MACD_Hist'] = df['MACD'] - df['Signal_Line']

    # 2. RSI (14)
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).ewm(com=13, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(com=13, adjust=False).mean()
    rs = gain / loss
    df['RSI'] = 100 - (100 / (1 + rs))

    # 3. Bollinger Bands (20, 2)
    df['SMA20'] = df['Close'].rolling(window=20).mean()
    df['STD20'] = df['Close'].rolling(window=20).std()
    df['BB_Upper'] = df['SMA20'] + (df['STD20'] * 2)
    df['BB_Lower'] = df['SMA20'] - (df['STD20'] * 2)

    # 4. CCI (20)
    tp = (df['High'] + df['Low'] + df['Close']) / 3
    df['CCI'] = (tp - tp.rolling(20).mean()) / (0.015 * tp.rolling(20).apply(lambda x: abs(x - x.mean()).mean()))

    # 5. Stochastic Fast (14)
    low_14 = df['Low'].rolling(14).min()
    high_14 = df['High'].rolling(14).max()
    df['Stoch_K'] = (df['Close'] - low_14) / (high_14 - low_14) * 100

    # 6. Momentum (10)
    df['Momentum'] = df['Close'] - df['Close'].shift(10)

    # --- 시그널 판정 ---
    last = df.iloc[-1]
    signals = {}

    # MACD: 히스토그램이 양수면 매수 우위 (Signal선 돌파)
    signals['MACD'] = "Buy" if last['MACD'] > last['Signal_Line'] else "Sell"
    
    # RSI: 30 이하 과매도(Buy), 70 이상 과매수(Sell)
    signals['RSI'] = "Buy" if last['RSI'] < 30 else "Sell" if last['RSI'] > 70 else "Neutral"
    
    # BB: 가격이 하단 밴드보다 낮으면 Buy, 상단보다 높으면 Sell
    signals['BB'] = "Buy" if last['Close'] <= last['BB_Lower'] else "Sell" if last['Close'] >= last['BB_Upper'] else "Neutral"
    
    # CCI
    signals['CCI'] = "Buy" if last['CCI'] < -100 else "Sell" if last['CCI'] > 100 else "Neutral"
    
    # Stochastic
    signals['Stochastic'] = "Buy" if last['Stoch_K'] < 20 else "Sell" if last['Stoch_K'] > 80 else "Neutral"
    
    # Momentum
    signals['Momentum'] = "Buy" if last['Momentum'] > 0 else "Sell"

    return signals, df

# --- 4. UI 출력 함수 ---
def display_ai_news(item):
    try:
        content = item.get('content', {})
        title = content.get('title') or item.get('title') or '제목 없음'
        summary = content.get('summary') or item.get('summary') or ''
        publisher = content.get('provider', {}).get('displayName') or item.get('publisher') or '출처 없음'
        link = content.get('canonicalUrl', {}).get('url') or item.get('link') or '#'
        
        pub_date_str = content.get('pubDate') or item.get('pubDate')
        
        # [수정 1] 날짜 정보가 없으면 False, 0 반환
        if not pub_date_str: return False, 0 

        pub_date = pd.to_datetime(pub_date_str).astimezone(KST)
        
        # [수정 2] 10일 지난 뉴스면 False, 0 반환
        if datetime.now(KST) - pub_date > timedelta(days=10): return False, 0

        ai_analysis = analyze_news_sentiment(title, summary)
        score = ai_analysis.get('score', 0)
        sentiment = ai_analysis.get('sentiment', 'Neutral')
        
        color = "green" if sentiment == "Bullish" else "red" if sentiment == "Bearish" else "gray"
        st.markdown(f"### :{color}[{sentiment} ({score:+.2f})] {title}")
        st.caption(f"**{publisher}** | {pub_date.strftime('%Y-%m-%d %H:%M')} | [Link]({link})")
        
        one_liner = ai_analysis.get('one_liner', '요약 없음')
        if score >= 0.5: st.success(f"**AI 요약:** {one_liner}")
        elif score <= -0.5: st.error(f"**AI 요약:** {one_liner}")
        else: st.info(f"**AI 요약:** {one_liner}")
        st.divider()
        return True, score
    except Exception as e:
        return False, 0

# --- 5. 앱 실행 로직 ---
if 'tickers' not in st.session_state: st.session_state.tickers = load_tickers()
if 'selected_ticker' not in st.session_state: st.session_state.selected_ticker = None

# 사이드바
st.sidebar.header("🕹️ 종목 관리")
st.sidebar.caption(f"🤖 Model: {MODEL}")
new_ticker = st.sidebar.text_input("티커 입력 (예: NVDA)")
if st.sidebar.button("추가"):
    if new_ticker and new_ticker.upper() not in st.session_state.tickers:
        st.session_state.tickers.append(new_ticker.upper())
        save_tickers(st.session_state.tickers)
        st.rerun()
if st.sidebar.button("초기화"):
    st.session_state.tickers = []
    save_tickers([])
    st.rerun()

st.sidebar.divider()
if st.sidebar.button("📰 전체 뉴스 피드"):
    st.session_state.selected_ticker = None
    st.rerun()
for t in st.session_state.tickers:
    if st.sidebar.button(f"📊 {t}", key=f"btn_{t}", use_container_width=True):
        st.session_state.selected_ticker = t
        st.rerun()

# 메인 페이지
st.title("📈 Finance Jarvis Pro")

# 캘린더
with st.expander("📅 배당금 캘린더 (KST 기준)", expanded=False):
    calendar_events = []
    for t in st.session_state.tickers:
        info, _, _, divs = get_stock_data(t)
        if info and info.get('exDividendDate'):
            ex_date = datetime.fromtimestamp(info['exDividendDate'], tz=KST).strftime('%Y-%m-%d')
            calendar_events.append({"title": f"[{t}] 배당락", "start": ex_date, "allDay": True, "color": "#FF6B6B"})
        if divs is not None and not divs.empty:
            for date, amount in divs.items():
                calendar_events.append({"title": f"[{t}] ${amount:.2f}", "start": date.strftime('%Y-%m-%d'), "allDay": True, "color": "#4ECDC4"})
    if calendar_events: calendar(events=calendar_events, options={"initialView": "dayGridMonth"}, key="cal")
    else: st.write("예정된 배당 정보가 없습니다.")

st.divider()

if st.session_state.selected_ticker:
    ticker = st.session_state.selected_ticker
    info, history, news, _ = get_stock_data(ticker)
    
    if history is not None:
        # 1. 종합 신호등 (7-Factor Analysis)
        st.header(f"🚦 {ticker} 7-Factor 투자 신호등")
        tech_signals, df_calc = calculate_comprehensive_signals(history)
        last_row = df_calc.iloc[-1]
        
        # AI 뉴스 점수
        news_scores = []
        if news:
            for n in news[:5]:
                c = n.get('content', {})
                a = analyze_news_sentiment(c.get('title', ''), c.get('summary', ''))
                news_scores.append(a.get('score', 0))
        avg_news_score = sum(news_scores)/len(news_scores) if news_scores else 0
        news_signal = "Buy" if avg_news_score > 0.3 else "Sell" if avg_news_score < -0.3 else "Neutral"

        # 종합 점수 계산 (7개 요소)
        score_map = {"Buy": 1, "Sell": -1, "Neutral": 0}
        tech_score = sum([score_map[s] for s in tech_signals.values()])
        total_score = tech_score + score_map[news_signal]
        
        if total_score >= 4: decision, color = "💎 강력 매수 (Strong Buy)", "blue"
        elif total_score >= 1: decision, color = "✅ 매수 (Buy)", "blue"
        elif total_score <= -4: decision, color = "🚨 강력 매도 (Strong Sell)", "red"
        elif total_score <= -1: decision, color = "🔻 매도 (Sell)", "red"
        else: decision, color = "⚖️ 중립/관망 (Hold)", "gray"

        # 대시보드 UI
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("MACD", tech_signals['MACD'], f"{last_row['MACD_Hist']:.2f}")
        c2.metric("RSI (14)", tech_signals['RSI'], f"{last_row['RSI']:.1f}")
        c3.metric("Bollinger", tech_signals['BB'], f"밴드폭: {(last_row['BB_Upper']-last_row['BB_Lower']):.1f}")
        c4.metric("AI News", news_signal, f"{avg_news_score:+.2f}")
        
        c5, c6, c7, c8 = st.columns(4)
        c5.metric("CCI", tech_signals['CCI'], f"{last_row['CCI']:.1f}")
        c6.metric("Stochastic", tech_signals['Stochastic'], f"{last_row['Stoch_K']:.1f}")
        c7.metric("Momentum", tech_signals['Momentum'], f"{last_row['Momentum']:.2f}")
        c8.markdown(f"### 종합: :{color}[{decision}]")

        # 2. 캔들 + 볼린저 밴드 차트
        st.divider()
        st.subheader("📊 볼린저 밴드 & 캔들 차트")
        
        fig = go.Figure()
        # 캔들
        fig.add_trace(go.Candlestick(x=df_calc.index, open=df_calc['Open'], high=df_calc['High'],
                                     low=df_calc['Low'], close=df_calc['Close'], name='Price',
                                     increasing_line_color='red', decreasing_line_color='blue'))
        # 볼린저 밴드 (상/하단)
        fig.add_trace(go.Scatter(x=df_calc.index, y=df_calc['BB_Upper'], line=dict(color='gray', width=1), name='BB Upper'))
        fig.add_trace(go.Scatter(x=df_calc.index, y=df_calc['BB_Lower'], line=dict(color='gray', width=1), name='BB Lower',
                                 fill='tonexty', fillcolor='rgba(200,200,200,0.1)')) # 밴드 사이 채우기
        
        fig.update_layout(xaxis_rangeslider_visible=False, height=600, title=f"{ticker} Price with Bollinger Bands")
        st.plotly_chart(fig, use_container_width=True)

        # 3. 뉴스
        st.divider()
        st.subheader("📰 AI 뉴스 분석 (최근 10일)")
        if news:
            count = 0
            for n in news:
                displayed, _ = display_ai_news(n)
                if displayed: count += 1
            if count == 0: st.write("최근 10일 뉴스가 없습니다.")
        else: st.write("뉴스가 없습니다.")

else:
    st.header("📰 전체 종목 요약")
    for t in st.session_state.tickers:
        with st.expander(f"{t} 뉴스", expanded=True):
            _, _, news, _ = get_stock_data(t)
            if news:
                found = False
                for n in news:
                    if display_ai_news(n)[0]: 
                        found = True
                        break
                if not found: st.write("최근 뉴스가 없습니다.")