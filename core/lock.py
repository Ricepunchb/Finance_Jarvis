# core/lock.py
"""엔진 프로세스 단일 인스턴스 락.

flock은 프로세스가 죽으면(SIGKILL 포함) OS가 자동으로 fd를 닫아 락을 해제하므로,
DB에 "leader" 플래그를 두는 방식보다 크래시 상황에서 더 안전하다 (stale 락이 남지 않음).
"""
import fcntl
import os
from pathlib import Path


class EngineAlreadyRunningError(Exception):
    pass


class InstanceLock:
    def __init__(self, path: str):
        self.path = path
        self._fd = None

    def acquire(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._fd)
            self._fd = None
            raise EngineAlreadyRunningError(
                f"다른 엔진 프로세스가 이미 실행 중입니다 (lock: {self.path})."
            )
        os.write(self._fd, str(os.getpid()).encode())

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
