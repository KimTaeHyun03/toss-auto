"""Claude 매매 판단 엔진 (2단 구조).

1차(저가 모델, 예: Haiku): 모든 대상 종목에 대해 BUY/SELL/HOLD 판단.
2차(상위 모델, 예: Opus): 1차가 '사거나 팔자'(BUY/SELL)고 한 건만 골라 검증.
  - HOLD 는 검증하지 않는다(대부분이 HOLD라 상위 모델 호출이 거의 없어 비용 절감).
  - 검증에서 반려(REJECT)되면 그 종목은 HOLD 로 강등되어 주문이 나가지 않는다.
출력은 strict tool use 로 구조를 강제하고, 받은 값은 코드에서 다시 검증한다.
모델 원문 텍스트를 절대 주문 payload 로 직접 쓰지 않는다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import anthropic

log = logging.getLogger("claude")


# ── 도구 정의 ───────────────────────────────────────────────
def _decision_tool(symbols: list[str]) -> dict:
    """1차 판단 도구. 대상 종목을 enum 으로 박아 임의 종목 생성을 막는다."""
    return {
        "name": "submit_decision",
        "description": "한 종목에 대한 매매 판단을 제출한다. 종목마다 정확히 한 번 호출한다.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "enum": symbols, "description": "판단 대상 종목 코드."},
                "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
                "quantity": {"type": "integer", "description": "BUY/SELL 시 주문 수량(주). HOLD 면 0."},
                "confidence": {"type": "number", "description": "판단 확신도 0.0~1.0."},
                "reason": {"type": "string", "description": "판단 근거 한 줄 요약."},
            },
            "required": ["symbol", "action", "quantity", "confidence", "reason"],
            "additionalProperties": False,
        },
    }


def _review_tool(symbols: list[str]) -> dict:
    """2차 검증 도구. 1차 제안 종목만 enum 으로 둔다."""
    return {
        "name": "submit_review",
        "description": "1차 매매 제안 한 건을 검증한다. 제안마다 정확히 한 번 호출한다.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "enum": symbols, "description": "검증 대상 종목 코드."},
                "verdict": {
                    "type": "string",
                    "enum": ["APPROVE", "REJECT"],
                    "description": "APPROVE=주문 승인, REJECT=주문 반려(HOLD 강등).",
                },
                "adjusted_quantity": {
                    "type": "integer",
                    "description": "승인 시 수량 조정값. 0이면 1차 제안 수량 유지. 1차 수량보다 작게만 조정 가능.",
                },
                "reason": {"type": "string", "description": "검증 사유 한 줄."},
            },
            "required": ["symbol", "verdict", "adjusted_quantity", "reason"],
            "additionalProperties": False,
        },
    }


PRIMARY_SYSTEM = """\
너는 한국 주식시장(코스피)에서 동작하는 보수적인 1차 매매 판단 엔진이다.
주어진 코스피 프록시(KODEX200 등) 추세, 대상 종목 시세/캔들, 보유 현황, 매수가능금액을 보고
각 대상 종목에 대해 BUY / SELL / HOLD 중 하나를 판단한다.

원칙:
- 확실하지 않으면 HOLD 한다. 과도한 매매를 피한다.
- 매수 수량은 매수가능금액과 1회 주문 상한을 넘지 않도록 보수적으로 제시한다.
- 매도는 보유 수량 범위 내에서만 제시한다.
- 코스피 프록시가 뚜렷한 하락 추세면 신규 매수에 신중하라.
- screening_reasons 가 있으면 선정 사유(뉴스/시장 변화)를 참고하되 맹신하지 말고 시세로 교차검증하라.
반드시 submit_decision 도구로만 답한다. 종목마다 한 번씩 호출한다."""

REVIEW_SYSTEM = """\
너는 1차 엔진의 매매 제안을 검증하는 신중한 상위 검토자(게이트키퍼)다.
1차가 '사자/팔자'(BUY/SELL)고 한 제안만 받는다. 각 제안에 대해:
- 시세·추세·보유현황·리스크에 비추어 타당하면 APPROVE.
- 근거가 약하거나, 추세와 어긋나거나, 과도하거나, 의심스러우면 REJECT(보수적으로 반려).
- 승인하되 수량이 과하면 adjusted_quantity 로 더 작게 줄일 수 있다(키울 수는 없다).
확신이 없으면 REJECT 한다. 한 번 거른다는 마음으로 깐깐하게 본다.
반드시 submit_review 도구로만 답한다. 제안마다 한 번씩 호출한다."""


@dataclass
class Decision:
    symbol: str
    action: str       # BUY | SELL | HOLD
    quantity: int
    confidence: float
    reason: str
    reviewed: bool = False     # 2차 검증을 거쳤는지
    review_note: str = ""      # 2차 검증 사유


class ClaudeEngine:
    def __init__(
        self,
        api_key: str,
        primary_model: str,
        review_model: str | None = None,
        review_enabled: bool = True,
    ):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._primary = primary_model
        self._review = review_model
        self._review_enabled = bool(review_enabled and review_model)

    # ── 진입점 ──────────────────────────────────────────────
    def decide(self, market_context: dict) -> list[Decision]:
        decisions = self._primary_pass(market_context)
        if not self._review_enabled:
            return decisions

        actionable = [d for d in decisions if d.action in ("BUY", "SELL")]
        if not actionable:
            return decisions  # 전부 HOLD → 상위 모델 호출 없음(비용 절감)

        log.info("2차 검증(%s): %d건 (%s)", self._review, len(actionable),
                 ", ".join(f"{d.symbol} {d.action}" for d in actionable))
        verdicts = self._review_pass(market_context, actionable)
        return [self._apply_review(d, verdicts) for d in decisions]

    # ── 1차 판단 ────────────────────────────────────────────
    def _primary_pass(self, ctx: dict) -> list[Decision]:
        symbols = list(ctx.get("symbols", []))
        user_msg = (
            "다음은 현재 시장 상황이다. 각 대상 종목에 대해 submit_decision 을 한 번씩 호출하라.\n\n"
            + json.dumps(ctx, ensure_ascii=False, indent=2)
        )
        resp = self._client.messages.create(
            model=self._primary,
            max_tokens=2000,
            system=PRIMARY_SYSTEM,
            tools=[_decision_tool(symbols)],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_msg}],
        )
        by_symbol: dict[str, Decision] = {}
        for block in resp.content:
            if block.type != "tool_use":
                continue
            d = self._validate_decision(block.input or {}, symbols)
            if d is None or d.symbol in by_symbol:
                continue
            by_symbol[d.symbol] = d
        return list(by_symbol.values())

    # ── 2차 검증 ────────────────────────────────────────────
    def _review_pass(self, ctx: dict, actionable: list[Decision]) -> dict[str, dict]:
        symbols = [d.symbol for d in actionable]
        proposals = [
            {"symbol": d.symbol, "action": d.action, "quantity": d.quantity, "reason": d.reason}
            for d in actionable
        ]
        user_msg = (
            "시장 상황:\n" + json.dumps(ctx, ensure_ascii=False, indent=2)
            + "\n\n1차 엔진의 매매 제안(아래)을 각각 검증하라. 제안마다 submit_review 를 한 번씩 호출하라.\n"
            + json.dumps(proposals, ensure_ascii=False, indent=2)
        )
        resp = self._client.messages.create(
            model=self._review,
            max_tokens=2000,
            system=REVIEW_SYSTEM,
            tools=[_review_tool(symbols)],
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_msg}],
        )
        verdicts: dict[str, dict] = {}
        for block in resp.content:
            if block.type != "tool_use":
                continue
            inp = block.input or {}
            sym = str(inp.get("symbol", ""))
            if sym in symbols and sym not in verdicts:
                verdicts[sym] = inp
        return verdicts

    def _apply_review(self, d: Decision, verdicts: dict[str, dict]) -> Decision:
        if d.action == "HOLD":
            return d  # HOLD 는 검증 대상 아님

        v = verdicts.get(d.symbol)
        if v is None:  # 검증 누락 → 보수적으로 반려
            log.warning("• %s %s 검증 누락 → HOLD 강등", d.symbol, d.action)
            return Decision(d.symbol, "HOLD", 0, d.confidence, d.reason, reviewed=True,
                            review_note="검증 응답 누락(보수적 반려)")

        verdict = str(v.get("verdict", "REJECT")).upper()
        note = str(v.get("reason", ""))[:200]
        if verdict != "APPROVE":
            log.info("• %s %s → 반려(HOLD): %s", d.symbol, d.action, note)
            return Decision(d.symbol, "HOLD", 0, d.confidence, d.reason, reviewed=True, review_note=note)

        # 승인 — 수량은 줄이는 방향만 허용
        try:
            adj = int(v.get("adjusted_quantity", 0))
        except (TypeError, ValueError):
            adj = 0
        qty = min(adj, d.quantity) if adj > 0 else d.quantity
        if qty <= 0:
            return Decision(d.symbol, "HOLD", 0, d.confidence, d.reason, reviewed=True,
                            review_note=f"{note} (수량 0 → HOLD)")
        log.info("• %s %s 승인 (수량 %d→%d): %s", d.symbol, d.action, d.quantity, qty, note)
        return Decision(d.symbol, d.action, qty, d.confidence, d.reason, reviewed=True, review_note=note)

    # ── 검증 유틸 ───────────────────────────────────────────
    @staticmethod
    def _validate_decision(inp: dict, allowed_symbols: list[str]) -> Decision | None:
        symbol = str(inp.get("symbol", ""))
        if symbol not in allowed_symbols:
            log.warning("판단의 symbol 이 대상에 없음(무시): %r", symbol)
            return None
        action = str(inp.get("action", "HOLD")).upper()
        if action not in ("BUY", "SELL", "HOLD"):
            action = "HOLD"
        try:
            qty = max(0, int(inp.get("quantity", 0)))
        except (TypeError, ValueError):
            qty = 0
        try:
            conf = min(1.0, max(0.0, float(inp.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        reason = str(inp.get("reason", ""))[:200]
        if action == "HOLD":
            qty = 0
        return Decision(symbol=symbol, action=action, quantity=qty, confidence=conf, reason=reason)
