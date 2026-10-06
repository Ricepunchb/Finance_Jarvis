import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.profile_manager import (
    get_active_profile,
    get_all_profiles_info,
    parse_env_file,
    set_active_profile,
    switch_profile,
)


def test_parse_env_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        env_path = Path(tmpdir) / ".env.testprof"
        env_path.write_text(
            """
PROFILE_NAME="testprof"
PROFILE_DISPLAY_NAME="테스트 프로필"
API_PORT=9900
FRONT_PORT=9500
KIS_ACCOUNT_NO="12345678-01"
IS_MOCK=False
DOMESTIC_ONLY=True
            """.strip(),
            encoding="utf-8",
        )

        meta = parse_env_file(env_path)
        assert meta["name"] == "testprof"
        assert meta["display_name"] == "테스트 프로필"
        assert meta["api_port"] == 9900
        assert meta["front_port"] == 9500
        assert meta["account_no"] == "12345678-01"
        assert meta["is_mock"] is False
        assert meta["domestic_only"] is True


def test_get_all_profiles_info():
    profiles = get_all_profiles_info()
    assert isinstance(profiles, list)
    assert len(profiles) >= 3  # isa, mock, real
    names = [p["name"] for p in profiles]
    assert "isa" in names
    assert "mock" in names
    assert "real" in names

    isa_prof = next(p for p in profiles if p["name"] == "isa")
    assert isa_prof["api_port"] == 8800
    assert isa_prof["front_port"] == 8501
    assert "status_desc" in isa_prof
    assert "status_code" in isa_prof


def test_set_and_switch_profile():
    with patch("core.profile_manager.ACTIVE_PROFILE_FILE") as mock_active_file:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = Path(f.name)
        try:
            with patch("core.profile_manager.ACTIVE_PROFILE_FILE", temp_path):
                set_active_profile("mock")
                assert temp_path.read_text(encoding="utf-8").strip() == "mock"
                assert get_active_profile() == "mock"

                set_active_profile("isa")
                assert temp_path.read_text(encoding="utf-8").strip() == "isa"
                assert get_active_profile() == "isa"
        finally:
            if temp_path.exists():
                temp_path.unlink()

