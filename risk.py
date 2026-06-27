"""리스크 관리 / 안전장치.

- DRY_RUN: 실제 주문 차단
- 1회 주문 금액 상한
- 일일 주문 횟수 상한
- 일일 손실 한도 초과 시 당일 매매 중단(킬스위치)
- 당일 상태를 state/state_YYYYMMDD.json 에 영속화 (재시작해도 유지)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("risk")
KST = ZoneInfo("Asia/Seoul")
STATE_DIR = Path(__file__).parent / "state"


@dataclass
class DailyState:
    date: str
    trades: int = 0
    killed: bool = False
    kill_reason: str = ""


class RiskManager:
    def __init__(
        self,
        *,
        dry_run: bool,
        max_order_krw: int,
        max_trades_per_day: int,
        max_daily_loss_krw: int,
    ):
        self.dry_run = dry_run
        self.max_order_krw = max_order_krw
        self.max_trades_per_day = max_trades_per_day
        self.max_daily_loss_krw = max_daily_loss_krw
        STATE_DIR.mkdir(exist_ok=True)
        self.state = self._load()

    # ── 상태 영속화 ─────────────────────────────────────────
    def _today(self) -> str:
        return datetime.now(KST).strftime("%Y%m%d")

    def _path(self) -> Path:
        return STATE_DIR / f"state_{self._today()}.json"

    def _load(self) -> DailyState:
        p = self._path()
        if p.exists():
            try:
                return DailyState(**json.loads(p.read_text(encoding="utf-8")))
            except Exception as e:  # 손상 시 새로 시작
                log.warning("상태 파일 로드 실패(%s), 새로 시작", e)
        return DailyState(date=self._today())

    def _save(self) -> None:
        self._path().write_text(
            json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 체크 ────────────────────────────────────────────────
    def check_loss(self, daily_pnl_krw: float) -> None:
        """당일 손익을 받아 손실 한도 초과 시 킬스위치를 켠다."""
        if self.state.killed:
            return
        if daily_pnl_krw <= -abs(self.max_daily_loss_krw):
            self.state.killed = True
            self.state.kill_reason = (
                f"일일 손실 한도 초과 (손익 {daily_pnl_krw:,.0f}원 ≤ -{self.max_daily_loss_krw:,}원)"
            )
            self._save()
            log.error("🛑 킬스위치 발동: %s", self.state.kill_reason)

    def can_trade(self) -> tuple[bool, str]:
        if self.state.killed:
            return False, f"당일 매매 중단됨: {self.state.kill_reason}"
        if self.state.trades >= self.max_trades_per_day:
            return False, f"일일 주문 횟수 상한 도달 ({self.state.trades}/{self.max_trades_per_day})"
        return True, ""

    def validate_order(self, *, est_amount_krw: float) -> tuple[bool, str]:
        """주문 직전 금액 상한 검증."""
        ok, why = self.can_trade()
        if not ok:
            return False, why
        if est_amount_krw > self.max_order_krw:
            return False, (
                f"1회 주문 금액 상한 초과 (예상 {est_amount_krw:,.0f}원 > {self.max_order_krw:,}원)"
            )
        return True, ""

    def record_trade(self) -> None:
        self.state.trades += 1
        self._save()
        log.info("주문 카운트: %d/%d", self.state.trades, self.max_trades_per_day)
