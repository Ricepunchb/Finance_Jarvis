# core/kis_common.py
"""국내/해외 KIS API 래퍼가 공유하는 공통 에러 처리.

조용히 실패를 삼키는 건 자동매매에서 가장 위험한 패턴이므로, rt_cd 실패를 항상
명시적인 예외로 드러낸다.
"""
from typing import Any, Dict


class KisApiError(Exception):
    """KIS API가 rt_cd != '0'(실패)를 반환했을 때 발생."""

    def __init__(self, msg_cd: str, msg1: str, raw: Dict[str, Any]):
        self.msg_cd = msg_cd
        self.msg1 = msg1
        self.raw = raw
        super().__init__(f"KIS API 오류 [{msg_cd}]: {msg1}")


def ensure_ok(response: Dict[str, Any]) -> Dict[str, Any]:
    if response.get("rt_cd") != "0":
        raise KisApiError(
            msg_cd=response.get("msg_cd", ""),
            msg1=response.get("msg1", "unknown error"),
            raw=response,
        )
    return response
