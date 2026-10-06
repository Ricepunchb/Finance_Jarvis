# core/profile_manager.py
"""멀티 계좌 프로필 관리 및 관제 모듈.

각 프로필(.env.{name})의 설정 파일 파싱, tmux 세션(엔진 및 대시보드) 실행/중지/상태 감지,
FastAPI 제어 플레인 헬스체크 및 실시간 자산 모니터링을 담당합니다.
CLI(scripts/manage_profile.py)와 웹 UI(pages/0_계좌_마스터.py)에서 공통으로 사용됩니다.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
ACTIVE_PROFILE_FILE = ROOT / ".active_profile"

# 기본 포트 및 명칭 매핑 (설정 파일에 없을 시 폴백)
PROFILE_DEFAULTS = {
    "isa": {
        "display": "ISA 절세 계좌",
        "api_port": 8800,
        "front_port": 8501,
        "is_mock": False,
    },
    "real": {
        "display": "실전 일반 계좌 (수수료 무료)",
        "api_port": 8801,
        "front_port": 8502,
        "is_mock": False,
    },
    "mock": {
        "display": "모의투자 계좌",
        "api_port": 8802,
        "front_port": 8503,
        "is_mock": True,
    },
}

MASTER_FRONT_PORT = 8500
MASTER_SESSION_NAME = "front-master"


def get_active_profile() -> str:
    """현재 기본 활성 프로필 이름을 반환 (.active_profile 기준)."""
    if ACTIVE_PROFILE_FILE.exists():
        try:
            val = ACTIVE_PROFILE_FILE.read_text(encoding="utf-8").strip()
            if val:
                return val
        except Exception as e:
            logger.warning(f"Failed to read .active_profile: {e}")
    return "isa"


def set_active_profile(name: str) -> bool:
    """기본 활성 프로필을 변경하고 .env 파일을 동기화."""
    try:
        name_clean = name.strip()
        ACTIVE_PROFILE_FILE.write_text(name_clean, encoding="utf-8")
        env_target = ROOT / f".env.{name_clean}"
        if env_target.exists():
            shutil.copy2(env_target, ROOT / ".env")
        return True
    except Exception as e:
        logger.error(f"Failed to set active profile '{name}': {e}")
        return False


def get_tmux_sessions() -> Set[str]:
    """현재 실행 중인 tmux 세션 이름 목록을 반환."""
    try:
        out = subprocess.check_output(["tmux", "ls"], stderr=subprocess.DEVNULL).decode("utf-8")
        sessions = set()
        for line in out.splitlines():
            if ":" in line:
                sessions.add(line.split(":", 1)[0].strip())
        return sessions
    except Exception:
        return set()


def parse_env_file(env_path: Path) -> Dict[str, Any]:
    """단일 .env 파일의 핵심 설정 메타데이터 파싱."""
    name = env_path.name.replace(".env.", "")
    defaults = PROFILE_DEFAULTS.get(name, {})

    config: Dict[str, Any] = {
        "name": name,
        "display_name": defaults.get("display", name),
        "api_port": defaults.get("api_port", 8800),
        "front_port": defaults.get("front_port", 8501),
        "account_no": "-",
        "is_mock": defaults.get("is_mock", False),
        "domestic_only": True,
        "proxy_trading": False,
        "hts_id": "-",
        "understand_risk": False,
    }

    try:
        content = env_path.read_text(encoding="utf-8")
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")

            if k == "PROFILE_NAME":
                config["name"] = v
            elif k == "PROFILE_DISPLAY_NAME":
                config["display_name"] = v
            elif k == "API_PORT":
                try:
                    config["api_port"] = int(v)
                except ValueError:
                    pass
            elif k == "FRONT_PORT":
                try:
                    config["front_port"] = int(v)
                except ValueError:
                    pass
            elif k == "KIS_ACCOUNT_NO":
                config["account_no"] = v
            elif k == "KIS_HTS_ID":
                config["hts_id"] = v
            elif k == "IS_MOCK":
                config["is_mock"] = v.lower() in ("true", "1", "yes")
            elif k == "DOMESTIC_ONLY":
                config["domestic_only"] = v.lower() in ("true", "1", "yes")
            elif k == "OVERSEAS_PROXY_TRADING_ENABLED":
                config["proxy_trading"] = v.lower() in ("true", "1", "yes")
            elif k == "I_UNDERSTAND_REAL_MONEY_RISK":
                config["understand_risk"] = v.lower() in ("true", "1", "yes")
    except Exception as e:
        logger.warning(f"Error parsing {env_path}: {e}")

    return config


def query_api_status(api_port: int, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
    """FastAPI 엔진 제어 플레인 /engine/status 조회."""
    url = f"http://127.0.0.1:{api_port}/engine/status"
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def query_analytics_overview(api_port: int, timeout: float = 1.2) -> Optional[Dict[str, Any]]:
    """FastAPI 엔진 제어 플레인 /analytics/overview 조회 (자산 및 손익)."""
    url = f"http://127.0.0.1:{api_port}/analytics/overview"
    try:
        resp = requests.get(url, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def get_all_profiles_info() -> List[Dict[str, Any]]:
    """등록된 모든 프로필(.env.*)의 실시간 상태 및 메타데이터를 수집하여 리스트로 반환."""
    active = get_active_profile()
    tmux_sessions = get_tmux_sessions()

    profiles_map: Dict[str, Path] = {}
    for p in sorted(ROOT.glob(".env.*")):
        if p.name.endswith(".template") or p.name.endswith(".bak"):
            continue
        p_name = p.name.replace(".env.", "")
        profiles_map[p_name] = p

    # 등록된 기본 3대 계좌(isa, real, mock) 중 누락된 것은 순서에 맞춤
    result = []
    for name, path in sorted(profiles_map.items()):
        meta = parse_env_file(path)
        is_active = (name == active)

        engine_sess = f"engine-{name}"
        front_sess = f"front-{name}"

        # 레거시 단일 이름 호환
        if is_active and "engine" in tmux_sessions:
            engine_sess = "engine"
        if is_active and "front" in tmux_sessions:
            front_sess = "front"

        engine_running = engine_sess in tmux_sessions
        front_running = front_sess in tmux_sessions

        # API 실시간 조회
        api_port = meta["api_port"]
        api_status = None
        analytics_info = None

        if engine_running:
            api_status = query_api_status(api_port)
            if api_status:
                analytics_info = query_analytics_overview(api_port)

        # 종합 상태 텍스트 및 배지
        if engine_running and front_running:
            status_desc = "🟢 가동 중 (엔진+대시보드)"
            status_code = "running"
        elif engine_running:
            status_desc = "🟡 엔진만 가동 중"
            status_code = "engine_only"
        elif front_running:
            status_desc = "🟡 대시보드만 가동 중"
            status_code = "front_only"
        else:
            status_desc = "⚪ 정지됨"
            status_code = "stopped"

        engine_loop_running = False
        ws_alive = False
        kill_switch = False
        holdings_value = 0.0
        unrealized_pnl = 0.0
        realized_pnl = 0.0

        if api_status:
            engine_loop_running = api_status.get("engine_running", False)
            ws_age = api_status.get("ws_last_message_age_sec")
            ws_alive = (ws_age is not None and ws_age < 180)
            kill_switch = api_status.get("kill_switch_active", False)

        if analytics_info and "kpis" in analytics_info:
            kpis = analytics_info["kpis"]
            holdings_value = float(kpis.get("holdings_value_krw", 0.0))
            unrealized_pnl = float(kpis.get("unrealized_krw", 0.0))
            realized_pnl = float(kpis.get("realized_krw", 0.0))

        item = {
            "name": name,
            "display_name": meta["display_name"],
            "account_no": meta["account_no"],
            "is_mock": meta["is_mock"],
            "api_port": meta["api_port"],
            "front_port": meta["front_port"],
            "domestic_only": meta["domestic_only"],
            "proxy_trading": meta["proxy_trading"],
            "is_active": is_active,
            "engine_session": engine_sess,
            "front_session": front_sess,
            "engine_running": engine_running,
            "front_running": front_running,
            "status_desc": status_desc,
            "status_code": status_code,
            "api_online": api_status is not None,
            "api_status": api_status,
            "engine_loop_running": engine_loop_running,
            "ws_alive": ws_alive,
            "kill_switch": kill_switch,
            "holdings_value": holdings_value,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
        }
        result.append(item)

    return result


def start_component(name: str, component: str = "both") -> Tuple[bool, str]:
    """특정 프로필의 engine 또는 front 컴포넌트를 tmux로 기동.
    
    component: "engine", "front", "both"
    """
    env_file = ROOT / f".env.{name}"
    if not env_file.exists():
        return False, f"프로필 파일이 존재하지 않습니다: .env.{name}"

    meta = parse_env_file(env_file)
    api_port = meta["api_port"]
    front_port = meta["front_port"]

    engine_sess = f"engine-{name}"
    front_sess = f"front-{name}"
    tmux_sessions = get_tmux_sessions()
    messages = []

    # 1. 백엔드 엔진
    if component in ("engine", "both"):
        if engine_sess in tmux_sessions:
            messages.append(f"엔진({engine_sess})은 이미 실행 중입니다.")
        else:
            cmd = f"cd {ROOT} && JARVIS_PROFILE={name} uv run uvicorn api.main:app --port {api_port} --reload"
            subprocess.run(["tmux", "new-session", "-d", "-s", engine_sess, cmd])
            messages.append(f"엔진({engine_sess}) 기동 완료 (Port: {api_port})")

    # 2. 대시보드
    if component in ("front", "both"):
        if front_sess in tmux_sessions:
            messages.append(f"대시보드({front_sess})는 이미 실행 중입니다.")
        else:
            cmd = f"cd {ROOT} && JARVIS_PROFILE={name} uv run streamlit run app.py --server.port {front_port}"
            subprocess.run(["tmux", "new-session", "-d", "-s", front_sess, cmd])
            messages.append(f"대시보드({front_sess}) 기동 완료 (Port: {front_port})")

    return True, "\n".join(messages)


def stop_component(name: str, component: str = "both") -> Tuple[bool, str]:
    """특정 프로필의 engine 또는 front 세션을 안전하게 종료."""
    engine_sess = f"engine-{name}"
    front_sess = f"front-{name}"
    active = get_active_profile()
    tmux_sessions = get_tmux_sessions()

    # 레거시 호환
    if name == active and "engine" in tmux_sessions:
        engine_sess = "engine"
    if name == active and "front" in tmux_sessions:
        front_sess = "front"

    messages = []

    # 엔진 중지 시 제어 플레인의 /engine/stop 먼저 우아하게 호출 시도
    if component in ("engine", "both") and engine_sess in tmux_sessions:
        env_file = ROOT / f".env.{name}"
        if env_file.exists():
            meta = parse_env_file(env_file)
            try:
                requests.post(f"http://127.0.0.1:{meta['api_port']}/engine/stop", timeout=2.0)
            except Exception:
                pass
        subprocess.run(["tmux", "kill-session", "-t", engine_sess])
        messages.append(f"엔진({engine_sess}) 세션 종료")

    if component in ("front", "both") and front_sess in tmux_sessions:
        subprocess.run(["tmux", "kill-session", "-t", front_sess])
        messages.append(f"대시보드({front_sess}) 세션 종료")

    if not messages:
        return False, f"실행 중인 [{name}] {component} 세션이 없습니다."

    return True, "\n".join(messages)


def start_profile(name: str, auto_start_trading: bool = True) -> Tuple[bool, str]:
    """프로필의 전체(엔진 + 대시보드) 기동 및 옵션에 따라 트레이딩 루프 기동."""
    ok, msg = start_component(name, "both")
    if not ok:
        return False, msg

    # 트레이딩 루프 기동
    if auto_start_trading:
        env_file = ROOT / f".env.{name}"
        meta = parse_env_file(env_file)
        api_port = meta["api_port"]
        # API 부팅 대기 (최대 4초)
        for _ in range(8):
            time.sleep(0.5)
            status = query_api_status(api_port)
            if status is not None:
                try:
                    requests.post(f"http://127.0.0.1:{api_port}/engine/start", timeout=5.0)
                    msg += f"\n트레이딩 루프(/engine/start)가 자동 활성화되었습니다."
                except Exception as e:
                    logger.warning(f"Failed to trigger /engine/start: {e}")
                break

    return True, msg


def stop_profile(name: str) -> Tuple[bool, str]:
    """프로필 전체(엔진 + 대시보드) 종료."""
    return stop_component(name, "both")


def restart_profile(name: str, auto_start_trading: bool = True) -> Tuple[bool, str]:
    """프로필 전체 재기동."""
    stop_component(name, "both")
    time.sleep(1.2)
    return start_profile(name, auto_start_trading=auto_start_trading)


def switch_profile(name: str) -> Tuple[bool, str]:
    """기본 활성 프로필 변경."""
    env_file = ROOT / f".env.{name}"
    if not env_file.exists():
        return False, f"프로필 설정 파일이 존재하지 않습니다: .env.{name}"
    set_active_profile(name)
    return True, f"기본 활성 프로필이 '{name}'(으)로 변경되었습니다 (.env 동기화 완료)."


def trigger_engine_loop(api_port: int, action: str = "start") -> Tuple[bool, str]:
    """실행 중인 FastAPI 엔진에 /engine/start 또는 /engine/stop 명령 전송."""
    url = f"http://127.0.0.1:{api_port}/engine/{action}"
    try:
        resp = requests.post(url, timeout=10.0)
        if resp.status_code == 200:
            return True, f"엔진 루프 {action} 성공"
        else:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            return False, f"요청 실패 ({resp.status_code}): {detail}"
    except Exception as e:
        return False, f"API 통신 오류: {e}"


def clear_kill_switch(api_port: int) -> Tuple[bool, str]:
    """실행 중인 FastAPI 엔진의 킬스위치 해제."""
    url = f"http://127.0.0.1:{api_port}/engine/clear-kill-switch"
    try:
        resp = requests.post(url, timeout=5.0)
        if resp.status_code == 200:
            return True, "킬스위치가 성공적으로 해제되었습니다."
        return False, f"해제 실패 ({resp.status_code}): {resp.text}"
    except Exception as e:
        return False, f"API 통신 오류: {e}"


def get_session_output(session_name: str, lines: int = 50) -> str:
    """tmux 세션 콘솔의 최근 출력 텍스트 캡처."""
    try:
        out = subprocess.check_output(
            ["tmux", "capture-pane", "-pt", session_name, "-p", "-S", f"-{lines}"],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
        return out
    except Exception as e:
        return f"콘솔 출력을 가져올 수 없습니다 ({session_name}): {e}"


def is_master_session_running() -> bool:
    """전용 마스터 포털(:8500) tmux 세션 실행 여부."""
    return MASTER_SESSION_NAME in get_tmux_sessions()


def start_master_portal() -> Tuple[bool, str]:
    """전용 마스터 웹 포털(:8500) tmux 세션 기동."""
    sessions = get_tmux_sessions()
    if MASTER_SESSION_NAME in sessions:
        return True, "마스터 포털 세션이 이미 실행 중입니다."
    cmd = f"cd {ROOT} && uv run streamlit run app.py --server.port {MASTER_FRONT_PORT}"
    subprocess.run(["tmux", "new-session", "-d", "-s", MASTER_SESSION_NAME, cmd])
    return True, f"마스터 포털 기동 완료 (접속: http://localhost:{MASTER_FRONT_PORT})"

