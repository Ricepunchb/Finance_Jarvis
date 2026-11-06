# core/meritz_client.py
"""메리츠증권 Open API 비동기 REST 클라이언트.

메리츠 게이트웨이의 핵심 규칙:
1. 계좌번호(acno/asno/acnt_pwd)는 앱키에 1:1 바인딩되어 있으므로 요청 파라미터에 넣지 않는다.
2. 모든 요청에 12자리 HEX 형식의 `mac_address` 헤더가 필수다 (누락 시 IGW50024 거절).
3. 오류 응답도 HTTP 500 JSON으로 내려오므로 상태코드가 아니라 본문의 `rsp_cd`로 판정한다.
   - 성공 코드(OK_CODES): 0000, 0001, 5620, 5762(연속조회), 5766, 5820(자료없음/일부포함), 5822
   - 실제 조치 사유는 `rsp_msg`와 `rsp_sub_msg`를 함께 조합해야 알 수 있다.
4. API당 초당 호출 한도는 10건(TPS=10)이며 초과 시 HTTP 500 + `EGW00200`이 반환된다.
5. 토큰 만료/폐기 코드(`EGW00121`, `EGW00123`) 수신 시 캐시를 비우고 1회 자동 재발급 후 재시도한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from core.config import settings

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent

# 토큰 만료 전 여유 시간 (10분)
TOKEN_REFRESH_MARGIN_SEC = 60 * 10

# 메리츠 초당 호출 제한: API당 10건(TPS=10)이므로 안전하게 약 9 req/sec (0.11초 간격)으로 제어
MIN_REQUEST_INTERVAL_SEC = 0.11

# 게이트웨이 응답 코드 정의
OK_CODES = {"0000", "0001", "5620", "5762", "5766", "5820", "5822"}
NO_DATA_CODES = {"5820", "5822"}
RETRYABLE_CODES = {"EGW00200"}
TOKEN_DEAD_CODES = {"EGW00121", "EGW00123"}

RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SEC = 1.0


def mac_address() -> str:
    """메리츠 게이트웨이 필수 헤더 (12자리 대문자 HEX). 없으면 IGW50024로 거절된다."""
    n = uuid.getnode()
    return "".join(f"{(n >> s) & 0xFF:02X}" for s in range(40, -1, -8))


def format_meritz_message(body: Any, fallback: str = "") -> str:
    """rsp_msg와 rsp_sub_msg를 함께 이어 붙여 구체적인 오류 원인을 반환한다."""
    if not isinstance(body, dict):
        return fallback
    main = str(body.get("rsp_msg") or "").strip()
    sub = str(body.get("rsp_sub_msg") or "").strip()
    if main and sub and sub not in main:
        return f"{main} ({sub})"
    return main or sub or fallback


class MeritzApiError(Exception):
    """메리츠 Open API 호출 실패 또는 업무 오류 시 발생하는 예외.

    기존 KisApiError와 호환되도록 msg_cd / msg1 속성을 함께 제공한다.
    """

    def __init__(
        self,
        rsp_cd: str,
        message: str,
        raw: Optional[Dict[str, Any]] = None,
        status: Optional[int] = None,
        warn_cls_code: Optional[str] = None,
        resendable: bool = False,
    ):
        self.rsp_cd = rsp_cd
        self.msg_cd = rsp_cd
        self.rsp_msg = message
        self.msg1 = message
        self.raw = raw or {}
        self.status = status
        self.warn_cls_code = warn_cls_code
        self.resendable = resendable
        super().__init__(f"Meritz API 오류 [{rsp_cd}]: {message}")


def ensure_ok(response: Dict[str, Any], status: int = 200) -> Dict[str, Any]:
    """메리츠 응답 본문의 rsp_cd를 검증하고 실패 시 MeritzApiError를 발생시킨다."""
    if not isinstance(response, dict):
        raise MeritzApiError(
            rsp_cd="BAD_RESPONSE",
            message=f"비정상 응답 형식 (HTTP {status})",
            raw={"body": response},
            status=status,
        )
    code = str(response.get("rsp_cd") or "").strip()
    if status != 200 or code not in OK_CODES:
        err_code = code or f"HTTP_{status}"
        msg = format_meritz_message(response, f"업무 오류 ({err_code})")
        raise MeritzApiError(rsp_cd=err_code, message=msg, raw=response, status=status)
    return response


class AsyncMeritzClient:
    """비동기 메리츠증권 Open API 클라이언트."""

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.base_url = (base_url or settings.MERITZ_BASE_URL).rstrip("/")
        self.app_key = app_key if app_key is not None else settings.MERITZ_APP_KEY
        self.app_secret = app_secret if app_secret is not None else settings.MERITZ_APP_SECRET
        self.session: Optional[aiohttp.ClientSession] = None
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._next_request_at: float = 0.0
        self._mac = mac_address()

    @property
    def token_cache_file(self) -> Path:
        p_name = getattr(settings, "PROFILE_NAME", "") or "meritz"
        return ROOT_DIR / "data" / f"meritz_token_{p_name}.json"

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def invalidate_token(self) -> None:
        self.access_token = None
        self.token_expires_at = 0.0
        try:
            if self.token_cache_file.exists():
                self.token_cache_file.unlink()
        except Exception:
            pass

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
    )
    async def issue_token(self, force_refresh: bool = False) -> str:
        """OAuth 접근 토큰을 발급받거나 유효한 캐시 토큰을 반환한다."""
        async with self._token_lock:
            now = time.time()
            if (
                not force_refresh
                and self.access_token
                and now < self.token_expires_at - TOKEN_REFRESH_MARGIN_SEC
            ):
                return self.access_token

            token_file = self.token_cache_file
            if not force_refresh and token_file.exists():
                try:
                    cached = json.loads(token_file.read_text(encoding="utf-8"))
                    c_token = cached.get("access_token")
                    c_expires = float(cached.get("expires_at", 0.0))
                    c_app_key = cached.get("app_key")
                    c_base_url = cached.get("base_url")
                    if (
                        c_token
                        and c_app_key == self.app_key
                        and c_base_url == self.base_url
                        and now < c_expires - TOKEN_REFRESH_MARGIN_SEC
                    ):
                        self.access_token = c_token
                        self.token_expires_at = c_expires
                        return self.access_token
                except Exception:
                    pass

            if not self.app_key or not self.app_secret:
                raise MeritzApiError(
                    rsp_cd="NO_CREDENTIALS",
                    message="MERITZ_APP_KEY / MERITZ_APP_SECRET 설정이 비어 있습니다.",
                )

            url = f"{self.base_url}/oauth2/token"
            form_data = {
                "grant_type": "client_credentials",
                "client_id": self.app_key,
                "client_secret": self.app_secret,
                "scope": "login",
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

            session = await self.get_session()
            async with session.post(url, data=form_data, headers=headers) as response:
                status = response.status
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    text = await response.text()
                    raise MeritzApiError(
                        rsp_cd="AUTH_BAD_RESPONSE",
                        message=f"토큰 응답 파싱 실패 (HTTP {status}): {text[:200]}",
                        status=status,
                    )

                if status != 200 or not isinstance(data, dict) or "access_token" not in data:
                    gw_code = (
                        data.get("rsp_cd") or data.get("error") or data.get("error_code") or "AUTH_FAILED"
                        if isinstance(data, dict)
                        else "AUTH_FAILED"
                    )
                    gw_msg = (
                        format_meritz_message(data) or str(data.get("error_description") or "")
                        if isinstance(data, dict)
                        else "토큰 발급 실패"
                    )
                    raise MeritzApiError(
                        rsp_cd=str(gw_code),
                        message=f"메리츠 토큰 발급 실패 (HTTP {status}): {gw_msg}",
                        raw=data if isinstance(data, dict) else {},
                        status=status,
                    )

                self.access_token = data["access_token"]
                expires_in = int(data.get("expires_in", 43200))
                self.token_expires_at = now + expires_in
                try:
                    token_file.parent.mkdir(parents=True, exist_ok=True)
                    token_file.write_text(
                        json.dumps(
                            {
                                "access_token": self.access_token,
                                "expires_at": self.token_expires_at,
                                "app_key": self.app_key,
                                "base_url": self.base_url,
                            }
                        ),
                        encoding="utf-8",
                    )
                except Exception:
                    pass
                logger.info("메리츠 Open API 접근 토큰 발급 완료")
                return self.access_token

    async def request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """모든 메리츠 REST API 호출을 담당하는 공통 비동기 메서드."""
        token = await self.issue_token()
        url = f"{self.base_url}{path}"

        req_headers = {
            "authorization": f"Bearer {token}",
            "mac_address": self._mac,
        }
        if method.upper() == "POST":
            req_headers["content-type"] = "application/json"
        if headers:
            req_headers.update(headers)

        session = await self.get_session()
        max_attempts = 1 + (RATE_LIMIT_RETRIES if method.upper() == "GET" else 0)

        for attempt in range(1, max_attempts + 1):
            await self._throttle()
            async with session.request(
                method.upper(), url, headers=req_headers, json=data, params=params
            ) as response:
                status = response.status
                try:
                    result = await response.json(content_type=None)
                except Exception:
                    raw_text = await response.text()
                    result = {"rsp_cd": f"HTTP_{status}", "rsp_msg": raw_text[:300]}

                if isinstance(result, dict):
                    result["_http_status"] = status
                    result["_resp_headers"] = dict(response.headers)
                    rsp_cd = str(result.get("rsp_cd") or "").strip()

                    # 토큰 만료/폐기 감지 시 1회 강제 재발급 후 즉시 재시도
                    if rsp_cd in TOKEN_DEAD_CODES:
                        logger.warning(f"메리츠 토큰 만료 감지({rsp_cd}) - 강제 재발급 후 재시도")
                        new_token = await self.issue_token(force_refresh=True)
                        req_headers["authorization"] = f"Bearer {new_token}"
                        await self._throttle()
                        async with session.request(
                            method.upper(), url, headers=req_headers, json=data, params=params
                        ) as retry_resp:
                            status = retry_resp.status
                            result = await retry_resp.json(content_type=None)
                            if isinstance(result, dict):
                                result["_http_status"] = status
                                result["_resp_headers"] = dict(retry_resp.headers)

            rsp_cd = str(result.get("rsp_cd") or "").strip() if isinstance(result, dict) else ""
            if rsp_cd not in RETRYABLE_CODES or attempt == max_attempts:
                return ensure_ok(result, status=status)

            logger.warning(
                f"메리츠 초당 호출 한도 초과({rsp_cd}) - {attempt}/{max_attempts}회 백오프 대기 후 재시도"
            )
            await asyncio.sleep(RATE_LIMIT_BACKOFF_SEC * attempt)

        return ensure_ok(result, status=status)

    async def _throttle(self) -> None:
        async with self._rate_lock:
            wait = self._next_request_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request_at = time.monotonic() + MIN_REQUEST_INTERVAL_SEC

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("Meritz Client 세션 정상 종료")

