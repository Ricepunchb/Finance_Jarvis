# main.py
import asyncio
from core.kis_client import AsyncKISClient


async def main():
    print(" J.A.R.V.I.S. 시스템 초기화 중...")
    
    # 1. 클라이언트 인스턴스 생성
    kis = AsyncKISClient()

    try:
        # 2. 삼성전자(005930) 현재가 조회 API 제원 설정
        path = "/uapi/domestic-stock/v1/quotations/inquire-price"
        tr_id = "FHKST01010100" # 주식현재가 시세조회 TR_ID
        params = {
            "FID_COND_MRKT_DIV_CODE": "J", # 시장 분류 코드 (J: 주식, ETF, ETN)
            "FID_INPUT_ISCD": "005930"     # 종목코드 (삼성전자)
        }

        print("KIS API 연결 및 OAuth 토큰 발급/시세 요청 중...")
        
        # 3. 비동기 요청 전송
        response = await kis.request(
            method="GET",
            path=path,
            tr_id=tr_id,
            params=params
        )

        # 4. 응답 데이터 파싱 및 출력
        if response.get("rt_cd") == "0": # 0: 성공
            output = response.get("output", {})
            current_price = output.get("stck_prpr") # 주식 현재가
            change_rate = output.get("prdy_ctrt")   # 전일 대비 등락률
            volume = output.get("acml_vol")         # 누적 거래량

            print("\n✅ [테스트 성공] 데이터 수신 완료!")
            print(f"▶ 종목명: 삼성전자 (005930)")
            print(f"▶ 현재가: {int(current_price):,}원")
            print(f"▶ 등락률: {change_rate}%")
            print(f"▶ 거래량: {int(volume):,}주")
        else:
            print(f"\n❌ [API 에러] {response.get('msg1')}")

    except Exception as e:
        print(f"\n🚨 [시스템 에러] 통신 중 문제가 발생했습니다: {e}")

    finally:
        # 5. 세션 안전하게 종료
        await kis.close()

if __name__ == "__main__":
    asyncio.run(main())