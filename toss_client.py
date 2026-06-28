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

import json
import logging
import os
import time
from pathlib import Path

import requests

from config import TOSS_BASE_URL

log = logging.getLogger("toss")

# 토큰 공유 파일(L2 캐시). trader 와 dashboard 가 같은 컨테이너/볼륨에서 같은 자격증명으로
# 각자 토큰을 발급하면, 토스는 클라이언트당 활성 토큰을 1개만 인정하므로 서로의 토큰을
# 무효화한다(상대 토큰이 만료 전인데 401 invalid-token). 한 파일을 공유해 활성 토큰을
# 하나로 수렴시킨다. /app/state 는 영속 볼륨(없으면 state/ 폴더).
_TOKEN_FILE = Path(
    os.getenv("TOSS_TOKEN_FILE", str(Path(__file__).parent / "state" / "toss_token.json"))
)


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
    def _read_token_file(self) -> dict | None:
        """공유 토큰 파일(L2)을 읽는다. 자격증명이 다르면 무시. 없으면 None."""
        try:
            d = json.loads(_TOKEN_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(d, dict) or d.get("client_id") != self._client_id:
            return None
        return d

    def _write_token_file(self, token: str, exp: float) -> None:
        """공유 토큰 파일에 원자적으로 기록(다른 프로세스가 주워 쓰도록)."""
        try:
            _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _TOKEN_FILE.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"token": token, "exp": exp, "client_id": self._client_id}),
                encoding="utf-8",
            )
            os.replace(tmp, _TOKEN_FILE)  # 원자적 교체
            try:
                os.chmod(_TOKEN_FILE, 0o600)  # 자격증명 준함 → 권한 축소
            except OSError:
                pass
        except OSError as e:
            log.warning("토큰 파일 기록 실패(무시): %s", e)

    def _issue_token(self) -> str:
        """토스에서 새 토큰을 발급받고 L1(메모리)+L2(파일)에 기록."""
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
        self._write_token_file(self._token, self._token_exp)
        log.info("액세스 토큰 발급 완료 (expires_in=%ss)", data.get("expires_in"))
        return self._token

    def _access_token(self) -> str:
        # 1) L1(메모리) 토큰이 만료 60초 전이면 그대로 사용
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        # 2) L2(공유 파일)에 유효 토큰이 있으면 채택 — 다른 프로세스가 갱신했을 수 있음
        d = self._read_token_file()
        if d and time.time() < float(d.get("exp", 0)) - 60:
            self._token = d["token"]
            self._token_exp = float(d["exp"])
            return self._token
        # 3) 둘 다 없음/만료 → 신규 발급
        return self._issue_token()

    def _recover_after_401(self) -> bool:
        """401(invalid-token) 후 토큰 회복. 파일에 *방금 실패한 것과 다른* 토큰이 있으면
        그걸 채택(재발급 안 함 → ping-pong 방지), 없으면 신규 발급. 회복 시 True."""
        failed = self._token
        d = self._read_token_file()
        if d and d.get("token") and d["token"] != failed:
            self._token = d["token"]
            self._token_exp = float(d.get("exp", 0))
            log.info("401 → 공유 파일의 새 토큰 채택")
            return True
        # 파일에도 실패한 토큰뿐(또는 없음) → 강제 신규 발급
        self._token = None
        try:
            self._issue_token()
        except TossError as e:
            log.warning("401 후 토큰 재발급 실패: %s", e)
            return False
        log.info("401 → 토큰 신규 발급")
        return True

    def _headers(self, account: bool = False) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self._access_token()}"}
        if account:
            if self.account_seq is None:
                raise TossError("account_seq 가 설정되지 않았습니다.")
            h["X-Tossinvest-Account"] = str(self.account_seq)
        return h

    def _request(self, method: str, path: str, *, account: bool = False,
                 params: dict | None = None, json: dict | None = None):
        """공통 요청. 401 이면 토큰 회복 후 1회 재시도(다른 프로세스의 토큰 무효화 대응)."""
        url = f"{TOSS_BASE_URL}{path}"
        r = self._s.request(
            method, url, headers=self._headers(account), params=params, json=json, timeout=10
        )
        if r.status_code == 401 and self._recover_after_401():
            r = self._s.request(
                method, url, headers=self._headers(account), params=params, json=json, timeout=10
            )
        return self._unwrap(r)

    def _get(self, path: str, *, account: bool = False, params: dict | None = None):
        return self._request("GET", path, account=account, params=params)

    def _post(self, path: str, *, account: bool = False, json: dict | None = None):
        return self._request("POST", path, account=account, json=json)

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

    def exchange_rate(self, base: str = "USD", quote: str = "KRW") -> float:
        """환율(base→quote). 다통화 계좌 자산을 한 통화로 환산할 때 사용. 실패 시 0."""
        res = self._get(
            "/api/v1/exchange-rate", params={"baseCurrency": base, "quoteCurrency": quote}
        ) or {}
        try:
            return float(res.get("rate", 0))
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
