"""메인 자동매매 루프.

흐름: 장 상태 확인 → 시세/잔고 수집 → Claude 판단 → 리스크 검증 → 주문 실행 → 로깅
실거래는 DRY_RUN=false 일 때만 실제 주문이 나갑니다. 충분히 검증 후 전환하세요.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import Config
from claude_engine import ClaudeEngine, Decision
from market import market_status, resolve_allowed, SESSION_LABELS
from risk import RiskManager
from toss_client import TossClient, TossError

KST = ZoneInfo("Asia/Seoul")
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / f"trader_{datetime.now(KST):%Y%m%d}.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("trader")


def build_context(toss: TossClient, cfg: Config, symbols: list[str], universe: list[dict] | None = None) -> dict:
    """Claude 에 보낼 시장 상황 dict 구성. symbols = 이번에 판단할 대상 종목."""
    proxy = cfg.kospi_proxy_symbol
    all_symbols = list(dict.fromkeys([proxy, *symbols]))  # 중복 제거, 순서 유지
    prices = toss.prices(all_symbols)

    # 코스피 프록시 추세(일봉 종가 최근 10개). Candle.closePrice 는 문자열 → float.
    proxy_candles = toss.candles(proxy, interval="1d", count=10)
    proxy_closes: list[float] = []
    for c in proxy_candles:
        try:
            proxy_closes.append(float(c["closePrice"]))
        except (KeyError, TypeError, ValueError):
            continue

    holdings = toss.holdings()
    buying_power = toss.buying_power()

    held = {
        it.get("symbol"): {
            "quantity": it.get("quantity"),
            "averagePurchasePrice": it.get("averagePurchasePrice"),
            "lastPrice": it.get("lastPrice"),
        }
        for it in (holdings.get("items") or [])
    }

    ctx = {
        "now_kst": datetime.now(KST).isoformat(),
        "kospi_proxy": {"symbol": proxy, "last_price": prices.get(proxy), "recent_daily_closes": proxy_closes},
        "symbols": symbols,
        "prices": {s: prices.get(s) for s in symbols},
        "buying_power_krw": buying_power,
        "holdings": held,
        "daily_profit_loss_krw": _daily_pnl(holdings),
        "limits": {
            "max_order_krw": cfg.max_order_krw,
            "note": "주문 수량은 이 금액과 매수가능금액을 넘지 않게 보수적으로 제시할 것",
        },
    }
    if universe:  # 동적 선정 시 각 종목의 선정 사유(뉴스 근거)를 함께 전달
        ctx["screening_reasons"] = {u["symbol"]: u.get("reason", "") for u in universe}
    return ctx


def _daily_pnl(holdings: dict) -> float:
    """당일 손익(원). holdings.dailyProfitLoss.amount.krw (Price 객체의 KRW 합산)."""
    dp = holdings.get("dailyProfitLoss") or {}
    amount = dp.get("amount") or {}
    try:
        return float(amount.get("krw", 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def execute(toss: TossClient, risk: RiskManager, cfg: Config, d: Decision, price: float | None) -> None:
    """단일 판단 실행."""
    if d.action == "HOLD" or d.quantity <= 0:
        log.info("• %s HOLD (conf=%.2f) — %s", d.symbol, d.confidence, d.reason)
        return

    if price is None:
        log.warning("• %s %s 스킵: 현재가 없음", d.symbol, d.action)
        return

    est = price * d.quantity
    ok, why = risk.validate_order(est_amount_krw=est)
    if not ok:
        log.warning("• %s %s x%d 거부: %s", d.symbol, d.action, d.quantity, why)
        return

    # 매도는 보유 수량 검증
    if d.action == "SELL":
        sellable = toss.sellable_quantity(d.symbol)
        if d.quantity > sellable:
            log.warning("• %s SELL x%d 거부: 매도가능 %s주", d.symbol, d.quantity, sellable)
            return

    client_order_id = uuid.uuid4().hex[:32]  # 멱등키 — 타임아웃 재시도 시 중복주문 방지
    log.info(
        "→ 주문 %s %s x%d @LIMIT %s (≈%s원) conf=%.2f — %s",
        d.symbol, d.action, d.quantity, price, f"{est:,.0f}", d.confidence, d.reason,
    )

    if cfg.dry_run:
        log.info("  [DRY_RUN] 실제 주문은 내지 않음. (clientOrderId=%s)", client_order_id)
        risk.record_trade()
        return

    try:
        res = toss.create_order(
            symbol=d.symbol,
            side=d.action,
            order_type="LIMIT",
            quantity=d.quantity,
            price=price,
            client_order_id=client_order_id,
        )
        log.info("  ✅ 주문 접수: orderId=%s", res.get("orderId"))
        risk.record_trade()
    except TossError as e:
        log.error("  ❌ 주문 실패: %s", e)


def resolve_universe(toss: TossClient, cfg: Config, screener) -> tuple[list[str], list[dict]]:
    """이번 판단에 쓸 종목 목록과(동적 선정 시) 선정 메타를 반환.

    동적 모드면 그날 universe(캐시) ∪ 보유종목, 아니면 고정 TRADE_SYMBOLS ∪ 보유종목.
    보유종목을 항상 포함해 보유분 매도(SELL) 길을 열어둔다.
    """
    held = [it.get("symbol") for it in (toss.holdings().get("items") or []) if it.get("symbol")]
    if cfg.dynamic_universe and screener is not None:
        universe = screener.select(toss)
        base = [u["symbol"] for u in universe]
    else:
        universe = []
        base = cfg.trade_symbols
    symbols = list(dict.fromkeys([*base, *held]))  # 중복 제거, 순서 유지
    return symbols, universe


def run_once(toss: TossClient, engine: ClaudeEngine, risk: RiskManager, cfg: Config, screener=None) -> None:
    # 1) 장 상태 (허용 세션: 프리/정규/애프터 중 .env 설정값)
    cal = toss.market_calendar_kr()
    allowed = resolve_allowed(cfg.trade_sessions)
    open_now, session_key, reason = market_status(cal, allowed)
    if not open_now:
        log.info("매매 시간 아님: %s — 건너뜀", reason)
        return
    log.info("장 상태: %s", reason)

    # 2) 종목 universe 결정 (동적 선정 or 고정) + 보유종목
    symbols, universe = resolve_universe(toss, cfg, screener)
    if not symbols:
        log.info("판단 대상 종목이 없음 — 건너뜀")
        return

    # 3) 시장 상황 수집
    ctx = build_context(toss, cfg, symbols, universe)
    ctx["session"] = SESSION_LABELS.get(session_key, session_key)
    if session_key != "regularMarket":
        ctx["session_note"] = "프리/애프터마켓은 유동성이 얕고 변동성이 큽니다. 더 보수적으로 판단하세요."

    # 4) 손실 한도 체크 (당일 손익 기준 킬스위치)
    risk.check_loss(ctx.get("daily_profit_loss_krw", 0.0))
    can, why = risk.can_trade()
    if not can:
        log.warning("매매 불가: %s", why)
        return

    # 5) Claude 판단
    decisions = engine.decide(ctx)

    # 6) 실행
    prices = ctx.get("prices", {})
    for d in decisions:
        execute(toss, risk, cfg, d, prices.get(d.symbol))


def main() -> None:
    cfg = Config()
    cfg.validate()

    log.info("=" * 60)
    log.info("토스 자동매매 시작 | DRY_RUN=%s", cfg.dry_run)
    review = f"+검증({cfg.review_model})" if cfg.review_trades else "(검증 없음)"
    log.info("판단모델: 1차=%s %s | 스크리너=%s", cfg.decision_model, review, cfg.screener_model)
    log.info("코스피 프록시=%s", cfg.kospi_proxy_symbol)
    if cfg.dynamic_universe:
        log.info("종목선정=동적(Claude web_search) | 최대 %d종목 | 경고종목제외=%s",
                 cfg.max_universe, cfg.skip_warned_stocks)
    else:
        log.info("종목선정=고정 | 대상=%s", cfg.trade_symbols)
    log.info("매매 허용 세션=%s", cfg.trade_sessions)
    if not cfg.dry_run:
        log.warning("⚠️  실거래 모드입니다. 실제 주문이 나갑니다.")
    log.info("=" * 60)

    toss = TossClient(cfg.toss_client_id, cfg.toss_client_secret, cfg.account_seq)
    toss.resolve_account_seq()
    engine = ClaudeEngine(
        cfg.anthropic_api_key, cfg.decision_model, cfg.review_model, cfg.review_trades
    )
    risk = RiskManager(
        dry_run=cfg.dry_run,
        max_order_krw=cfg.max_order_krw,
        max_trades_per_day=cfg.max_trades_per_day,
        max_daily_loss_krw=cfg.max_daily_loss_krw,
    )
    screener = None
    if cfg.dynamic_universe:
        from screener import ClaudeScreener
        screener = ClaudeScreener(
            cfg.anthropic_api_key, cfg.screener_model, cfg.max_universe, cfg.skip_warned_stocks
        )

    while True:
        try:
            run_once(toss, engine, risk, cfg, screener)
        except KeyboardInterrupt:
            log.info("종료합니다.")
            break
        except Exception as e:  # 루프는 죽지 않게
            log.exception("루프 오류: %s", e)
        time.sleep(cfg.loop_interval_sec)


if __name__ == "__main__":
    main()
