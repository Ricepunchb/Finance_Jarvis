#!/usr/bin/env bash
# scripts/service.sh
# Finance Jarvis 24/7 백그라운드 무중단 데몬 관리 스크립트.
# VSCode 대화창이나 터미널 창을 닫아도 프로세스가 종료되지 않도록 nohup + disown으로 완전 분리 실행합니다.

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

LOGS_DIR="$PROJECT_DIR/logs"
DATA_DIR="$PROJECT_DIR/data"
API_PID_FILE="$DATA_DIR/service_api.pid"
UI_PID_FILE="$DATA_DIR/service_ui.pid"

mkdir -p "$LOGS_DIR" "$DATA_DIR"

is_pid_running() {
    local pid="$1"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0
    else
        return 1
    fi
}

start_service() {
    echo "======================================================="
    echo "🚀 Finance Jarvis 무중단 백그라운드 서비스 기동"
    echo "======================================================="

    # 1. FastAPI 제어 플레인 기동
    local api_pid=""
    if [ -f "$API_PID_FILE" ]; then
        api_pid=$(cat "$API_PID_FILE" 2>/dev/null || true)
    fi

    if is_pid_running "$api_pid"; then
        echo "✅ FastAPI API 서버가 이미 실행 중입니다 (PID: $api_pid, Port: 8800)."
    else
        echo "⏳ FastAPI API 서버 기동 중 (:8800)..."
        # 혹시 남아있는 포트 점유 프로세스 정리
        local old_api
        old_api=$(lsof -ti:8800 2>/dev/null || true)
        if [ -n "$old_api" ]; then
            echo "   기존 :8800 점유 프로세스(PID: $old_api) 종료..."
            kill -15 $old_api 2>/dev/null || true
            sleep 1
        fi

        nohup uv run uvicorn api.main:app --port 8800 --host 0.0.0.0 > "$LOGS_DIR/api.log" 2>&1 &
        api_pid=$!
        disown "$api_pid"
        echo "$api_pid" > "$API_PID_FILE"
        echo "✅ FastAPI 시작됨 (PID: $api_pid, 로그: logs/api.log)"
    fi

    # 2. Streamlit 대시보드 기동
    local ui_pid=""
    if [ -f "$UI_PID_FILE" ]; then
        ui_pid=$(cat "$UI_PID_FILE" 2>/dev/null || true)
    fi

    if is_pid_running "$ui_pid"; then
        echo "✅ Streamlit 대시보드가 이미 실행 중입니다 (PID: $ui_pid, Port: 8501)."
    else
        echo "⏳ Streamlit 대시보드 기동 중 (:8501)..."
        local old_ui
        old_ui=$(lsof -ti:8501 2>/dev/null || true)
        if [ -n "$old_ui" ]; then
            echo "   기존 :8501 점유 프로세스(PID: $old_ui) 종료..."
            kill -15 $old_ui 2>/dev/null || true
            sleep 1
        fi

        nohup uv run streamlit run app.py --server.port 8501 --server.headless true --server.address 0.0.0.0 > "$LOGS_DIR/ui.log" 2>&1 &
        ui_pid=$!
        disown "$ui_pid"
        echo "$ui_pid" > "$UI_PID_FILE"
        echo "✅ Streamlit 시작됨 (PID: $ui_pid, 로그: logs/ui.log)"
    fi

    # 3. 헬스체크 및 엔진/스케줄러 자동 가동
    echo "⏳ 서비스 헬스체크 및 트레이딩 엔진 연결 대기 (최대 10초)..."
    local waited=0
    local healthy=false
    while [ $waited -lt 10 ]; do
        if curl -s "http://127.0.0.1:8800/engine/status" > /dev/null 2>&1; then
            healthy=true
            break
        fi
        sleep 1
        waited=$((waited + 1))
    done

    if [ "$healthy" = true ]; then
        echo "✅ 제어 플레인 정상 응답 확인."

        # 트레이딩 엔진 자동 시작
        echo "⚡ 30분 모니터링/매매 루프(TradingEngine) 기동..."
        local start_res
        start_res=$(curl -s -X POST "http://127.0.0.1:8800/engine/start" || true)
        echo "   결과: $start_res"

        # 스케줄러 및 Auto-Apply 모드 자동 활성화
        echo "⚡ AI 스케줄러 및 무승인 자율 매매(Auto-Apply) 활성화..."
        local sched_res
        sched_res=$(curl -s -X POST "http://127.0.0.1:8800/ai-rebalance/scheduler" \
            -H "Content-Type: application/json" \
            -d '{"enabled": true, "auto_apply": true}' || true)
        echo "   결과: $sched_res"
    else
        echo "⚠️ FastAPI 시작 대기 시간 초과. logs/api.log를 확인해주세요."
    fi

    echo ""
    echo "🎉 서비스 기동 완료!"
    echo "· VSCode 창이나 터미널 창을 닫아도 백그라운드 24/7 계속 실행됩니다."
    echo "· 대시보드 URL: http://localhost:8501"
    echo "· 제어 API URL: http://localhost:8800/docs"
    echo "· 상태 확인:   ./scripts/service.sh status"
    echo "· 서비스 중지: ./scripts/service.sh stop"
    echo "======================================================="
}

stop_service() {
    echo "======================================================="
    echo "🛑 Finance Jarvis 서비스 중지"
    echo "======================================================="

    # 1. 트레이딩 엔진 정상 정지
    echo "⏳ 트레이딩 엔진 중지 요청 (/engine/stop)..."
    curl -s -X POST "http://127.0.0.1:8800/engine/stop" > /dev/null 2>&1 || true

    # 2. API 프로세스 종료
    if [ -f "$API_PID_FILE" ]; then
        local api_pid
        api_pid=$(cat "$API_PID_FILE" 2>/dev/null || true)
        if is_pid_running "$api_pid"; then
            echo "⏳ FastAPI 종료 (PID: $api_pid)..."
            kill -15 "$api_pid" 2>/dev/null || true
            sleep 1
            if is_pid_running "$api_pid"; then
                kill -9 "$api_pid" 2>/dev/null || true
            fi
        fi
        rm -f "$API_PID_FILE"
    fi

    # 혹시 남은 8800 포트 정리
    local rem_api
    rem_api=$(lsof -ti:8800 2>/dev/null || true)
    if [ -n "$rem_api" ]; then
        kill -9 $rem_api 2>/dev/null || true
    fi
    echo "✅ FastAPI 중지 완료."

    # 3. Streamlit 프로세스 종료
    if [ -f "$UI_PID_FILE" ]; then
        local ui_pid
        ui_pid=$(cat "$UI_PID_FILE" 2>/dev/null || true)
        if is_pid_running "$ui_pid"; then
            echo "⏳ Streamlit 종료 (PID: $ui_pid)..."
            kill -15 "$ui_pid" 2>/dev/null || true
            sleep 1
            if is_pid_running "$ui_pid"; then
                kill -9 "$ui_pid" 2>/dev/null || true
            fi
        fi
        rm -f "$UI_PID_FILE"
    fi

    local rem_ui
    rem_ui=$(lsof -ti:8501 2>/dev/null || true)
    if [ -n "$rem_ui" ]; then
        kill -9 $rem_ui 2>/dev/null || true
    fi
    echo "✅ Streamlit 중지 완료."
    echo "======================================================="
}

show_status() {
    echo "======================================================="
    echo "📊 Finance Jarvis 서비스 상태"
    echo "======================================================="

    local api_pid=""
    if [ -f "$API_PID_FILE" ]; then
        api_pid=$(cat "$API_PID_FILE" 2>/dev/null || true)
    fi
    if ! is_pid_running "$api_pid"; then
        api_pid=$(lsof -ti:8800 2>/dev/null | head -n 1 || true)
    fi

    local ui_pid=""
    if [ -f "$UI_PID_FILE" ]; then
        ui_pid=$(cat "$UI_PID_FILE" 2>/dev/null || true)
    fi
    if ! is_pid_running "$ui_pid"; then
        ui_pid=$(lsof -ti:8501 2>/dev/null | head -n 1 || true)
    fi

    local tmux_info=""
    if tmux has-session -t engine 2>/dev/null || tmux has-session -t front 2>/dev/null; then
        tmux_info=" (tmux 세션 연동)"
    fi

    echo "· FastAPI (:8800):    $([ -n "$api_pid" ] && echo "🟢 실행 중 (PID: $api_pid)$tmux_info" || echo "⚪ 정지됨")"
    echo "· Streamlit (:8501):  $([ -n "$ui_pid" ] && echo "🟢 실행 중 (PID: $ui_pid)$tmux_info" || echo "⚪ 정지됨")"

    if curl -s "http://127.0.0.1:8800/engine/status" > /dev/null 2>&1; then
        local eng_stat
        eng_stat=$(curl -s "http://127.0.0.1:8800/engine/status")
        local sched_stat
        sched_stat=$(curl -s "http://127.0.0.1:8800/ai-rebalance/scheduler")

        echo "· 트레이딩 엔진:      $(echo "$eng_stat" | grep -o '"engine_running":[^,]*' || echo '정보 없음')"
        echo "· 킬스위치 상태:      $(echo "$eng_stat" | grep -o '"kill_switch_active":[^,]*' || echo '정보 없음')"
        echo "· 스케줄러 / Auto:    $(echo "$sched_stat" | grep -o '"enabled":[^,]*' || echo '') | $(echo "$sched_stat" | grep -o '"auto_apply":[^,]*' || echo '')"
    else
        echo "· API 상태:           ⚠️ 연결 불가"
    fi
    echo "======================================================="
}

show_logs() {
    local target="${1:-api}"
    if [ "$target" = "ui" ]; then
        echo "=== [Streamlit Logs: logs/ui.log] ==="
        tail -n 40 "$LOGS_DIR/ui.log" 2>/dev/null || echo "로그 파일 없음"
    else
        echo "=== [FastAPI Logs: logs/api.log] ==="
        tail -n 40 "$LOGS_DIR/api.log" 2>/dev/null || echo "로그 파일 없음"
    fi
}

case "${1:-status}" in
    start)
        start_service
        ;;
    stop)
        stop_service
        ;;
    restart)
        stop_service
        sleep 2
        start_service
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs "$2"
        ;;
    *)
        echo "사용법: $0 {start|stop|restart|status|logs [api|ui]}"
        exit 1
        ;;
esac
