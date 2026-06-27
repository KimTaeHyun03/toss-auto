"""모의 체결 + 페이퍼 손익 시뮬레이터 (DRY_RUN 전용).

주문이 지정가에 체결됐다고 가정하고 가상 포트폴리오(현금·포지션·실현손익)를 DB에 갱신한다.
실제 계좌와 완전히 분리된 '100만원 가상계좌'로 봇 전략의 성과를 평가하는 용도.
DATABASE_URL 없으면 전부 무동작.

테이블: paper_account(현금·실현손익) / paper_positions(보유) / paper_fills(체결원장)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

import db

log = logging.getLogger("paper")

START_KRW = float(os.getenv("PAPER_START_KRW", "1000000"))      # 가상 시작 현금
FEE_PCT = float(os.getenv("PAPER_FEE_PCT", "0.015")) / 100.0     # 편도 수수료율
TAX_PCT = float(os.getenv("PAPER_TAX_PCT", "0.18")) / 100.0      # 매도 거래세율

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS paper_account (
        id INT PRIMARY KEY DEFAULT 1,
        cash NUMERIC NOT NULL,
        realized_pnl NUMERIC NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ
    );""",
    """CREATE TABLE IF NOT EXISTS paper_positions (
        symbol TEXT PRIMARY KEY,
        qty NUMERIC NOT NULL,
        avg_cost NUMERIC NOT NULL,
        updated_at TIMESTAMPTZ
    );""",
    """CREATE TABLE IF NOT EXISTS paper_fills (
        id BIGSERIAL PRIMARY KEY,
        ts_kst TIMESTAMPTZ NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        qty NUMERIC, price NUMERIC, amount NUMERIC,
        fee NUMERIC, tax NUMERIC, realized_pnl NUMERIC
    );""",
]


def _apply_fill(old_qty: float, old_avg: float, side: str, qty: float, price: float):
    """포지션·현금 변화 계산(순수 함수, DB 무관 — 테스트 가능).

    반환: (new_qty, new_avg, cash_delta, realized, fee, tax)
      cash_delta: 현금 증감(BUY 음수, SELL 양수)  realized: 실현손익(SELL만)
    매수: 평단 가중평균. 매도: 보유초과는 보유분까지만(페이퍼), 실현손익=(체결가-평단)*수량-수수료-세금.
    """
    amount = qty * price
    fee = amount * FEE_PCT
    if side == "BUY":
        new_qty = old_qty + qty
        new_avg = (old_qty * old_avg + amount) / new_qty if new_qty else 0.0
        return new_qty, new_avg, -(amount + fee), 0.0, fee, 0.0
    # SELL
    sell_qty = min(qty, old_qty)            # 보유 초과 매도 방지
    sell_amt = sell_qty * price
    fee = sell_amt * FEE_PCT
    tax = sell_amt * TAX_PCT
    realized = (price - old_avg) * sell_qty - fee - tax
    new_qty = old_qty - sell_qty
    new_avg = old_avg if new_qty > 0 else 0.0
    return new_qty, new_avg, (sell_amt - fee - tax), realized, fee, tax


def init() -> None:
    if not db.enabled():
        return
    try:
        with db._connect() as c, c.cursor() as cur:
            for s in _SCHEMA:
                cur.execute(s)
            cur.execute(
                "INSERT INTO paper_account (id,cash,realized_pnl,updated_at) VALUES (1,%s,0,%s) "
                "ON CONFLICT (id) DO NOTHING",
                (START_KRW, datetime.now()),
            )
        log.info("페이퍼 계좌 준비 (시작 현금 %s원)", f"{START_KRW:,.0f}")
    except Exception as e:  # noqa: BLE001
        log.warning("페이퍼 초기화 실패(%s)", e)


def record_fill(*, ts: datetime, symbol: str, side: str, qty, price) -> bool:
    """모의 체결 1건 반영(포지션·현금·실현손익 갱신 + 체결원장 기록)."""
    if not db.enabled():
        return False
    qty = float(qty)
    price = float(price)
    try:
        with db._connect() as c, c.cursor() as cur:
            cur.execute("SELECT qty, avg_cost FROM paper_positions WHERE symbol=%s FOR UPDATE", (symbol,))
            row = cur.fetchone()
            old_qty, old_avg = (float(row[0]), float(row[1])) if row else (0.0, 0.0)
            new_qty, new_avg, cash_delta, realized, fee, tax = _apply_fill(old_qty, old_avg, side, qty, price)

            if new_qty > 0:
                cur.execute(
                    "INSERT INTO paper_positions (symbol,qty,avg_cost,updated_at) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (symbol) DO UPDATE SET qty=EXCLUDED.qty, avg_cost=EXCLUDED.avg_cost, "
                    "updated_at=EXCLUDED.updated_at",
                    (symbol, new_qty, new_avg, ts),
                )
            else:
                cur.execute("DELETE FROM paper_positions WHERE symbol=%s", (symbol,))

            cur.execute(
                "UPDATE paper_account SET cash=cash+%s, realized_pnl=realized_pnl+%s, updated_at=%s WHERE id=1",
                (cash_delta, realized, ts),
            )
            cur.execute(
                "INSERT INTO paper_fills (ts_kst,symbol,side,qty,price,amount,fee,tax,realized_pnl) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ts, symbol, side, qty, price, qty * price, fee, tax, realized),
            )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("페이퍼 체결기록 실패(%s)", e)
        return False


def position_symbols() -> list[str]:
    """현재 페이퍼 보유 종목코드(현재가 조회용)."""
    if not db.enabled():
        return []
    try:
        with db._connect() as c, c.cursor() as cur:
            cur.execute("SELECT symbol FROM paper_positions")
            return [r[0] for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def snapshot(prices: dict) -> dict | None:
    """현재 페이퍼 포트폴리오 상태(대시보드용). prices: {symbol: 현재가}."""
    if not db.enabled():
        return None
    try:
        with db._connect() as c, c.cursor() as cur:
            cur.execute("SELECT cash, realized_pnl FROM paper_account WHERE id=1")
            acc = cur.fetchone()
            cur.execute("SELECT symbol, qty, avg_cost FROM paper_positions ORDER BY symbol")
            pos = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        log.warning("페이퍼 조회 실패(%s)", e)
        return None
    if not acc:
        return None
    cash = float(acc[0])
    realized = float(acc[1])
    positions, mv, unreal = [], 0.0, 0.0
    for sym, q, avg in pos:
        q, avg = float(q), float(avg)
        last = float(prices.get(sym) or avg)  # 현재가 없으면 평단으로 평가
        val, u = last * q, (last - avg) * q
        mv += val
        unreal += u
        positions.append({
            "symbol": sym, "qty": q, "avg": avg, "last": last, "eval_krw": val,
            "unreal_pnl": u, "pnl_pct": ((last - avg) / avg * 100) if avg else 0.0,
        })
    equity = cash + mv
    return {
        "start_krw": START_KRW, "cash": cash, "market_value": mv, "equity": equity,
        "realized_pnl": realized, "unreal_pnl": unreal,
        "total_pnl": equity - START_KRW,
        "total_pnl_pct": (equity - START_KRW) / START_KRW * 100 if START_KRW else 0.0,
        "positions": positions,
    }
