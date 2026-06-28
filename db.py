"""주문 감사 로그 PostgreSQL 적재 (선택적).

DATABASE_URL 환경변수가 있으면 Postgres 에 기록/조회, 없으면 비활성화되어
trader 는 CSV(state/orders.csv)로, dashboard 는 CSV 로 폴백한다.
→ 로컬 개발은 DB 없이 그대로 동작, 배포(Cloudtype Postgres)는 DB 영속.

연결 문자열 예: postgresql://user:password@host:5432/dbname
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

log = logging.getLogger("db")

_DSN = os.getenv("DATABASE_URL", "").strip()

# orders = 실제로 나간 주문 원장 / decisions = 매 사이클 모든 판단(HOLD·반려 포함)
_SCHEMA_ORDERS = """
CREATE TABLE IF NOT EXISTS orders (
    id          BIGSERIAL PRIMARY KEY,
    ts_kst      TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    qty         NUMERIC,
    price       NUMERIC,
    est_krw     NUMERIC,
    order_id    TEXT,
    dry_run     BOOLEAN,
    confidence  NUMERIC,
    reason      TEXT,
    review_note TEXT
);
"""

_SCHEMA_DECISIONS = """
CREATE TABLE IF NOT EXISTS decisions (
    id          BIGSERIAL PRIMARY KEY,
    ts_kst      TIMESTAMPTZ NOT NULL,
    symbol      TEXT NOT NULL,
    session     TEXT,            -- 프리/정규/애프터
    last_price  NUMERIC,         -- 판단 당시 현재가
    action      TEXT,            -- 최종 판단 BUY/SELL/HOLD
    qty         INTEGER,         -- 제안 수량
    confidence  NUMERIC,         -- 1차 확신도
    reviewed    BOOLEAN,         -- 2차 검증 거쳤는지
    review_note TEXT,            -- 2차 검증 사유
    reason      TEXT,            -- 1차 판단 근거
    outcome     TEXT             -- SUBMITTED(접수,체결미확정)/PAPER_FILL(모의가정체결)/HOLD/SKIP_*/REJECT_*/FAIL_*
);
"""


def enabled() -> bool:
    return bool(_DSN)


def _connect():
    import psycopg  # 지연 import — DB 미사용 환경에서 의존성 없어도 동작
    return psycopg.connect(_DSN, connect_timeout=10)


def init() -> None:
    """스키마 생성(멱등). 실패해도 예외를 삼켜 CSV 폴백을 막지 않는다."""
    if not enabled():
        return
    try:
        with _connect() as c, c.cursor() as cur:
            cur.execute(_SCHEMA_ORDERS)
            cur.execute(_SCHEMA_DECISIONS)
        log.info("DB 스키마 준비 완료 (orders, decisions)")
    except Exception as e:  # noqa: BLE001
        log.warning("DB 초기화 실패(%s) — CSV 폴백", e)


def insert_order(
    *, ts: datetime, symbol: str, side: str, qty, price, est_krw,
    order_id: str, dry_run: bool, confidence, reason: str, review_note: str,
) -> bool:
    """주문 1건 적재. 성공 시 True. 실패/비활성 시 False(→ 호출측이 CSV 폴백)."""
    if not enabled():
        return False
    try:
        with _connect() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO orders
                   (ts_kst,symbol,side,qty,price,est_krw,order_id,dry_run,confidence,reason,review_note)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ts, symbol, side, qty, price, est_krw, order_id, dry_run, confidence, reason, review_note),
            )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("DB 주문기록 실패(%s) — CSV 폴백", e)
        return False


def insert_decision(
    *, ts: datetime, symbol: str, session: str, last_price, action: str, qty: int,
    confidence, reviewed: bool, review_note: str, reason: str, outcome: str,
) -> bool:
    """판단 1건(HOLD 포함) 적재. DB 비활성이면 무동작(판단 로그는 DB 전용)."""
    if not enabled():
        return False
    try:
        with _connect() as c, c.cursor() as cur:
            cur.execute(
                """INSERT INTO decisions
                   (ts_kst,symbol,session,last_price,action,qty,confidence,reviewed,review_note,reason,outcome)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (ts, symbol, session, last_price, action, qty, confidence, reviewed, review_note, reason, outcome),
            )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("DB 판단기록 실패(%s)", e)
        return False


def _numstr(v) -> str:
    """NUMERIC → 프론트가 +o.price 로 파싱할 수 있게 숫자 문자열로."""
    if v is None:
        return ""
    s = str(v)
    return s.rstrip("0").rstrip(".") if "." in s else s


def recent_orders(limit: int = 25) -> list[dict]:
    """최근 주문(최신순). CSV 와 동일한 키/문자열 형식으로 반환해 프론트 호환."""
    if not enabled():
        return []
    try:
        with _connect() as c, c.cursor() as cur:
            cur.execute(
                """SELECT ts_kst,symbol,side,qty,price,est_krw,order_id,dry_run,confidence,reason,review_note
                   FROM orders ORDER BY id DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("DB 주문조회 실패(%s)", e)
        return []
    out = []
    for r in rows:
        ts, sym, side, qty, price, est, oid, dry, conf, reason, note = r
        out.append({
            "ts_kst": ts.isoformat() if ts else "",
            "symbol": sym,
            "side": side,
            "qty": _numstr(qty),
            "price": _numstr(price),
            "est_krw": _numstr(est),
            "order_id": oid or "",
            "dry_run": "True" if dry else "False",  # CSV 와 동일하게 문자열
            "confidence": f"{float(conf):.2f}" if conf is not None else "",
            "reason": reason or "",
            "review_note": note or "",
        })
    return out
