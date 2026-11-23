# tests/test_intel_daemon.py
import asyncio
import sqlite3
import time

from core.intel.daemon import IntelMasterDaemon, discover_union_symbols
from core.us_market.surprise import NewsArticle, SurpriseAnalysisResult


def test_discover_union_symbols_from_multiple_profile_dbs(tmp_path):
    """여러 계좌 프로필 DB에서 US/KR 종목 합집합을 중복 없이 수집하는지 검증."""
    db1 = tmp_path / "jarvis_isa.db"
    c1 = sqlite3.connect(db1)
    c1.execute("CREATE TABLE target_weights (symbol TEXT, market TEXT, active INTEGER, target_weight REAL)")
    c1.execute("INSERT INTO target_weights VALUES ('005930', 'domestic', 1, 0.3)")
    c1.execute("INSERT INTO target_weights VALUES ('000660', 'domestic', 1, 0.2)")
    c1.commit()
    c1.close()

    db2 = tmp_path / "jarvis_meritz.db"
    c2 = sqlite3.connect(db2)
    c2.execute("CREATE TABLE us_watchlist (symbol TEXT, alert_enabled INTEGER)")
    c2.execute("INSERT INTO us_watchlist VALUES ('AVGO', 1)")
    c2.execute("INSERT INTO us_watchlist VALUES ('MSFT', 0)")
    c2.commit()
    c2.close()

    us_syms, kr_syms = discover_union_symbols(data_dir=tmp_path)
    assert "AVGO" in us_syms
    assert "NVDA" in us_syms  # core symbol
    assert "MSFT" not in us_syms  # alert_enabled=0
    assert kr_syms == ["000660", "005930"]


def test_intel_daemon_us_cycle_caches_low_score_and_dedupes(tmp_path, monkeypatch):
    """마스터 데몬 US 사이클이 75점 미만 뉴스도 캐싱해 재실행 시 재분석 0회인지 검증."""
    async def _run():
        now = time.time()
        fake_articles = [
            NewsArticle(
                symbol="NVDA",
                title="Nvidia shares edge slightly higher in quiet trading",
                summary="Quiet session",
                url="https://example.com/nvda-1",
                source="Reuters",
                published_at=now,
            )
        ]

        analyze_calls = 0

        async def _fake_analyze(cluster, llm_provider=None, current_price_change_pct=None, summary_threshold=75):
            nonlocal analyze_calls
            analyze_calls += 1
            return SurpriseAnalysisResult(
                surprise_score=30,  # 75점 미만
                impact_type="중립",
                affected_sector_theme="반도체",
                summary_sentences=["일상 시황"],
                rationale="일상 시황",
            )

        async def _fake_quote(sym):
            return {"price": 140.0, "change_pct": 0.4}

        monkeypatch.setattr("core.us_market.surprise.fetch_latest_us_news", lambda sym, limit=8: fake_articles)
        monkeypatch.setattr("core.us_market.surprise.analyze_news_surprise", _fake_analyze)
        monkeypatch.setattr("core.us_market.client.get_us_quote", _fake_quote)

        daemon = IntelMasterDaemon(
            intel_db_path=str(tmp_path / "jarvis_intel.db"),
            lock_path=str(tmp_path / "intel.lock"),
        )

        # 1차 스캔: 1건 신규 분석, 75점 미만이므로 알림 0건, 공유 DB에 저장
        r1 = await daemon.run_us_news_cycle(symbols=["NVDA"], threshold=75, send_notifications=False)
        assert r1["analyzed_new"] == 1
        assert r1["alerted"] == 0
        assert analyze_calls == 1

        # 2차 스캔 (30분 뒤): 동일 뉴스 -> exact_alias 스킵, 분석 0회!
        r2 = await daemon.run_us_news_cycle(symbols=["NVDA"], threshold=75, send_notifications=False)
        assert r2["analyzed_new"] == 0
        assert r2["skipped_exact"] == 1
        assert analyze_calls == 1

    asyncio.run(_run())
