# 토스 자동매매 (Claude 판단 엔진)

토스증권 **공식 Open API**로 시세·장 상태·잔고를 가져오고, **Claude(`claude-opus-4-8`)** 가
코스피(프록시) 추세와 시장 상황을 보고 종목별 **BUY / SELL / HOLD** 를 판단해 주문하는 프로그램입니다.

> ⚠️ **실거래는 손실 위험이 있습니다.** 본 코드는 자동매매 *프레임워크*이며 수익을 보장하지 않습니다.
> `DRY_RUN=true`(기본값)로 충분히 검증한 뒤 실거래로 전환하세요.

## 중요한 전제

- **토스 API는 코스피 지수값 자체를 제공하지 않습니다.** "KOSPI"는 종목의 시장 구분 라벨로만 존재합니다.
  그래서 코스피 추세는 **KODEX 200 ETF(`069500`)** 를 프록시로 사용합니다 (`.env`의 `KOSPI_PROXY_SYMBOL`로 변경 가능).
- **장 상태**는 `/api/v1/market-calendar/KR` 의 통합(KRX+NXT) 운영시간으로 판단하며,
  프리마켓·정규장·애프터마켓 중 `TRADE_SESSIONS` 로 켠 세션에서 매매합니다 (VI 미반영).
- **토스 API에는 뉴스·등락률 순위·종목 검색이 없습니다.** 그래서 동적 종목 선정은
  Claude의 `web_search`로 웹을 직접 조사하고, 고른 코드는 토스 `/stocks`로 실재·상장·이름 검증을 거칩니다.

## 동적 종목 선정 (`DYNAMIC_UNIVERSE=true`)

```
[1일 1회] Claude(web_search) 가 오늘 뉴스·코스피·등락 조사 → 후보 코드 제시
   → 토스 /stocks 검증: 실재·상장(ACTIVE)·KR시장 + 이름 교차검증(다른 회사면 제거)
   → /stocks/{symbol}/warnings 로 투자경고·위험·과열·정리매매 종목 제외
   → state/universe_YYYYMMDD.json 에 캐시
[매주기] universe ∪ 보유종목 에 대해 Claude 가 BUY/SELL/HOLD 판단
```

> ⚠️ **뉴스 기반 개별종목 매매는 지수 ETF보다 훨씬 위험합니다.** 경고종목 필터는 *이미 지정된* 종목만
> 거르므로, 지정 전 급등주(작전·테마)는 못 막습니다. 반드시 `DRY_RUN=true`로 충분히 관찰 후,
> 소액·낮은 상한에서만 실거래로 전환하세요.

## 판단 흐름 (2단 검증)

```
[매 루프] 1차 판단 (DECISION_MODEL=Haiku, 저가)
   → 종목별 BUY / SELL / HOLD
        │
   HOLD → 그대로 통과 (상위 모델 호출 없음)
   BUY/SELL → 2차 검증 (REVIEW_MODEL=Opus)
        → APPROVE: 주문 진행 (수량은 줄이는 방향만 조정 가능)
        → REJECT : HOLD 로 강등 (주문 안 나감)
```

대부분의 판단이 HOLD라 **상위 모델(Opus)은 실제 매수/매도 후보가 생길 때만** 호출됩니다.
→ 평상시 비용은 1차 모델(Haiku) 수준, 실제 주문에만 상위 검증이 붙어 품질을 확보합니다.
`REVIEW_TRADES=false`로 끄면 1차 판단만으로 매매합니다.

## 구조

```
config.py        환경설정 + 안전장치 파라미터 (.env에서 로드)
toss_client.py   토스 Open API 클라이언트 (인증/시세/잔고/주문)
market.py        프리/정규/애프터마켓 개장 여부 판단
screener.py      동적 종목 선정 (Claude web_search 조사 → 검증/필터)
claude_engine.py 2단 판단 엔진 (Haiku 1차 판단 → 매수/매도만 Opus 검증)
risk.py          리스크 관리 (DRY_RUN·금액상한·일일횟수·손실한도 킬스위치, 상태 영속화)
trader.py        메인 루프
spec/openapi.json 토스 OpenAPI 원본 명세 (스펙 기준점)
```

## 설치 & 실행

```bash
# 1) 의존성
pip install -r requirements.txt

# 2) 자격증명 설정
#    토스증권 WTS 로그인 → 설정 > Open API 에서 client_id / client_secret 발급
cp .env.example .env       # Windows PowerShell: copy .env.example .env
#    .env 를 열어 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET / ANTHROPIC_API_KEY 채우기

# 3) 실행 (기본 DRY_RUN=true — 실제 주문 안 나감)
python trader.py
```

## 안전장치 (`.env`)

| 변수 | 의미 |
|------|------|
| `DRY_RUN` | `true`면 실제 주문 차단, 로그만. **검증 끝나기 전까지 true 유지** |
| `MAX_ORDER_KRW` | 1회 주문 금액 상한(원). 초과 주문 거부 |
| `MAX_TRADES_PER_DAY` | 하루 최대 주문 횟수 |
| `MAX_DAILY_LOSS_KRW` | 당일 손실이 이 값 넘으면 그날 매매 중단(킬스위치) |
| `LOOP_INTERVAL_SEC` | 장중 판단 주기(초) |
| `DECISION_MODEL` | 1차 판단 모델(매 루프). 기본 Haiku(저가) |
| `REVIEW_MODEL` | 2차 검증 모델. 1차가 BUY/SELL일 때만 호출. 기본 Opus |
| `REVIEW_TRADES` | `true`면 매수/매도를 상위 모델이 검증(반려 시 HOLD). `false`면 1차만 |
| `SCREENER_MODEL` | 종목 선정 모델. 기본 Opus |
| `DYNAMIC_UNIVERSE` | `true`면 Claude가 매일 뉴스/시장을 조사해 종목을 직접 선정. `false`면 `TRADE_SYMBOLS` 고정 |
| `MAX_UNIVERSE` | 동적 선정 시 하루 최대 후보 종목 수 |
| `SKIP_WARNED_STOCKS` | 투자경고/위험/과열/정리매매 종목 제외 |
| `TRADE_SYMBOLS` | 고정 매매 대상(쉼표 구분, 6자리). `DYNAMIC_UNIVERSE=false`일 때만 사용 |
| `TRADE_SESSIONS` | 매매 허용 세션: `pre`(프리)·`regular`(정규)·`after`(애프터). 통합(KRX+NXT) 운영시간을 API에서 매일 읽음. 정규장만 원하면 `regular` |

- 모든 주문에 **멱등키(clientOrderId)** 를 부여해, 타임아웃 후 재시도 시 중복 주문을 방지합니다.
- **미체결 주문 중복 방지** — 매 루프마다 `GET /orders`로 미체결(PENDING·부분체결) 주문을 확인하고,
  해당 종목은 새 매수/매도를 건너뜁니다 (지정가 미체결 상태에서 같은 종목에 주문이 쌓이거나 초과매도되는 것 방지).
- **손실 한도는 '계좌 자산 변동' 기준** — 보유종목 평가손익이 아니라 `현금 + 평가금액(equity)` 변화로 판단해,
  **손절로 실현한 손실·수수료·거래세까지 모두 포착**합니다 (손절 후 보유 0이 되어도 누적 손실을 놓치지 않음).
- **단일 인스턴스 잠금** (`state/trader.lock`) — 실수로 두 번 띄워도 이중 주문이 나가지 않습니다.
- 당일 상태(주문 횟수/킬스위치/시작자산)는 `state/state_YYYYMMDD.json` 에 저장되어 재시작해도 유지됩니다.
- 모든 주문은 `state/orders.csv` 에 누적 기록됩니다(전략 평가용 감사 로그). 판단·주문 로그는 `logs/`.

## 실거래로 전환할 때

1. `DRY_RUN=true` 로 며칠간 로그를 보며 판단·수량·금액이 합리적인지 확인
2. `MAX_ORDER_KRW` / `MAX_DAILY_LOSS_KRW` 를 작게 설정
3. `DRY_RUN=false` 로 변경 후 소액으로 시작

> 본 프로그램은 투자 자문이 아니며, 매매 결정과 그 결과(손익)에 대한 책임은 전적으로 사용자에게 있습니다.


