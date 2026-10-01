# core/kis_client.py
import aiohttp
import asyncio
import json
from pathlib import Path
import time
from typing import Dict, Any, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import settings

ROOT_DIR = Path(__file__).resolve().parent.parent
TOKEN_CACHE_FILE = ROOT_DIR / "data" / "kis_token.json"

# 발급된 토큰을 만료 임박(1일 유효기간) 전까지 재사용하기 위한 여유 시간.
# 너무 늦게 갱신하면 만료된 토큰으로 요청을 보내다 401을 받을 수 있어 여유를 둔다.
TOKEN_REFRESH_MARGIN_SEC = 60 * 10

# KIS 초당 호출 제한: 실전투자는 넉넉하지만 모의투자는 매우 낮아(초당 2건 수준),
# 그대로 부딪히면 "EGW00201 초당 거래건수를 초과하였습니다" 오류가 난다. 여유를 두고 낮게 잡는다.
MAX_REQUESTS_PER_SECOND = 15 if not settings.IS_MOCK else 1

# 호출 시작 사이의 최소 간격. 모의투자는 서버 한도(약 2건/초)에 비해 1건/초도 지터(요청 지연 편차)로
# 같은 초에 두 건이 도착해 넘칠 수 있어 1.0초에 여유(20%)를 더한다.
MIN_REQUEST_INTERVAL_SEC = (1.0 / MAX_REQUESTS_PER_SECOND) * (1.2 if settings.IS_MOCK else 1.0)

# 초당 거래건수 초과(EGW00201)는 서버가 요청을 처리하지 않고 거절한 것이라, 조회(GET)는 잠시 뒤 재시도해도 안전하다.
RATE_LIMIT_MSG_CD = "EGW00201"
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_SEC = 1.5


class AsyncKISClient:
    def __init__(self):
        self.domain = settings.kis_domain
        self.app_key = settings.KIS_APP_KEY
        self.app_secret = settings.KIS_APP_SECRET
        self.session: Optional[aiohttp.ClientSession] = None
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0.0  # time.monotonic() 기준 만료 시각
        self._token_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._next_request_at: float = 0.0  # time.monotonic() 기준, 다음 호출이 나갈 수 있는 가장 이른 시각

    async def get_session(self) -> aiohttp.ClientSession:
        """aiohttp 세션을 생성하거나 반환합니다 (Connection Pooling)"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json"}
            )
        return self.session

    # 일시적인 네트워크 오류 발생 시 자동으로 1초, 2초, 4초 대기하며 최대 3번 재시도합니다.
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError))
    )
    async def issue_token(self) -> str:
        """OAuth 접근 토큰을 발급받습니다. 만료 임박 전까지는 캐시된 토큰을 재사용합니다.

        KIS는 토큰을 새로 발급할 때마다 카카오톡 알림을 보내므로, 만료되지 않은
        토큰을 불필요하게 재발급받지 않도록 만료 시각을 직접 추적한다.
        """
        async with self._token_lock:  # 동시 요청들이 각자 재발급을 시도하지 않도록 직렬화
            now = time.time()
            if self.access_token and now < self.token_expires_at - TOKEN_REFRESH_MARGIN_SEC:
                return self.access_token

            # 프로세스 간 공유 디스크 캐시 확인
            if TOKEN_CACHE_FILE.exists():
                try:
                    with open(TOKEN_CACHE_FILE, "r", encoding="utf-8") as f:
                        cached_data = json.load(f)
                    c_token = cached_data.get("access_token")
                    c_expires = float(cached_data.get("expires_at", 0.0))
                    if c_token and now < c_expires - TOKEN_REFRESH_MARGIN_SEC:
                        self.access_token = c_token
                        self.token_expires_at = c_expires
                        return self.access_token
                except Exception:
                    pass

            url = f"{self.domain}/oauth2/tokenP"
            payload = {
                "grant_type": "client_credentials",
                "appkey": self.app_key,
                "appsecret": self.app_secret
            }

            session = await self.get_session()
            async with session.post(url, json=payload) as response:
                data = await response.json()
                if response.status == 200:
                    self.access_token = data.get("access_token")
                    expires_in = int(data.get("expires_in", 86400))
                    self.token_expires_at = now + expires_in
                    try:
                        TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                        with open(TOKEN_CACHE_FILE, "w", encoding="utf-8") as f:
                            json.dump(
                                {"access_token": self.access_token, "expires_at": self.token_expires_at},
                                f,
                            )
                    except Exception:
                        pass
                    print("✅ KIS API 토큰 발급 성공")
                    return self.access_token
                else:
                    raise Exception(f"토큰 발급 실패: {data}")

    async def get_hashkey(self, payload: Dict[str, Any]) -> str:
        """주문(POST) 시 반드시 필요한 보안 해시키를 발급합니다."""
        url = f"{self.domain}/uapi/hashkey"
        headers = {
            "appkey": self.app_key,
            "appsecret": self.app_secret
        }
        
        session = await self.get_session()
        async with session.post(url, headers=headers, json=payload) as response:
            data = await response.json()
            return data.get("HASH")

    async def request(self, method: str, path: str, tr_id: str, data: Dict = None, params: Dict = None) -> Dict:
        """모든 KIS API 호출을 담당하는 공통 비동기 메서드"""
        token = await self.issue_token()
        url = f"{self.domain}{path}"

        headers = {
            "authorization": f"Bearer {token}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P", # 개인
        }

        # POST 요청(주문 등)일 경우 Hashkey 추가
        if method.upper() == "POST" and data is not None:
            headers["hashkey"] = await self.get_hashkey(data)

        session = await self.get_session()
        # 한도 초과 거절은 조회(GET)만 재시도한다 - 주문(POST)은 재전송이 중복 주문이 될 수 있어 호출자에게 그대로 돌려준다.
        max_attempts = 1 + (RATE_LIMIT_RETRIES if method.upper() == "GET" else 0)
        for attempt in range(1, max_attempts + 1):
            await self._throttle()
            async with session.request(method, url, headers=headers, json=data, params=params) as response:
                result = await response.json()
            if result.get("msg_cd") != RATE_LIMIT_MSG_CD or attempt == max_attempts:
                return result
            await asyncio.sleep(RATE_LIMIT_BACKOFF_SEC * attempt)

    async def _throttle(self) -> None:
        """직전 호출과의 최소 간격을 보장한다. 락 안에서 기다리므로 호출 순서가 유지된다."""
        async with self._rate_lock:
            wait = self._next_request_at - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_request_at = time.monotonic() + MIN_REQUEST_INTERVAL_SEC

    async def close(self):
        """앱 종료 시 세션을 안전하게 닫습니다."""
        if self.session and not self.session.closed:
            await self.session.close()
            print("💤 KIS Client 세션 정상 종료")