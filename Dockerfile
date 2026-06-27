FROM python:3.13-slim

WORKDIR /app

# 의존성 먼저 설치(레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# state/logs 는 영속 볼륨 마운트 권장(없어도 동작하나 재시작 시 초기화)
RUN mkdir -p /app/state /app/logs && chmod +x start.sh

# 대시보드 포트(Cloudtype 에서 PORT 로 덮어쓸 수 있음)
ENV DASHBOARD_PORT=8787
EXPOSE 8787

CMD ["sh", "start.sh"]
