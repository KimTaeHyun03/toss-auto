"""토스증권 Open API 클라이언트.

스펙 기준(openapi.json v1.1.5, spec/openapi.json 에 원본 보관):
  base                = https://openapi.tossinvest.com
  토큰 발급           POST /oauth2/token              (form: grant_type, client_id, client_secret)
  현재가              GET  /api/v1/prices?symbols=...
  캔들                GET  /api/v1/candles?symbol=&interval=1m|1d&count=
  국내 장 운영시간    GET  /api/v1/market-calendar/KR
  계좌 목록           GET  /api/v1/accounts
  보유주식            GET  /api/v1/holdings           (X-Tossinvest-Account)
  매수가능금액        GET  /api/v1/buying-power        (X-Tossinvest-Account)
  매도가능수량        GET  /api/v1/sellable-quantity?symbol=  (X-Tossinvest-Account)
  주문 생성           POST /api/v1/orders             (X-Tossinvest-Account)
  주문 취소           POST /api/v1/orders/{id}/cancel
  주문 정정           POST /api/v1/orders/{id}/modify

응답은 보통 {"result": ...} 봉투(envelope)에 담깁니다.
"""
from __future__ import annotations

import logging
import time

import requests

from config import TOSS_BASE_URL

log = logging.getLogger("toss")


class TossError(RuntimeError):
    pass


class TossClient:
    def __init__(self, client_id: str, client_secret: str, account_seq: int | None = None):
        self._client_id = client_id
        self._client_secret = client_secret
        self.account_seq = account_seq
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._s = requests.Session()

    # ── 인증 ────────────────────────────────────────────────
    def _access_token(self) -> str:
        # 만료 60초 전이면 갱신
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self._s.post(
            f"{TOSS_BASE_URL}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=10,
        )
        if r.status_code != 200:
            raise TossError(f"토큰 발급 실패 {r.status_code}: {r.text}")
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600))
        log.info("액세스 토큰 발급 완료 (expires_in=%ss)", data.get("expires_in"))
        return self._token

    def _headers(self, account: bool = False) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._access_token()}"}
        if account:
            if self.account_seq is None:
                raise TossError("account_seq 가 설정되지 않았습니다.")
            h["X-Tossinvest-Account"] = str(self.account_seq)
        return h

    def _get(self, path: str, *, account: bool = False, params: dict | None = None):
        r = self._s.get(
            f"{TOSS_BASE_URL}{path}", headers=self._headers(account), params=params, timeout=10
        )
        return self._unwrap(r)

    def _post(self, path: str, *, account: bool = False, json: dict | None = None):
        r = self._s.post(
            f"{TOSS_BASE_URL}{path}", headers=self._headers(account), json=json, timeout=10
        )
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r: requests.Response):
        if r.status_code >= 400:
            raise TossError(f"{r.request.method} {r.url} → {r.status_code}: {r.text}")
        if not r.content:
            return None
        body = r.json()
        return body.get("result", body) if isinstance(body, dict) else body

    # ── 시세 / 장 상태 ──────────────────────────────────────
    def prices(self, symbols: list[str]) -> dict[str, float]:
        """현재가 조회. {symbol: lastPrice(float)} 반환."""
        res = self._get("/api/v1/prices", params={"symbols": ",".join(symbols)})
        out: dict[str, float] = {}
        for row in res or []:
            try:
                out[row["symbol"]] = float(row["lastPrice"])
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def orderbook(self, symbol: str) -> dict:
        """호가창 조회. {asks:[{price,volume}...](낮은가격순), bids:[...](높은가격순)}.

        asks[0]=최우선 매도호가(최저), bids[0]=최우선 매수호가(최고).
        호가 가격은 거래소가 주는 값이라 항상 유효 호가단위(틱)이다.
        """
        return self._get("/api/v1/orderbook", params={"symbol": symbol}) or {}

    def candles(self, symbol: str, interval: str = "1d", count: int = 20) -> list[dict]:
        """캔들 조회. interval: '1m' | '1d'. 결과는 {candles:[...]} 래퍼라 풀어서 반환."""
        res = self._get(
            "/api/v1/candles",
            params={"symbol": symbol, "interval": interval, "count": count, "adjusted": "true"},
        ) or {}
        return res.get("candles", [])

    def market_calendar_kr(self) -> dict:
        return self._get("/api/v1/market-calendar/KR") or {}

    def stocks(self, symbols: list[str]) -> list[dict]:
        """종목 기본정보 조회. StockInfo 리스트(symbol,name,market,status,securityType...)."""
        if not symbols:
            return []
        res = self._get("/api/v1/stocks", params={"symbols": ",".join(symbols)})
        return res or []

    def stock_warnings(self, symbol: str) -> list[dict]:
        """매수 유의사항(경고종목) 조회. StockWarning 리스트."""
        try:
            res = self._get(f"/api/v1/stocks/{symbol}/warnings")
        except TossError:
            return []
        return res or []

    # ── 계좌 / 잔고 ─────────────────────────────────────────
    def accounts(self) -> list[dict]:
        return self._get("/api/v1/accounts") or []

    def resolve_account_seq(self) -> int:
        """account_seq 미설정 시 첫 위탁(BROKERAGE)계좌를 자동 선택."""
        if self.account_seq is not None:
            return self.account_seq
        accs = self.accounts()
        if not accs:
            raise TossError("조회된 계좌가 없습니다.")
        brokerage = [a for a in accs if a.get("accountType") == "BROKERAGE"]
        chosen = (brokerage or accs)[0]
        self.account_seq = int(chosen["accountSeq"])
        log.info("계좌 자동 선택: accountNo=%s seq=%s", chosen.get("accountNo"), self.account_seq)
        return self.account_seq

    def holdings(self) -> dict:
        return self._get("/api/v1/holdings", account=True) or {}

    def buying_power(self, currency: str = "KRW") -> float:
        # currency 는 필수 쿼리 파라미터(KRW|USD). 누락 시 400(invalid-request, field=currency).
        res = self._get("/api/v1/buying-power", account=True, params={"currency": currency}) or {}
        try:
            return float(res.get("cashBuyingPower", 0))
        except (TypeError, ValueError):
            return 0.0

    def sellable_quantity(self, symbol: str) -> float:
        res = self._get("/api/v1/sellable-quantity", account=True, params={"symbol": symbol}) or {}
        # 스키마상 수량 필드명이 환경에 따라 다를 수 있어 방어적으로 탐색
        for k in ("sellableQuantity", "quantity"):
            if k in res:
                try:
                    return float(res[k])
                except (TypeError, ValueError):
                    pass
        return 0.0

    # ── 주문 ────────────────────────────────────────────────
    def create_order(
        self,
        *,
        symbol: str,
        side: str,            # "BUY" | "SELL"
        order_type: str,      # "LIMIT" | "MARKET"
        quantity: int,
        price: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """주문 생성. 멱등키(clientOrderId)로 타임아웃 재시도 시 중복주문을 방지한다."""
        body: dict = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "quantity": str(quantity),
        }
        if order_type == "LIMIT":
            if price is None:
                raise TossError("LIMIT 주문에는 price 가 필요합니다.")
            # 호가단위는 정수원 → 정수면 "10020.0" 가 아닌 "10020" 으로 보낸다.
            body["price"] = str(int(price)) if float(price).is_integer() else str(price)
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._post("/api/v1/orders", account=True, json=body) or {}

    def cancel_order(self, order_id: str) -> dict:
        return self._post(f"/api/v1/orders/{order_id}/cancel", account=True) or {}

    # 아직 체결되지 않고 호가창에 남아있는(또는 취소/정정 대기) 상태들
    OPEN_STATUSES = {"PENDING", "PENDING_CANCEL", "PENDING_REPLACE", "PARTIAL_FILLED"}

    def open_orders(self) -> list[dict]:
        """미체결(대기/부분체결) 주문 목록. 주문 중복 적재 방지에 사용.

        status 는 필수 쿼리(OPEN|CLOSED) — 누락 시 400(field=status).
        OPEN 으로 받은 뒤 세부 상태(OPEN_STATUSES)로 한 번 더 거른다(방어).
        """
        res = self._get("/api/v1/orders", account=True, params={"status": "OPEN"}) or {}
        orders = res.get("orders", []) if isinstance(res, dict) else (res or [])
        return [o for o in orders if o.get("status") in self.OPEN_STATUSES]
