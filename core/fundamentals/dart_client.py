# core/fundamentals/dart_client.py
"""DART(전자공시시스템) OpenAPI 연동 — Phase 2, 아직 미구현.

무료 API 키 발급이 별도로 필요해(opendart.fss.or.kr) 이번 구현 범위에서는 제외했다.
core.fundamentals.valuation은 KIS financial-ratio + 네이버 컨센서스만으로 동작하며
이 모듈에 의존하지 않는다. DART를 붙일 때는 종목코드(6자리) -> corp_code 매핑
(corpCode.xml, 월 1회 정도 갱신)을 먼저 구축해야 한다.
"""
raise NotImplementedError("DART 연동은 Phase 2 — DART_API_KEY 발급 후 구현 예정")
