"""동적 종목 선정 (Claude + web_search).

토스 API는 뉴스·등락률 순위·검색을 제공하지 않으므로, Claude 의 web_search 도구로
오늘 한국시장을 직접 조사해 매매 후보를 고른다.

설계(검토 반영):
  1) 조사: web_search(tool_choice auto)로 뉴스/코스피/주요 등락을 텍스트로 정리
  2) 추출: 그 텍스트를 output_config.format(구조화)으로 {symbol,name,reason} 배열화
     → 강제 tool 과 server-tool 의 모호한 상호작용을 피하려 호출을 둘로 분리
  3) 검증: 토스 /stocks 로 실재·상장(ACTIVE)·KR시장 확인 + 이름 교차검증(코드가 실재해도
     의도한 회사와 다를 수 있으므로 스크리너가 말한 이름과 대조)
  4) 필터: /stocks/{symbol}/warnings 로 투자경고·위험·과열·정리매매 종목 제외
  5) 캐시: 그날 universe 를 state/universe_YYYYMMDD.json 에 저장(1일 1회 선정)
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

log = logging.getLogger("screener")
KST = ZoneInfo("Asia/Seoul")
STATE_DIR = Path(__file__).parent / "state"

KR_MARKETS = {"KOSPI", "KOSDAQ", "KR_ETC"}
# 매수를 피해야 할 지정 사유 (VI_* 는 일시적 변동성완화라 제외 대상에서 뺀다)
DANGER_WARNINGS = {"INVESTMENT_WARNING", "INVESTMENT_RISK", "OVERHEATED", "LIQUIDATION_TRADING"}

RESEARCH_SYSTEM = """\
너는 한국 주식시장(KRX: 코스피/코스닥) 리서치 보조원이다.
web_search 도구로 '오늘' 한국 시장 상황을 조사한다: 코스피·코스닥 지수 흐름,
주요 뉴스(실적/공시/정책/업종), 거래대금·등락률 상위 종목 등.
조사 결과를 바탕으로 단기 매매 후보가 될 만한 KRX 상장 종목을 추린다.
각 종목에 대해 '한국어 종목명'과 'KRX 6자리 종목코드', 선정 사유를 함께 적는다.
확실하지 않으면 적게 추려라. 관리종목·정리매매·테마성 급등주는 신중히 다뤄라.
마지막에 후보들을 '종목명(코드): 사유' 형식 목록으로 정리한다."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "KRX 6자리 종목코드"},
                    "name": {"type": "string", "description": "한국어 종목명"},
                    "reason": {"type": "string", "description": "선정 사유 한 줄"},
                },
                "required": ["symbol", "name", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

_CODE_RE = re.compile(r"^\d{6}$")


def _norm(s: str) -> str:
    """이름 비교용 정규화: 공백/괄호/특수문자 제거, 소문자."""
    return re.sub(r"[\s()\[\]·.,\-/]", "", str(s)).lower()


class ClaudeScreener:
    def __init__(self, api_key: str, model: str, max_universe: int, skip_warned: bool):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self.max_universe = max_universe
        self.skip_warned = skip_warned
        STATE_DIR.mkdir(exist_ok=True)

    # ── 캐시 (1일 1회 선정) ─────────────────────────────────
    def _cache_path(self) -> Path:
        return STATE_DIR / f"universe_{datetime.now(KST):%Y%m%d}.json"

    def load_today(self) -> list[dict] | None:
        p = self._cache_path()
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def _save_today(self, universe: list[dict]) -> None:
        self._cache_path().write_text(
            json.dumps(universe, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 1) 조사 ─────────────────────────────────────────────
    def _research(self) -> str:
        messages = [
            {
                "role": "user",
                "content": (
                    f"오늘은 {datetime.now(KST):%Y-%m-%d} 이다. 위 지침대로 한국시장을 web_search 로 "
                    "조사하고, 단기 매매 후보 종목을 '종목명(6자리코드): 사유' 목록으로 정리하라."
                ),
            }
        ]
        resp = None
        for _ in range(6):  # server-tool 루프가 pause_turn 으로 끊기면 이어받기
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=4000,
                system=RESEARCH_SYSTEM,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 6}],
                messages=messages,
            )
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        return "".join(b.text for b in (resp.content if resp else []) if b.type == "text")

    # ── 2) 추출 ─────────────────────────────────────────────
    def _extract(self, findings: str) -> list[dict]:
        if not findings.strip():
            return []
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1500,
            output_config={"format": {"type": "json_schema", "schema": EXTRACT_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "다음 한국시장 리서치 요약에서 매매 후보 종목을 추출하라. "
                        "KRX 6자리 코드와 한국어 종목명을 정확히 기입하라.\n\n" + findings
                    ),
                }
            ],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning("후보 추출 JSON 파싱 실패")
            return []
        return data.get("candidates", []) or []

    # ── 3·4) 검증 + 필터 ────────────────────────────────────
    def _validate(self, toss, candidates: list[dict]) -> list[dict]:
        # 코드 형식 + 중복 제거
        seen: dict[str, dict] = {}
        for c in candidates:
            sym = str(c.get("symbol", "")).strip()
            if _CODE_RE.match(sym) and sym not in seen:
                seen[sym] = {"symbol": sym, "name": str(c.get("name", "")), "reason": str(c.get("reason", ""))[:200]}
        if not seen:
            return []

        # 토스 /stocks 로 실재/상장/시장 확인
        info = {s["symbol"]: s for s in toss.stocks(list(seen.keys())) if isinstance(s, dict)}

        validated: list[dict] = []
        for sym, cand in seen.items():
            si = info.get(sym)
            if not si:
                log.info("후보 제외(토스에 없음): %s %s", sym, cand["name"])
                continue
            if si.get("status") != "ACTIVE" or si.get("market") not in KR_MARKETS:
                log.info("후보 제외(비활성/비KR): %s status=%s market=%s", sym, si.get("status"), si.get("market"))
                continue
            # 이름 교차검증 — 코드가 실재해도 의도한 회사와 다르면 제거
            actual = _norm(si.get("name", ""))
            claimed = _norm(cand["name"])
            if claimed and actual and claimed not in actual and actual not in claimed:
                log.warning("후보 제외(이름 불일치): %s 주장=%s 실제=%s", sym, cand["name"], si.get("name"))
                continue
            cand["name"] = si.get("name", cand["name"])  # 실제 종목명으로 교정

            # 경고종목 필터
            if self.skip_warned:
                warns = {w.get("warningType") for w in toss.stock_warnings(sym)}
                hit = warns & DANGER_WARNINGS
                if hit:
                    log.info("후보 제외(경고종목 %s): %s %s", sorted(hit), sym, cand["name"])
                    continue
            validated.append(cand)
            if len(validated) >= self.max_universe:
                break
        return validated

    # ── 진입점 ──────────────────────────────────────────────
    def select(self, toss, force: bool = False) -> list[dict]:
        """오늘의 매매 universe 반환(캐시 우선). [{symbol,name,reason}, ...]"""
        if not force:
            cached = self.load_today()
            if cached is not None:
                log.info("오늘 universe 캐시 사용 (%d종목)", len(cached))
                return cached

        log.info("동적 종목 선정 시작 (web_search 조사)...")
        findings = self._research()
        candidates = self._extract(findings)
        log.info("후보 %d개 추출 → 검증/필터 중", len(candidates))
        universe = self._validate(toss, candidates)
        log.info(
            "오늘 universe 확정: %s",
            ", ".join(f"{u['name']}({u['symbol']})" for u in universe) or "(없음)",
        )
        self._save_today(universe)
        return universe
