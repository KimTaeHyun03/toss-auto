# Cloudtype 배포 가이드

토스 자동매매 봇을 Cloudtype에 배포합니다. **trader(자동매매) + dashboard(모니터링)** 를 한 컨테이너에서 실행하고, 영속 볼륨에 로그를 남깁니다.

## 구성 요약
- **Dockerfile** 기반 배포 (`start.sh` 가 trader + dashboard 동시 실행)
- **replica = 1** (필수 — 2개 이상이면 이중 주문/로그 꼬임)
- **영속 볼륨** 2개: `/app/state`, `/app/logs`
- **DRY_RUN=true** 유지 (모의매매 — 실제 주문 안 나감)

## 1) GitHub 푸시
`.env` 는 절대 커밋 금지(`.gitignore`로 제외됨). 시크릿은 Cloudtype 환경변수로만 넣습니다.
```
git add -A && git commit -m "deploy" && git push
```

## 2) Cloudtype 앱 생성
1. Cloudtype → **새 프로젝트 → GitHub 저장소 연결**
2. 빌드 방식: **Dockerfile** 선택
3. **포트**: `8787` (대시보드)
4. **인스턴스 수(replica): 1** ← 반드시

## 3) 환경변수 등록 (Cloudtype 콘솔)
아래 값을 채웁니다. 시크릿(★)은 본인 `.env` 값을 사용하세요.

| 변수 | 값 | 비고 |
|---|---|---|
| `TOSS_CLIENT_ID` | ★ | 토스 라이브 client_id |
| `TOSS_CLIENT_SECRET` | ★ | 토스 라이브 client_secret |
| `ANTHROPIC_API_KEY` | ★ | Claude API 키 |
| `DRY_RUN` | `true` | **모의매매 유지 필수** |
| `DASHBOARD_TOKEN` | ★(임의 난수) | 대시보드 외부접속 보호 |
| `DYNAMIC_UNIVERSE` | `true` | Claude 종목 선정 |
| `TRADE_SESSIONS` | `pre,regular,after` | 프리~애프터 |
| `LOOP_INTERVAL_SEC` | `300` | 5분 주기 |
| `MAX_ORDER_KRW` | `100000` | 1회 주문 상한 |
| `MAX_TRADES_PER_DAY` | `5` | 일일 주문 횟수 |
| `MAX_DAILY_LOSS_KRW` | `50000` | 일일 손실 한도(킬스위치) |
| `REVIEW_TRADES` | `true`/`false` | false면 opus 검증 생략(비용↓) |

## 4) 볼륨 마운트 (로그 영속화)
재시작/재배포 시 `orders.csv`·상태가 날아가지 않게:
- 볼륨 1 → 마운트 경로 `/app/state`
- 볼륨 2 → 마운트 경로 `/app/logs`

## 5) 배포 후 확인
- 대시보드: `https://<할당된주소>/?token=<DASHBOARD_TOKEN>` (이후 쿠키로 유지)
- 토요일/휴장일엔 "🔴 휴장일" 표시, 거래 없음
- 다음 거래일 **프리마켓 08:00 KST 부터** 자동 매매 판단 시작

## 보안·주의
- 토스 자격증명은 **라이브(실계좌)** 입니다. `DRY_RUN=false` 로 바꾸면 **실제 주문**이 나갑니다. 충분히 검증 전까지 절대 바꾸지 마세요.
- `DASHBOARD_TOKEN` 없이 공개하면 누구나 계좌 잔고를 봅니다 — 반드시 설정.
- 한 컨테이너에서 두 프로세스가 도므로 trader가 죽으면 `start.sh`가 컨테이너를 종료해 Cloudtype가 재시작합니다.

## 비용(대략)
거래일 1일 기준 Claude API ≈ **$2~5** (모델·검증빈도에 따라). 자세한 산정은 대화 참고. `REVIEW_TRADES=false` + `LOOP_INTERVAL_SEC` 확대로 절감 가능.
