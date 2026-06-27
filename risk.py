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
    equity_start: float | None = None  # 일중 시작 자산(현금+평가액) 기준점


def buy_orders_to_cancel(
    open_orders: list[dict],
    *,
    last_prices: dict[str, float],
    ref_prices: dict[str, float],
    drop_pct: float,
) -> list[tuple[dict, str]]:
    """폭락한 미체결 '매수 지정가' 주문의 취소 대상과 사유를 판정(순수 함수).

    open_orders : Order dict 리스트(side/orderType/price/symbol/status 포함)
    last_prices : {symbol: 현재가}
    ref_prices  : {symbol: 최근 N분 고점} — 없으면 키 누락 허용
    drop_pct    : 취소 임계 하락률(%). 0 이하이면 비활성.

    트리거(둘 중 하나라도 충족 시 취소):
      A) 연속 하락 — 현재가가 최근 고점(ref) 대비 drop_pct% 이상 하락.
         지정가보다 위에서 무너질 때 '체결 전에' 잡는다(사용자가 우려한 케이스).
      B) 갭/VI 하락 — 현재가가 지정가(limit) 대비 drop_pct% 이상 낮음.
         연속 거래에선 그 전에 체결되므로 갭다운·정지 후 재개 시의 안전망.
    """
    if drop_pct <= 0:
        return []
    thr = drop_pct / 100.0
    out: list[tuple[dict, str]] = []
    for o in open_orders:
        if o.get("side") != "BUY" or o.get("orderType") != "LIMIT":
            continue
        if o.get("status") == "PENDING_CANCEL":  # 이미 취소 진행 중 → 중복 취소 방지
            continue
        sym = o.get("symbol")
        last = last_prices.get(sym)
        if last is None:
            continue
        try:
            limit = float(o.get("price"))
        except (TypeError, ValueError):
            continue
        ref = ref_prices.get(sym)
        if ref and ref > 0 and (ref - last) / ref >= thr:  # A) 최근 고점 대비 폭락
            out.append((o, f"최근고점 {ref:,.0f} 대비 {(ref - last) / ref * 100:.1f}% 폭락 (현재 {last:,.0f})"))
            continue
        if limit > 0 and (limit - last) / limit >= thr:  # B) 지정가 대비 갭다운
            out.append((o, f"매수지정가 {limit:,.0f} 대비 {(limit - last) / limit * 100:.1f}% 낮음 (현재 {last:,.0f})"))
    return out


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
    def update_equity(self, equity_now: float) -> None:
        """현재 자산(현금+평가액)을 받아 일중 손실 한도 초과 시 킬스위치를 켠다.

        보유종목 평가손익(dailyProfitLoss)이 아니라 '계좌 자산 변동'을 본다.
        → 손절로 실현한 손실, 수수료·거래세가 모두 현금에 반영되므로
          여러 번 손절매해 누적된 실현손실도 빠짐없이 잡힌다.
        """
        if self.state.equity_start is None:  # 그날 첫 관측을 기준점으로
            self.state.equity_start = equity_now
            self._save()
            log.info("일중 시작 자산 기준: %s원", f"{equity_now:,.0f}")
            return
        pnl = equity_now - self.state.equity_start
        if not self.state.killed and pnl <= -abs(self.max_daily_loss_krw):
            self.state.killed = True
            self.state.kill_reason = (
                f"일일 손실 한도 초과 (자산변동 {pnl:,.0f}원 ≤ -{self.max_daily_loss_krw:,}원)"
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
