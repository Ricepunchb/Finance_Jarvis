import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import meritz_overseas
from core.config import settings
from core.meritz_client import AsyncMeritzClient, MeritzApiError


def test_get_price_exchange_mapping():
    async def run():
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "rsp_cd": "0000",
            "data": {
                "ovrs_nmix_prpr": "235.50",
                "hts_kor_isnm": "애플",
                "prdy_ctrt": "1.25",
            },
        }

        # NASD -> OQ
        res_nasd = await meritz_overseas.get_price(mock_client, "NASD", "AAPL")
        assert res_nasd["last"] == "235.50"
        assert res_nasd["currency"] == "USD"
        assert res_nasd["name"] == "애플"
        call_params = mock_client.request.call_args[1]["params"]
        assert call_params["mrkt_cls_code"] == "OQ"
        assert call_params["iscd"] == "AAPL"

        # NYSE -> NY
        await meritz_overseas.get_price(mock_client, "NYSE", "IBM")
        assert mock_client.request.call_args[1]["params"]["mrkt_cls_code"] == "NY"

        # AMEX -> AX
        await meritz_overseas.get_price(mock_client, "AMEX", "SPY")
        assert mock_client.request.call_args[1]["params"]["mrkt_cls_code"] == "AX"
    asyncio.run(run())


def test_get_daily_chart():
    async def run():
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "rsp_cd": "0000",
            "data": [
                {
                    "date": "20261105",
                    "oprc": "230.0",
                    "hprc": "236.0",
                    "lprc": "229.0",
                    "prpr": "235.5",
                    "acml_vol": "50000000",
                }
            ],
        }

        rows = await meritz_overseas.get_daily_chart(mock_client, "NASD", "AAPL")
        assert len(rows) == 1
        assert rows[0]["prpr"] == "235.5"
        call_params = mock_client.request.call_args[1]["params"]
        assert call_params["mod_stpr_cls_code"] == "0"
        assert call_params["mrkt_cls_code"] == "OQ"
    asyncio.run(run())


def test_get_minute_chart():
    async def run():
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "rsp_cd": "0000",
            "data": [
                {
                    "korea_date": "20261105",
                    "korea_hour": "233000",
                    "oprc": "230.0",
                    "hprc": "232.0",
                    "lprc": "229.5",
                    "prpr": "231.0",
                    "acml_vol": "1200000",
                }
            ],
        }

        rows = await meritz_overseas.get_minute_chart(mock_client, "NASD", "AAPL", n_min="30")
        assert len(rows) == 1
        call_params = mock_client.request.call_args[1]["params"]
        assert call_params["hour_cls_code"] == "1800"  # 30분 = 1800초
    asyncio.run(run())


def test_get_holdings():
    async def run():
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "rsp_cd": "0000",
            "data": [
                {
                    "ovrs_stck_symb_iscd": "TSLA",
                    "ovrs_exch_vndr_idno": "0537",
                    "rmnd_dqty": "10",
                    "seln_able_dqty": "10",
                    "avrg_unpr": "210.50",
                    "exrt": "1420.0",
                    "crcd": "USD",
                }
            ],
        }

        holdings = await meritz_overseas.get_holdings(mock_client)
        assert len(holdings) == 1
        h = holdings[0]
        assert h["pdno"] == "TSLA"
        assert h["cblc_qty13"] == "10"
        assert h["ord_psbl_qty1"] == "10"
        assert h["avg_unpr3"] == "210.50"
        assert h["ovrs_excg_cd"] == "NASD"
    asyncio.run(run())


def test_get_present_balance_krw():
    async def run():
        mock_client = AsyncMock()

        async def fake_request(method, path, **kwargs):
            if "holdings" in path:
                return {
                    "rsp_cd": "0000",
                    "data": [{
                        "ovrs_stck_symb_iscd": "AAPL",
                        "ovrs_exch_vndr_idno": "0537",
                        "rmnd_dqty": "5",
                        "avrg_unpr": "200.0",
                        "exrt": "1400.0",
                        "crcd": "USD",
                    }],
                }
            elif "currencies" in path:
                return {
                    "rsp_cd": "0000",
                    "data": [{
                        "crcd": "USD",
                        "frcr_amt": "1000.0",
                        "entm_aset_vltn_amt": "2000.0",  # 총자산 USD
                        "vltn_exrt": "1400.0",
                    }],
                }
            return {"rsp_cd": "0000", "data": []}

        mock_client.request = fake_request
        present = await meritz_overseas.get_present_balance_krw(mock_client)
        assert len(present["holdings"]) == 1
        # 순수 USD 총자산 $2,000, KRW 환산치 2,800,000원
        assert float(present["totals"]["tot_asst_amt"]) == 2000.0
        assert float(present["totals"]["tot_asst_usd"]) == 2000.0
        assert float(present["totals"]["tot_asst_krw"]) == 2800000.0
        assert present["totals"]["bass_exrt"] == "1400.0"
        assert present["totals"]["currency"] == "USD"
    asyncio.run(run())


def test_get_buyable_cash_pure_usd():
    async def run():
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "rsp_cd": "0000",
            "data": [{
                "crcd": "USD",
                "oder_crnc_oder_able_camt": "416.19",
                "othr_crnc_evdn_camt": "0.0000",
                "exrt": "1342.0",
            }],
        }
        res = await meritz_overseas.get_buyable_cash(mock_client, "NASD", "NVDA", 200.0)
        assert res["ovrs_ord_psbl_amt"] == "416.19"
        assert res["max_ord_psbl_qty"] == "2"
        # intg_marg_iqry_yn="N" 파라미터가 반드시 전송되어야 함
        assert mock_client.request.call_args[1]["params"]["intg_marg_iqry_yn"] == "N"
    asyncio.run(run())


def test_order_auto_fx_prevention_guard():
    async def run():
        mock_client = AsyncMock()
        with patch.object(settings, "MAX_ORDER_NOTIONAL_USD", 5000.0):
            # 가용 달러 $100 인데 2주 @ $60 = $120 매수 시도 -> 차단
            with pytest.raises(MeritzApiError) as exc_info:
                await meritz_overseas.order(
                    mock_client,
                    ovrs_excg_cd="NASD",
                    symbol="NVDA",
                    side="buy",
                    qty=2,
                    price=60.0,
                    shadow=False,
                    available_usd=100.0,
                )
            assert exc_info.value.rsp_cd == "NO_USD_CASH_AUTO_FX_PREVENTED"
            assert "원화 자동환전 방지" in exc_info.value.rsp_msg
            assert mock_client.request.call_count == 0  # 브로커에 전송되지 않음
    asyncio.run(run())


def test_order_shadow_mode():
    async def run():
        mock_client = AsyncMock()

        # shadow=True 시 실제 API 호출 없이 가상 주문번호 반환
        res = await meritz_overseas.order(
            mock_client,
            ovrs_excg_cd="NASD",
            symbol="NVDA",
            side="buy",
            qty=2,
            price=120.0,
            shadow=True,
        )
        assert res["shadow"] is True
        assert res["odno"].startswith("SHADOW_")
        assert not mock_client.request.called
    asyncio.run(run())


def test_order_max_notional_cap():
    async def run():
        mock_client = AsyncMock()

        with patch.object(settings, "MAX_ORDER_NOTIONAL_USD", 500.0):
            # 5주 @ $120 = $600 > $500 한도 초과
            with pytest.raises(MeritzApiError) as exc_info:
                await meritz_overseas.order(
                    mock_client,
                    ovrs_excg_cd="NASD",
                    symbol="NVDA",
                    side="buy",
                    qty=5,
                    price=120.0,
                    shadow=False,
                )
            assert exc_info.value.rsp_cd == "MAX_NOTIONAL_EXCEEDED"
            assert "1회 해외 주문 한도" in exc_info.value.rsp_msg
    asyncio.run(run())


def test_order_warning_auto_retry():
    async def run():
        mock_client = AsyncMock()

        # 첫 응답: warn_cls_code="1" (경고)
        resp_warn = {
            "rsp_cd": "0001",
            "warn_cls_code": "1",
            "rsp_msg": "가격 경고가 발생했습니다.",
            "data": {},
        }
        # 재전송 응답: warn_cls_code="0", 정상 접수
        resp_ok = {
            "rsp_cd": "0000",
            "warn_cls_code": "0",
            "rsp_msg": "주문 정상 접수",
            "data": {"odno": "12345678"},
        }
        mock_client.request.side_effect = [resp_warn, resp_ok]

        with patch.object(settings, "MAX_ORDER_NOTIONAL_USD", 1000.0):
            res = await meritz_overseas.order(
                mock_client,
                ovrs_excg_cd="NASD",
                symbol="AAPL",
                side="buy",
                qty=1,
                price=200.0,
                shadow=False,
                available_usd=1000.0,
            )
            assert res["odno"] == "12345678"
            assert mock_client.request.call_count == 2
            # 두 번째 호출에서는 warn_cnfr_yn="Y"가 포함되어야 함
            second_call_data = mock_client.request.call_args[1]["data"]
            assert second_call_data["warn_cnfr_yn"] == "Y"
    asyncio.run(run())


def test_get_fx_rate():
    async def run():
        mock_client = AsyncMock()
        mock_client.request.return_value = {
            "rsp_cd": "0000",
            "data": [
                {"crcd": "USD", "stnd_exrt": "1415.50"},
                {"crcd": "JPY", "stnd_exrt": "9.25"},
            ],
        }

        rate = await meritz_overseas.get_fx_rate(mock_client, "USD")
        assert rate == 1415.50
    asyncio.run(run())
