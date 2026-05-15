# core/kis_client.py
import aiohttp
import asyncio
from typing import Dict, Any, Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .config import settings


class AsyncKISClient:
    def __init__(self):
        self.domain = settings.kis_domain
        self.app_key = settings.KIS_APP_KEY
        self.app_secret = settings.KIS_APP_SECRET
        self.session: Optional[aiohttp.ClientSession] = None
        self.access_token: Optional[str] = None

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
        """OAuth 접근 토큰을 발급받습니다."""
        if self.access_token:
            return self.access_token # 이미 토큰이 있다면 재사용 (속도 최적화)

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
                print(f"✅ KIS API 토큰 발급 성공")
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
        async with session.request(method, url, headers=headers, json=data, params=params) as response:
            return await response.json()

    async def close(self):
        """앱 종료 시 세션을 안전하게 닫습니다."""
        if self.session and not self.session.closed:
            await self.session.close()
            print("💤 KIS Client 세션 정상 종료")