"""메인 자동매매 루프.

흐름: 장 상태 확인 → 시세/잔고 수집 → Claude 판단 → 리스크 검증 → 주문 실행 → 로깅
실거래는 DRY_RUN=false 일 때만 실제 주문이 나갑니다. 충분히 검증 후 전환하세요.
"""
from __future__ import annotations

import atexit
import csv
import logging
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import Config
from claude_engine import ClaudeEngine, Decision
from market import market_status, resolve_allowed, SESSION_LABELS
from risk import RiskManager, buy_orders_to_cancel
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

    # 계좌 자산(equity) = 보유 평가금액 + 현금매수가능액 → 킬스위치 기준
    mv = (holdings.get("marketValue") or {}).get("amount") or {}
    try:
        market_value_krw = float(mv.get("krw", 0) or 0)
    except (TypeError, ValueError):
        market_value_krw = 0.0
    equity_krw = market_value_krw + buying_power

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
        "equity_krw": equity_krw,
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


def _entry_prices(entries: list[dict]) -> list[float]:
    """호가 엔트리 리스트 → float 가격 리스트(원래 순서 유지)."""
    out: list[float] = []
    for e in entries or []:
        try:
            out.append(float(e["price"]))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def resolve_order_price(
    toss: TossClient, cfg: Config, symbol: str, side: str, *, fallback: float | None
) -> float | None:
    """주문 직전 최신가를 재조회하고 마케터블 리밋 지정가를 산출한다.

    #1 최신 현재가 재조회 → ctx 스냅샷(fallback) 대신 사용.
    #2 마케터블 리밋 → 최신 호가 사다리에서 약간 공격적인 지정가를 고른다.
       매수: 현재가×(1+α) 이하 중 최고 매도호가. α 밴드를 넘는 호가뿐이면(갭업/급변)
             추격하지 않고 보류(None) — 비싸게 따라붙는 것을 막는다(α=상한).
       매도: 현재가×(1−α) 이상 중 최저 매수호가. 밴드 밖이면(폭락) 손절·청산을 위해
             최우선 매수호가로 크로스해 체결을 우선한다(α=상한이 아니라 목표가).
    호가는 거래소가 주는 값이라 항상 유효 틱. 호가창이 없으면 최신 현재가로 평범한 지정가.
    반환 None = 가격 미확보 또는 매수 보류(주문 스킵).
    """
    try:
        fresh = toss.prices([symbol]).get(symbol)
    except TossError:
        fresh = None
    base = fresh if fresh is not None else fallback
    if base is None:
        return None

    a = cfg.marketable_limit_pct / 100.0
    if a <= 0:
        return base  # 마케터블 비활성: 최신 현재가 그대로 지정가

    try:
        ob = toss.orderbook(symbol)
    except TossError:
        ob = {}
    asks = sorted(_entry_prices(ob.get("asks", [])))          # 오름차순: [0]=최우선 매도호가
    bids = sorted(_entry_prices(ob.get("bids", [])), reverse=True)  # 내림차순: [0]=최우선 매수호가

    if side == "BUY":
        if not asks:
            return base  # 호가창 없음 → 폴백(최신 현재가)
        target = base * (1 + a)
        within = [p for p in asks if p <= target]
        if not within:  # 최우선 매도호가가 +α 밴드 밖(갭업/급변) → 추격 금지, 보류
            log.warning(
                "• %s BUY 보류: 최우선 매도호가 %s 가 +%.2f%% 밴드 밖 (현재가 %s)",
                symbol, asks[0], cfg.marketable_limit_pct, base,
            )
            return None
        return max(within)
    else:  # SELL: 손절·청산이므로 밴드를 벗어나도 체결 위해 최우선 매수호가로 크로스
        if not bids:
            return base
        target = base * (1 - a)
        within = [p for p in bids if p >= target]
        return min(within) if within else bids[0]


def execute(toss: TossClient, risk: RiskManager, cfg: Config, d: Decision, price: float | None) -> None:
    """단일 판단 실행."""
    if d.action == "HOLD" or d.quantity <= 0:
        log.info("• %s HOLD (conf=%.2f) — %s", d.symbol, d.confidence, d.reason)
        return

    # #1 주문 직전 최신가 재조회 + #2 마케터블 리밋 산출 (ctx 스냅샷 price 는 폴백)
    price = resolve_order_price(toss, cfg, d.symbol, d.action, fallback=price)
    if price is None:
        log.warning("• %s %s 스킵: 주문가 미확보/보류", d.symbol, d.action)
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
        _audit(d, price, est, order_id="DRY_RUN", dry_run=True, note=d.review_note)
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
        _audit(d, price, est, order_id=str(res.get("orderId")), dry_run=False, note=d.review_note)
        risk.record_trade()
    except TossError as e:
        log.error("  ❌ 주문 실패: %s", e)


_AUDIT_PATH = Path(__file__).parent / "state" / "orders.csv"


def _audit(d, price: float, est: float, *, order_id: str, dry_run: bool, note: str = "") -> None:
    """주문 1건을 state/orders.csv 에 누적 기록(전략 평가용 영속 감사 로그)."""
    new = not _AUDIT_PATH.exists()
    try:
        with _AUDIT_PATH.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts_kst", "symbol", "side", "qty", "price", "est_krw",
                            "order_id", "dry_run", "confidence", "reason", "review_note"])
            w.writerow([datetime.now(KST).isoformat(), d.symbol, d.action, d.quantity,
                        price, f"{est:.0f}", order_id, dry_run, f"{d.confidence:.2f}",
                        d.reason, note])
    except OSError as e:
        log.warning("감사 로그 기록 실패: %s", e)


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


def cancel_crashing_buys(toss: TossClient, cfg: Config, open_orders: list[dict]) -> None:
    """폭락한 미체결 '매수 지정가' 주문을 강제 취소한다.

    노출을 줄이는 행위라 킬스위치/주문횟수 한도와 무관하게 항상 동작한다.
    DRY_RUN 이면 실제 취소 대신 '취소 예정'만 로깅한다.
    미체결 종목은 universe·보유에 없을 수 있어 시세/캔들을 여기서 직접 조회한다.
    """
    if cfg.cancel_buy_drop_pct <= 0:
        return
    buys = [o for o in open_orders if o.get("side") == "BUY" and o.get("orderType") == "LIMIT"]
    syms = list({o.get("symbol") for o in buys if o.get("symbol")})
    if not syms:
        return

    last_prices = toss.prices(syms)
    ref_prices: dict[str, float] = {}
    for s in syms:  # 최근 N분 고점 = 폭락 판정 기준선
        closes = []
        for c in toss.candles(s, interval="1m", count=cfg.cancel_lookback_min):
            try:
                closes.append(float(c["closePrice"]))
            except (KeyError, TypeError, ValueError):
                continue
        if closes:
            ref_prices[s] = max(closes)

    targets = buy_orders_to_cancel(
        buys, last_prices=last_prices, ref_prices=ref_prices, drop_pct=cfg.cancel_buy_drop_pct
    )
    for o, why in targets:
        oid = o.get("orderId")
        log.warning("⚠️ 폭락 감지 — 미체결 매수주문 취소: %s orderId=%s — %s", o.get("symbol"), oid, why)
        if cfg.dry_run:
            log.info("  [DRY_RUN] 실제 취소는 하지 않음 (orderId=%s)", oid)
            continue
        try:
            toss.cancel_order(str(oid))
            log.info("  ✅ 취소 접수 완료: orderId=%s", oid)
        except TossError as e:
            log.error("  ❌ 취소 실패: %s", e)


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

    # 3) 미체결 주문 확인 (중복 적재/초과매도 방지) + 폭락 시 미체결 매수주문 강제 취소
    open_ords = toss.open_orders()
    cancel_crashing_buys(toss, cfg, open_ords)
    # 취소는 즉시 반영이 아닐 수 있어, 이번 사이클은 해당 종목을 그대로 미체결로 보고 스킵한다.
    resting = {o.get("symbol") for o in open_ords if o.get("symbol")}
    if resting:
        log.info("미체결 주문 보유 종목(이번 매매 제외): %s", ", ".join(sorted(resting)))

    # 4) 시장 상황 수집
    ctx = build_context(toss, cfg, symbols, universe)
    ctx["session"] = SESSION_LABELS.get(session_key, session_key)
    ctx["open_order_symbols"] = sorted(resting)
    if session_key != "regularMarket":
        ctx["session_note"] = "프리/애프터마켓은 유동성이 얕고 변동성이 큽니다. 더 보수적으로 판단하세요."

    # 5) 손실 한도 체크 — 계좌 자산(현금+평가액) 변동 기준 킬스위치
    risk.update_equity(ctx.get("equity_krw", 0.0))
    can, why = risk.can_trade()
    if not can:
        log.warning("매매 불가: %s", why)
        return

    # 6) Claude 판단
    decisions = engine.decide(ctx)

    # 7) 실행 (미체결 주문 있는 종목은 건너뜀)
    prices = ctx.get("prices", {})
    for d in decisions:
        if d.action in ("BUY", "SELL") and d.symbol in resting:
            log.info("• %s %s 스킵: 해당 종목 미체결 주문 존재", d.symbol, d.action)
            continue
        execute(toss, risk, cfg, d, prices.get(d.symbol))


def _acquire_lock() -> None:
    """단일 인스턴스 보장. 중복 실행 시 이중 주문을 막는다."""
    lock = Path(__file__).parent / "state" / "trader.lock"
    lock.parent.mkdir(exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(
            f"이미 실행 중이거나 비정상 종료된 잠금 파일이 있습니다: {lock}\n"
            "다른 인스턴스가 떠 있지 않다면 이 파일을 삭제하고 다시 실행하세요."
        )
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    atexit.register(lambda: lock.unlink(missing_ok=True))


def main() -> None:
    cfg = Config()
    cfg.validate()
    _acquire_lock()

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
