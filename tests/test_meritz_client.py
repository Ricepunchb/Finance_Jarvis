import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.meritz_client import AsyncMeritzClient, MeritzApiError, mac_address


def test_mac_address_format():
    mac = mac_address()
    assert len(mac) == 12
    assert all(c in "0123456789ABCDEF" for c in mac)


def test_meritz_token_issue_and_caching(monkeypatch):
    async def run():
        with tempfile.TemporaryDirectory() as tmpdir:
            token_file = Path(tmpdir) / "meritz_token_test.json"

            client = AsyncMeritzClient(
                app_key="TEST_KEY",
                app_secret="TEST_SECRET",
                base_url="https://openapi.imeritz.com",
            )
            monkeypatch.setattr(AsyncMeritzClient, "token_cache_file", property(lambda self: token_file))

            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.json = AsyncMock(return_value={
                "access_token": "MOCK_ACCESS_TOKEN_12345",
                "token_type": "Bearer",
                "expires_in": 86400,
            })
            mock_post = MagicMock()
            mock_post.return_value.__aenter__.return_value = mock_resp

            mock_session = MagicMock()
            mock_session.post = mock_post
            mock_session.closed = False
            mock_session.close = AsyncMock()
            client.session = mock_session

            token = await client.issue_token()
            assert token == "MOCK_ACCESS_TOKEN_12345"
            assert token_file.exists()

            # 두 번째 호출은 캐시를 사용하여 post가 호출되지 않아야 함
            mock_post.reset_mock()
            token2 = await client.issue_token()
            assert token2 == "MOCK_ACCESS_TOKEN_12345"
            assert not mock_post.called

            await client.close()
    asyncio.run(run())


def test_meritz_request_headers_and_success():
    async def run():
        client = AsyncMeritzClient(
            app_key="TEST_KEY",
            app_secret="TEST_SECRET",
            base_url="https://openapi.imeritz.com",
        )
        client.access_token = "CACHED_TOKEN"
        client.token_expires_at = time.time() + 3600

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={
            "rsp_cd": "0000",
            "rsp_msg": "정상처리",
            "data": [{"ovrs_stck_symb_iscd": "AAPL"}],
        })

        mock_session = MagicMock()
        mock_session.request = MagicMock()
        mock_session.request.return_value.__aenter__.return_value = mock_resp
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client.session = mock_session

        data = await client.request("GET", "/accounts/v1/me/overseas/holdings")
        assert data["rsp_cd"] == "0000"
        assert data["data"][0]["ovrs_stck_symb_iscd"] == "AAPL"

        # 전송된 헤더 확인
        call_kwargs = mock_session.request.call_args[1]
        headers = call_kwargs["headers"]
        assert headers["authorization"] == "Bearer CACHED_TOKEN"
        assert len(headers["mac_address"]) == 12

        await client.close()
    asyncio.run(run())


def test_meritz_token_auto_refresh_on_expiration():
    async def run():
        client = AsyncMeritzClient(
            app_key="TEST_KEY",
            app_secret="TEST_SECRET",
            base_url="https://openapi.imeritz.com",
        )
        client.access_token = "EXPIRED_TOKEN"
        client.token_expires_at = time.time() + 3600

        resp_expired = MagicMock()
        resp_expired.status = 500
        resp_expired.json = AsyncMock(return_value={
            "rsp_cd": "EGW00121",
            "rsp_msg": "Access Token이 유효하지 않습니다.",
        })

        resp_token = MagicMock()
        resp_token.status = 200
        resp_token.json = AsyncMock(return_value={
            "access_token": "NEW_TOKEN_999",
            "expires_in": 86400,
        })

        resp_success = MagicMock()
        resp_success.status = 200
        resp_success.json = AsyncMock(return_value={
            "rsp_cd": "0000",
            "rsp_msg": "성공",
            "data": {"price": "150.0"},
        })

        mock_session = MagicMock()
        mock_session.request = MagicMock()
        mock_session.request.return_value.__aenter__.side_effect = [resp_expired, resp_success]
        mock_session.post = MagicMock()
        mock_session.post.return_value.__aenter__.return_value = resp_token
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client.session = mock_session

        data = await client.request("GET", "/market/v1/overseas/prices")
        assert data["rsp_cd"] == "0000"
        assert client.access_token == "NEW_TOKEN_999"

        await client.close()
    asyncio.run(run())


def test_meritz_api_error_raised():
    async def run():
        client = AsyncMeritzClient(
            app_key="TEST_KEY",
            app_secret="TEST_SECRET",
            base_url="https://openapi.imeritz.com",
        )
        client.access_token = "VALID_TOKEN"
        client.token_expires_at = time.time() + 3600

        mock_resp = MagicMock()
        mock_resp.status = 500
        mock_resp.json = AsyncMock(return_value={
            "rsp_cd": "IGW50001",
            "rsp_msg": "요청 파라미터 유효성 검증 실패",
        })

        mock_session = MagicMock()
        mock_session.request = MagicMock()
        mock_session.request.return_value.__aenter__.return_value = mock_resp
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client.session = mock_session

        with pytest.raises(MeritzApiError) as exc_info:
            await client.request("GET", "/market/v1/overseas/prices")

        assert exc_info.value.rsp_cd == "IGW50001"
        assert "요청 파라미터 유효성 검증 실패" in exc_info.value.rsp_msg

        await client.close()
    asyncio.run(run())
