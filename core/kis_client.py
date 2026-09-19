# core/kis_client.py
import aiohttp
import asyncio
import time
from typing import Dict, Any, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import settings

# 발급된 토큰을 만료 임박(1일 유효기간) 전까지 재사용하기 위한 여유 시간.
# 너무 늦게 갱신하면 만료된 토큰으로 요청을 보내다 401을 받을 수 있어 여유를 둔다.
TOKEN_REFRESH_MARGIN_SEC = 60 * 10

# KIS 초당 호출 제한: 실전투자는 넉넉하지만 모의투자는 매우 낮아(초당 2건 수준),
# 그대로 부딪히면 "EGW00201 초당 거래건수를 초과하였습니다" 오류가 난다. 여유를 두고 낮게 잡는다.
MAX_REQUESTS_PER_SECOND = 15 if not settings.IS_MOCK else 1


class AsyncKISClient:
    def __init__(self):
        self.domain = settings.kis_domain
        self.app_key = settings.KIS_APP_KEY
        self.app_secret = settings.KIS_APP_SECRET
        self.session: Optional[aiohttp.ClientSession] = None
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0.0  # time.monotonic() 기준 만료 시각
        self._token_lock = asyncio.Lock()
        self._rate_limiter = asyncio.Semaphore(MAX_REQUESTS_PER_SECOND)

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
            if self.access_token and time.monotonic() < self.token_expires_at:
                return self.access_token

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
                    self.token_expires_at = time.monotonic() + expires_in - TOKEN_REFRESH_MARGIN_SEC
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
        # 초당 호출 제한: 세마포어를 획득한 뒤 1초가 지나서야 반납해 초당 호출 수를 제한한다.
        await self._rate_limiter.acquire()
        asyncio.get_running_loop().call_later(1.0, self._rate_limiter.release)
        async with session.request(method, url, headers=headers, json=data, params=params) as response:
            return await response.json()

    async def close(self):
        """앱 종료 시 세션을 안전하게 닫습니다."""
        if self.session and not self.session.closed:
            await self.session.close()
            print("💤 KIS Client 세션 정상 종료")