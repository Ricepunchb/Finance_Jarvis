#!/usr/bin/env python3
"""Finance Jarvis 멀티 계좌 프로필 관리 CLI.

사용 예시:
  uv run python scripts/manage_profile.py list
  uv run python scripts/manage_profile.py switch isa
  uv run python scripts/manage_profile.py start isa
  uv run python scripts/manage_profile.py stop isa
  uv run python scripts/manage_profile.py start-all
  uv run python scripts/manage_profile.py stop-all
  uv run python scripts/manage_profile.py restart isa
  uv run python scripts/manage_profile.py master
  uv run python scripts/manage_profile.py status
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.profile_manager import (
    get_active_profile,
    get_all_profiles_info,
    restart_profile,
    start_master_portal,
    start_profile,
    stop_profile,
    switch_profile,
)


def list_profiles():
    profiles = get_all_profiles_info()
    active = get_active_profile()

    print("\n=========================================================================================")
    print("📋 [Finance Jarvis 멀티 계좌 프로필 목록]")
    print("=========================================================================================")
    header = f"{'선택':<4} | {'프로필 ID':<10} | {'계좌 구분 / 설명':<26} | {'계좌번호':<15} | {'API/UI 포트':<14} | {'상태(tmux)'}"
    print(header)
    print("-" * 105)

    for item in profiles:
        active_mark = "👉 *" if item["is_active"] else "   "
        type_badge = "[모의]" if item["is_mock"] else "[실전]"
        display_full = f"{type_badge} {item['display_name']}"
        ports_str = f":{item['api_port']} / :{item['front_port']}"
        print(f"{active_mark:<4} | {item['name']:<10} | {display_full:<24} | {item['account_no']:<15} | {ports_str:<14} | {item['status_desc']}")

    print("-" * 105)
    print(f"👉 현재 기본 활성 프로필: [{active}]")
    print("   · 프로필 전환: uv run python scripts/manage_profile.py switch <이름>")
    print("   · 단독 기동:   uv run python scripts/manage_profile.py start <이름>")
    print("   · 세션 중지:   uv run python scripts/manage_profile.py stop <이름>")
    print("   · 전체 기동:   uv run python scripts/manage_profile.py start-all")
    print("   · 전체 중지:   uv run python scripts/manage_profile.py stop-all")
    print("   · 마스터 UI:   uv run python scripts/manage_profile.py master (포트 :8500)\n")


def do_switch(name: str):
    ok, msg = switch_profile(name)
    if ok:
        print(f"\n✅ {msg}")
        print(f"· 연결 DB: data/jarvis_{name}.db")
        print(f"\n💡 이 프로필로 서비스를 시작하려면:")
        print(f"   uv run python scripts/manage_profile.py start {name}\n")
    else:
        print(f"❌ {msg}")
        sys.exit(1)


def do_start(name: str):
    ok, msg = start_profile(name)
    if ok:
        print(f"\n🚀 [프로필 기동]\n{msg}\n")
    else:
        print(f"\n❌ [기동 실패]\n{msg}\n")
        sys.exit(1)


def do_stop(name: str):
    ok, msg = stop_profile(name)
    if ok:
        print(f"\n🛑 [프로필 중지]\n{msg}\n")
    else:
        print(f"\nℹ️ {msg}\n")


def do_restart(name: str):
    print(f"\n🔄 프로필 [{name}] 재기동 중...")
    ok, msg = restart_profile(name)
    if ok:
        print(f"✅ 재기동 완료:\n{msg}\n")
    else:
        print(f"❌ 재기동 실패:\n{msg}\n")
        sys.exit(1)


def do_start_all():
    profiles = get_all_profiles_info()
    print(f"\n🚀 전체 프로필({len(profiles)}개) 일괄 기동 시작...")
    for p in profiles:
        print(f"-> [{p['name']}] 기동 중...")
        ok, msg = start_profile(p["name"])
        print(f"   결과: {'성공' if ok else '실패'} - {msg}")
    print("\n🎉 전체 기동 작업 완료!")


def do_stop_all():
    profiles = get_all_profiles_info()
    print(f"\n🛑 전체 프로필({len(profiles)}개) 일괄 정지 시작...")
    for p in profiles:
        print(f"-> [{p['name']}] 정지 중...")
        ok, msg = stop_profile(p["name"])
        print(f"   결과: {msg}")
    print("\n✅ 전체 정지 작업 완료!")


def do_master():
    ok, msg = start_master_portal()
    print(f"\n🏢 {msg}\n")


def main():
    parser = argparse.ArgumentParser(description="Finance Jarvis 멀티 계좌 프로필 관리")
    subparsers = parser.add_subparsers(dest="action", help="명령어")

    subparsers.add_parser("list", help="프로필 목록 및 실행 상태 조회")
    subparsers.add_parser("status", help="프로필 가동 현황 요약")

    p_switch = subparsers.add_parser("switch", help="기본 활성 프로필 변경")
    p_switch.add_argument("name", help="프로필 이름 (예: isa, real, mock)")

    p_start = subparsers.add_parser("start", help="지정 프로필 tmux 세션 기동")
    p_start.add_argument("name", help="프로필 이름 (예: isa, real, mock)")

    p_stop = subparsers.add_parser("stop", help="지정 프로필 tmux 세션 중지")
    p_stop.add_argument("name", help="프로필 이름 (예: isa, real, mock)")

    p_restart = subparsers.add_parser("restart", help="지정 프로필 tmux 세션 재기동")
    p_restart.add_argument("name", help="프로필 이름 (예: isa, real, mock)")

    subparsers.add_parser("start-all", help="모든 프로필 일괄 기동")
    subparsers.add_parser("stop-all", help="모든 프로필 일괄 정지")
    subparsers.add_parser("master", help="전용 마스터 웹 관제 포털(:8500) 기동")

    args = parser.parse_args()

    if not args.action or args.action in ("list", "status"):
        list_profiles()
    elif args.action == "switch":
        do_switch(args.name)
    elif args.action == "start":
        do_start(args.name)
    elif args.action == "stop":
        do_stop(args.name)
    elif args.action == "restart":
        do_restart(args.name)
    elif args.action == "start-all":
        do_start_all()
    elif args.action == "stop-all":
        do_stop_all()
    elif args.action == "master":
        do_master()


if __name__ == "__main__":
    main()
