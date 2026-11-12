# tests/conftest.py
"""글로벌 테스트 설정 및 실제 계좌 오더 방지 안전 가드."""
import pytest
from core.config import settings


@pytest.fixture(autouse=True)
def safe_test_environment(monkeypatch):
    """모든 pytest 테스트 실행 시 실계좌 주문 전송을 원천 차단하기 위해 SHADOW_MODE=True를 기본값으로 강제한다."""
    monkeypatch.setattr(settings, "SHADOW_MODE", True)
