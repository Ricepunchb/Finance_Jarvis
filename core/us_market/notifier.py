# core/us_market/notifier.py
"""미국장 시그널 알림 및 텔레그램(Telegram) 모바일 푸시 발송 모듈.

한국 시각 야간(정규장 22:30~05:00)에 화면을 켜두지 않아도 스마트폰 텔레그램으로
30분 주기 매수/매도 시그널 및 손절/익절 경고를 실시간 전송합니다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import logging
import os
import re
import time
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 동일 종목에 대한 동일 시그널 반복 발송 방지 (쿨다운 1시간)
_ALERT_COOLDOWN_SEC = 3600
_LAST_ALERT_SENT: Dict[str, Tuple[str, float]] = {}  # symbol -> (action, timestamp)


from core.config import settings


def get_telegram_config() -> Tuple[Optional[str], Optional[str]]:
    """환경변수 또는 설정 파일에서 텔레그램 봇 토큰 및 채팅 ID 조회."""
    token = (
        getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TG_BOT_TOKEN")
    )
    chat_id = (
        getattr(settings, "TELEGRAM_CHAT_ID", None)
        or os.getenv("TELEGRAM_CHAT_ID")
        or os.getenv("TG_CHAT_ID")
    )
    return token, chat_id


def send_telegram_message_with_result(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "Markdown",
    timeout: int = 10,
) -> Tuple[bool, str]:
    """Telegram Bot API를 통한 단일 메시지 전송 및 결과/오류 메시지 반환."""
    if not token or not str(token).strip():
        return False, "봇 토큰(TELEGRAM_BOT_TOKEN)이 비어 있습니다."
    if not chat_id or not str(chat_id).strip():
        return False, "채팅 ID(TELEGRAM_CHAT_ID)가 비어 있습니다."
    if not text:
        return False, "메시지 내용이 비어 있습니다."

    url = f"https://api.telegram.org/bot{str(token).strip()}/sendMessage"
    payload = {
        "chat_id": str(chat_id).strip(),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code == 200 and data.get("ok"):
            logger.info("텔레그램 알림 발송 성공")
            return True, "성공"

        err_desc = data.get("description", resp.text)
        logger.warning(f"텔레그램 알림 발송 실패: status={resp.status_code}, response={resp.text}")

        # 사용자 친화적 가이드 추가
        if "chat not found" in err_desc.lower():
            err_desc += " ➔ [원인 & 해결법]: 1) 스마트폰/PC 텔레그램에서 생성한 봇(@..._bot) 대화방에 들어가 하단의 [시작(Start)] 버튼(또는 /start)을 반드시 한 번 먼저 눌러주셔야 합니다. (봇은 먼저 말을 걸 수 없습니다) 2) Chat ID는 @아이디가 아닌 순수 숫자(예: 123456789)여야 합니다."
        elif "unauthorized" in err_desc.lower():
            err_desc += " ➔ [원인 & 해결법]: 봇 토큰(HTTP API Token)이 올바르지 않습니다. BotFather가 발급해 준 전체 토큰 문자열을 다시 확인해주세요."

        return False, err_desc
    except Exception as e:
        logger.warning(f"텔레그램 전송 중 네트워크 예외: {e}")
        return False, f"네트워크 통신 오류: {e}"


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    parse_mode: str = "Markdown",
    timeout: int = 10,
) -> bool:
    """Telegram Bot API를 통한 단일 메시지 전송 (동기 함수)."""
    ok, _ = send_telegram_message_with_result(token, chat_id, text, parse_mode, timeout)
    return ok


async def send_telegram_message_async(token: str, chat_id: str, text: str) -> Tuple[bool, str]:
    """비동기 텔레그램 메시지 발송."""
    return await asyncio.to_thread(send_telegram_message_with_result, token, chat_id, text)


def _clean_emojis(text: str) -> str:
    if not text:
        return ""
    # 불필요한 시각적 이모지 및 특수 구분선 제거
    cleaned = re.sub(r"[🚨🎯🔔⚠️ℹ️📌💵📊🟢🔴🟡📝⏰💡⚡🏢📈🤖]+", "", text)
    return cleaned.strip()


def format_signal_alert_message(signal_data: Dict[str, Any], market_status_str: str = "") -> str:
    """쓸데없는 이모지를 배제하고 종목명과 판정을 최우선으로 강조한 고가독성 텔레그램 메시지 서식."""
    sym = signal_data.get("symbol", "").upper()
    price = signal_data.get("price", 0.0)
    action = signal_data.get("combined_action", "HOLD")
    strength = int(signal_data.get("strength", 0.0) * 100)
    exit_trigger = signal_data.get("exit_trigger", "NONE")
    recommendation = _clean_emojis(signal_data.get("action_recommendation", ""))
    pnl_pct = signal_data.get("pnl_pct")

    now_kst = datetime.now(tz=KST).strftime("%m-%d %H:%M KST")

    # 1. 핵심 정보 (종목명 · 판정)
    if exit_trigger == "STOP_LOSS":
        action_name = "손절 (STOP LOSS)"
    elif exit_trigger == "TRAILING_TAKE_PROFIT":
        action_name = "익절 (TRAILING TP)"
    elif action == "BUY":
        action_name = "매수 (BUY)"
    elif action == "SELL":
        action_name = "매도 (SELL)"
    else:
        action_name = "관망 (HOLD)"

    # 헤더: 알림 배너 및 잠금화면에서 바로 핵심을 확인할 수 있도록 종목명과 판정 전면 배치
    lines = [
        f"[{sym}] {action_name}",
        "",
        f"• 현재가: ${price:,.2f}",
    ]

    # 2. 부차적 정보 정렬
    if pnl_pct is not None:
        lines.append(f"• 보유손익: {pnl_pct:+.2f}%")

    if exit_trigger == "NONE" and action in ("BUY", "SELL") and strength > 0:
        lines.append(f"• 시그널강도: {strength}%")

    if recommendation:
        lines.append(f"• 판단근거: {recommendation}")

    time_str = now_kst
    if market_status_str:
        time_str += f" ({market_status_str})"
    lines.append(f"• 기준시각: {time_str}")

    return "\n".join(lines)


async def notify_signal_if_needed(
    signal_data: Dict[str, Any],
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    market_status_str: str = "",
    last_alert_key: Optional[str] = None,
) -> bool:
    """시그널이 이전 상태에서 변경되었을 때만(예: HOLD -> BUY, BUY -> SELL, SELL -> STOP_LOSS 등) 텔레그램 알림을 발송한다."""
    effective_token, effective_chat_id = token, chat_id
    if not effective_token or not effective_chat_id:
        env_token, env_chat_id = get_telegram_config()
        effective_token = effective_token or env_token
        effective_chat_id = effective_chat_id or env_chat_id

    if not effective_token or not effective_chat_id:
        return False  # 텔레그램 미설정 시 조용히 스킵

    action = signal_data.get("combined_action", "HOLD")
    exit_trigger = signal_data.get("exit_trigger", "NONE")
    sym = signal_data.get("symbol", "")
    current_key = f"{action}:{exit_trigger}"

    # 관망(HOLD)이고 Exit Guard도 없으면 발송 안 함
    if action == "HOLD" and exit_trigger == "NONE":
        return False

    # 직전 알림 키와 비교: DB의 last_alert_key 또는 메모리 _LAST_ALERT_SENT 기준
    prev_key = last_alert_key
    if not prev_key and sym in _LAST_ALERT_SENT:
        prev_key, _ = _LAST_ALERT_SENT[sym]

    # 시그널 변경이 없는 경우 (동일 시그널 지속) -> 텔레그램 중복 발송 차단
    if prev_key == current_key:
        logger.info(f"'{sym}' 시그널 변경 없음({current_key}) ➔ 텔레그램 중복 발송 생략 (시그널 전환 시에만 발송)")
        return False

    msg = format_signal_alert_message(signal_data, market_status_str)
    res = await send_telegram_message_async(effective_token, effective_chat_id, msg)
    ok = res[0] if isinstance(res, tuple) else bool(res)
    if ok:
        _LAST_ALERT_SENT[sym] = (current_key, time.time())
    return ok
