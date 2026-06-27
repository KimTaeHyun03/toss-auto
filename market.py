"""장 상태(개장 여부) 판단.

토스 /api/v1/market-calendar/KR 의 today.integrated 에서
preMarket / regularMarket / afterMarket 세 세션의 운영시간을 읽어
KST 현재시각과 비교한다. integrated 가 null 이면 휴장이다.
(통합 모드 = KRX + NXT(넥스트레이드) 기준이라 프리/애프터마켓 시간이 포함된다)
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger("market")
KST = ZoneInfo("Asia/Seoul")

# (API 필드 키, 표시 이름) — 시간 순서대로
SESSION_ORDER: list[tuple[str, str]] = [
    ("preMarket", "프리마켓"),
    ("regularMarket", "정규장"),
    ("afterMarket", "애프터마켓"),
]
SESSION_LABELS = dict(SESSION_ORDER)

# .env 의 짧은 이름 → API 필드 키
SESSION_ALIAS = {"pre": "preMarket", "regular": "regularMarket", "after": "afterMarket"}


def resolve_allowed(names: list[str]) -> set[str]:
    """['pre','regular','after'] → {'preMarket','regularMarket','afterMarket'}."""
    return {SESSION_ALIAS[n] for n in names if n in SESSION_ALIAS}


def _parse_hhmm(s: str) -> dtime | None:
    """'09:00' / '09:00:00' / ISO datetime 의 시:분 부분을 time 으로."""
    if not s:
        return None
    txt = s
    if "T" in txt:  # ISO datetime 이면 시간부만
        txt = txt.split("T", 1)[1]
    parts = txt.replace("Z", "").split("+")[0].split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
        return dtime(h, m)
    except (ValueError, IndexError):
        return None


def sessions(calendar: dict) -> dict[str, tuple[dtime, dtime]]:
    """오늘 열리는 세션들의 {키: (시작, 종료)}. 휴장이면 빈 dict."""
    today = calendar.get("today") or {}
    integrated = today.get("integrated")
    if not integrated:
        return {}
    out: dict[str, tuple[dtime, dtime]] = {}
    for key, _ in SESSION_ORDER:
        sess = integrated.get(key)
        if not sess:
            continue
        start = _parse_hhmm(sess.get("startTime", ""))
        end = _parse_hhmm(sess.get("endTime", ""))
        if start and end:
            out[key] = (start, end)
    return out


def market_status(
    calendar: dict,
    allowed: set[str] | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, str]:
    """(개장중 여부, 현재 세션 키, 사유) 반환.

    allowed: 매매를 허용할 세션 키 집합(None 이면 모든 세션 허용).
    """
    now = now or datetime.now(KST)
    sess = sessions(calendar)
    if not sess:
        return False, "", "휴장일(거래 시간 없음)"

    cur = now.time()
    for key, label in SESSION_ORDER:
        if key not in sess:
            continue
        if allowed is not None and key not in allowed:
            continue
        start, end = sess[key]
        if start <= cur <= end:
            return True, key, f"{label} 운영중 ({start:%H:%M}~{end:%H:%M})"

    # 개장 아님 — 오늘 운영 창들을 사유로 안내
    windows = ", ".join(
        f"{SESSION_LABELS[k]} {sess[k][0]:%H:%M}~{sess[k][1]:%H:%M}"
        for k, _ in SESSION_ORDER
        if k in sess
    )
    return False, "", f"거래시간 외 (현재 {cur:%H:%M}; 오늘 운영 {windows})"
