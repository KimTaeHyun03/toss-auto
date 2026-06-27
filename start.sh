#!/bin/sh
# Cloudtype 단일 컨테이너 엔트리포인트.
# trader(자동매매 워커) + dashboard(모니터링 웹)를 함께 띄운다.
# 영속 볼륨을 /app/state, /app/logs 에 마운트하면 재시작해도 주문로그·상태가 유지된다.
set -e

# 영속 볼륨에 남은 단일 인스턴스 잠금 제거 (replica=1 전제; 하드 종료 잔여물 정리)
rm -f /app/state/trader.lock

# 1) 자동매매 루프 — 백그라운드
python trader.py &
TRADER_PID=$!

# 2) 모니터링 대시보드 — 포그라운드(외부 공개). 0.0.0.0 바인딩 필수.
export DASHBOARD_HOST=0.0.0.0
python dashboard.py &
DASH_PID=$!

# 둘 중 하나라도 죽으면 컨테이너를 종료해 Cloudtype 가 재시작하도록 한다.
wait -n 2>/dev/null || wait
kill "$TRADER_PID" "$DASH_PID" 2>/dev/null || true
exit 1
