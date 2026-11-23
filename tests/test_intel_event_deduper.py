# tests/test_intel_event_deduper.py
import asyncio
import time

from core.intel import db as intel_db
from core.intel.event_deduper import (
    EventDeduper,
    build_cluster_alias_key,
    build_url_alias_key,
)


def test_exact_alias_hit_including_sub_threshold_score(tmp_path):
    """75점 미만 뉴스도 노드로 저장되어 다음 조회 시 정확 히트(LLM 0회)되는지 검증."""
    async def _run():
        db_path = str(tmp_path / "intel.db")
        await intel_db.init_intel_db(db_path)
        deduper = EventDeduper()

        conn = await intel_db.get_intel_connection(db_path)
        try:
            ak = build_cluster_alias_key("nvda_routine_001")
            m1 = await deduper.resolve_or_bind(
                conn,
                alias_key=ak,
                symbol="NVDA",
                market="US",
                title="Nvidia shares trade flat ahead of conference",
                source="Reuters",
                published_at=time.time(),
            )
            assert not m1.matched

            # 75점 미만(35점)이지만 노드로 저장
            node_id = await deduper.register_node(
                conn,
                alias_key=ak,
                symbol="NVDA",
                market="US",
                title="Nvidia shares trade flat ahead of conference",
                source="Reuters",
                published_at=time.time(),
                surprise_score=35,
                impact_type="중립",
                alerted=False,
            )

            # 30분 뒤 재스캔 시 exact_alias로 히트 -> LLM 호출 불필요
            m2 = await deduper.resolve_or_bind(
                conn,
                alias_key=ak,
                symbol="NVDA",
                market="US",
                title="Nvidia shares trade flat ahead of conference",
                source="Reuters",
                published_at=time.time(),
            )
            assert m2.matched
            assert m2.match_type == "exact_alias"
            assert m2.node["node_id"] == node_id
            assert m2.node["surprise_score"] == 35
        finally:
            await conn.close()

    asyncio.run(_run())


def test_similar_event_different_outlet_merges_into_same_node(tmp_path):
    """다른 언론사/시간/ID로 들어온 동일 사건 뉴스가 기존 사건 노드에 Alias로 병합되는지 검증."""
    async def _run():
        db_path = str(tmp_path / "intel.db")
        await intel_db.init_intel_db(db_path)
        deduper = EventDeduper()

        conn = await intel_db.get_intel_connection(db_path)
        try:
            # 1) 영문 US 뉴스 원본 등록
            ak1 = build_cluster_alias_key("nvda_doj_reuters")
            node_id = await deduper.register_node(
                conn,
                alias_key=ak1,
                symbol="NVDA",
                market="US",
                title="DOJ launches antitrust probe into Nvidia AI chip sales",
                source="Reuters",
                published_at=time.time() - 3600,
                surprise_score=82,
                impact_type="악재",
                alerted=True,
            )

            # 2) 다른 언론사/다른 cluster_id/살짝 다른 표현으로 들어온 동일 사건
            ak2 = build_cluster_alias_key("nvda_doj_bloomberg")
            m = await deduper.resolve_or_bind(
                conn,
                alias_key=ak2,
                symbol="NVDA",
                market="US",
                title="Nvidia AI chip sales face DOJ antitrust investigation",
                source="Bloomberg",
                published_at=time.time(),
            )
            assert m.matched
            assert m.match_type == "similar_node"
            assert m.node["node_id"] == node_id

            # 3) 이후 ak2로 재조회하면 이제 O(1) exact_alias 히트
            m_again = await deduper.resolve_or_bind(
                conn,
                alias_key=ak2,
                symbol="NVDA",
                market="US",
                title="Nvidia AI chip sales face DOJ antitrust investigation",
            )
            assert m_again.matched
            assert m_again.match_type == "exact_alias"

            # 4) 한국어 KR 뉴스 유사 사건 병합 검증
            kr_ak1 = build_url_alias_key("https://news.naver.com/a1")
            kr_node = await deduper.register_node(
                conn,
                alias_key=kr_ak1,
                symbol="005930",
                market="KR",
                title="삼성전자 3분기 영업이익 어닝 쇼크 전망에 급락",
                source="한경",
                published_at=time.time() - 1800,
                sentiment_direction="SELL",
                sentiment_score=-0.8,
            )
            kr_ak2 = build_url_alias_key("https://news.naver.com/a2")
            kr_m = await deduper.resolve_or_bind(
                conn,
                alias_key=kr_ak2,
                symbol="005930",
                market="KR",
                title="삼성전자 3분기 영업이익 어닝 쇼크 우려로 폭락",
                source="매경",
                published_at=time.time(),
            )
            assert kr_m.matched
            assert kr_m.match_type == "similar_node"
            assert kr_m.node["node_id"] == kr_node

            # 5) 전혀 다른 사건은 병합되지 않아야 함
            unrelated = await deduper.resolve_or_bind(
                conn,
                alias_key=build_cluster_alias_key("nvda_dividend"),
                symbol="NVDA",
                market="US",
                title="Nvidia announces quarterly cash dividend payment schedule",
            )
            assert not unrelated.matched
        finally:
            await conn.close()

    asyncio.run(_run())
