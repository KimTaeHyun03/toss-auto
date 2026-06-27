"""환경설정 로드 및 안전장치 파라미터.

자격증명은 .env(gitignore) 에서만 읽습니다. 코드에 비밀값을 박지 않습니다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

TOSS_BASE_URL = "https://openapi.tossinvest.com"


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v and v.strip() else default


def _csv(name: str, default: list[str]) -> list[str]:
    v = os.getenv(name)
    if not v or not v.strip():
        return default
    return [s.strip() for s in v.split(",") if s.strip()]


@dataclass
class Config:
    # 자격증명
    toss_client_id: str = field(default_factory=lambda: os.getenv("TOSS_CLIENT_ID", ""))
    toss_client_secret: str = field(default_factory=lambda: os.getenv("TOSS_CLIENT_SECRET", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))

    # 계좌 (비우면 자동 선택)
    account_seq: int | None = field(
        default_factory=lambda: (_int("TOSS_ACCOUNT_SEQ", 0) or None)
    )

    # 대상 종목
    kospi_proxy_symbol: str = field(default_factory=lambda: os.getenv("KOSPI_PROXY_SYMBOL", "069500"))
    trade_symbols: list[str] = field(default_factory=lambda: _csv("TRADE_SYMBOLS", ["069500"]))

    # 동적 종목 선정: Claude 가 뉴스/시장을 web_search 로 조사해 그날 매매 후보를 직접 고름
    dynamic_universe: bool = field(default_factory=lambda: _bool("DYNAMIC_UNIVERSE", False))
    max_universe: int = field(default_factory=lambda: _int("MAX_UNIVERSE", 8))
    skip_warned_stocks: bool = field(default_factory=lambda: _bool("SKIP_WARNED_STOCKS", True))

    # 안전장치
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    max_order_krw: int = field(default_factory=lambda: _int("MAX_ORDER_KRW", 100_000))
    max_trades_per_day: int = field(default_factory=lambda: _int("MAX_TRADES_PER_DAY", 5))
    max_daily_loss_krw: int = field(default_factory=lambda: _int("MAX_DAILY_LOSS_KRW", 50_000))

    # 매매를 허용할 세션 (pre=프리마켓, regular=정규장, after=애프터마켓)
    trade_sessions: list[str] = field(
        default_factory=lambda: _csv("TRADE_SESSIONS", ["pre", "regular", "after"])
    )

    # 루프
    loop_interval_sec: int = field(default_factory=lambda: _int("LOOP_INTERVAL_SEC", 300))

    # 모델: 1차 판단(저가) / 2차 검증(상위) / 스크리너(종목선정)
    decision_model: str = field(default_factory=lambda: os.getenv("DECISION_MODEL", "claude-haiku-4-5"))
    review_model: str = field(default_factory=lambda: os.getenv("REVIEW_MODEL", "claude-opus-4-8"))
    review_trades: bool = field(default_factory=lambda: _bool("REVIEW_TRADES", True))
    # 스크리너 모델 (하위호환: SCREENER_MODEL → 기존 CLAUDE_MODEL → opus)
    screener_model: str = field(
        default_factory=lambda: os.getenv("SCREENER_MODEL", os.getenv("CLAUDE_MODEL", "claude-opus-4-8"))
    )

    def validate(self) -> None:
        missing = [
            n
            for n, v in [
                ("TOSS_CLIENT_ID", self.toss_client_id),
                ("TOSS_CLIENT_SECRET", self.toss_client_secret),
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
            ]
            if not v
        ]
        if missing:
            raise SystemExit(
                f"필수 환경변수가 비어 있습니다: {', '.join(missing)}\n"
                ".env.example 을 .env 로 복사한 뒤 값을 채워주세요."
            )
