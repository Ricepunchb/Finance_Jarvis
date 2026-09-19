# Finance Jarvis

한국투자증권(KIS) OpenAPI 기반 자동매매/리밸런싱 엔진 + 해외주식 분석 대시보드.

## 실행 방법 (Phase 1: 국내주식·모의투자 전용)

1. `.env.template`을 참고해 `.env`를 작성한다. `KIS_HTS_ID`(HTS 로그인 ID, 실시간 체결통보 구독에 필요)를 반드시 채워야 한다.
2. `uv sync`
3. 제어 플레인(FastAPI) 실행: `uv run uvicorn api.main:app --port 8800`
4. 별도 터미널에서 대시보드 실행: `uv run streamlit run app.py` → 좌측 메뉴에서 "KIS 자동매매" 페이지로 이동
5. 대시보드에서 종목 등록 → 목표 비중 입력 → "시작" 버튼

**주의**: `IS_MOCK=False`(실전투자)로 전환하려면 `.env`에 `I_UNDERSTAND_REAL_MONEY_RISK=true`도 함께 설정해야 한다. Phase 1 단계에서는 항상 모의투자로만 검증할 것.

자세한 설계/단계별 계획은 `/home/idsh/.claude/plans/valiant-watching-barto.md` 참고.
