"""공인 IP 조회와 변동 감지.

국가법령정보 OPEN API는 도메인이 아니라 **등록된 IP 주소**로 사용자를 검증합니다.
가정용 인터넷처럼 공인 IP가 유동이면, 어느 날 갑자기 재부팅·회선 재접속만으로도
API가 '사용자 정보 검증에 실패하였습니다'를 반환하며 수집이 멈춥니다.
그래서 IP를 기억해 두었다가 바뀌면 먼저 알려 줍니다.
"""

from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

# 한 곳이 죽어도 되도록 여러 곳을 순서대로 시도합니다.
IP_PROVIDERS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
    "https://checkip.amazonaws.com",
)

# 같은 IP를 반복 조회하지 않도록 캐시합니다 (초).
CACHE_SECONDS = 300

_cache: tuple[float, str | None] = (0.0, None)


def _looks_like_ip(text: str) -> bool:
    parts = text.split(".")
    if len(parts) != 4:
        return ":" in text  # IPv6는 형식 검사를 생략
    return all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def get_public_ip(timeout: int = 8, force: bool = False) -> str | None:
    """이 서버의 공인 IP. 실패하면 None."""
    global _cache
    cached_at, cached_ip = _cache
    if not force and cached_ip and (time.monotonic() - cached_at) < CACHE_SECONDS:
        return cached_ip

    for url in IP_PROVIDERS:
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            ip = resp.text.strip()
            if ip and _looks_like_ip(ip):
                _cache = (time.monotonic(), ip)
                return ip
        except requests.RequestException as exc:
            log.debug("IP 조회 실패 (%s): %s", url, exc)
    log.warning("공인 IP를 확인하지 못했습니다.")
    return None


def reset_cache() -> None:
    global _cache
    _cache = (0.0, None)


def registration_hint(current_ip: str | None, registered_ip: str | None) -> str:
    """디스코드/콘솔에 그대로 넣을 안내 문구."""
    now = current_ip or "확인 실패"
    if registered_ip and current_ip and registered_ip != current_ip:
        return (
            f"이 서버의 공인 IP가 **{registered_ip}** → **{current_ip}** 로 바뀌었습니다.\n"
            f"국가법령정보 OPEN API는 등록된 IP로만 호출을 허용하므로, "
            f"https://open.law.go.kr/LSO/openApi/cuAskList.do 에서 "
            f"**{current_ip}** 를 등록해야 수집이 재개됩니다."
        )
    return (
        f"이 서버의 현재 공인 IP는 **{now}** 입니다.\n"
        f"https://open.law.go.kr/LSO/openApi/cuAskList.do 에서 이 IP를 등록하세요."
    )
