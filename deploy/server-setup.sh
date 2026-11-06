#!/usr/bin/env bash
# 공용 배포 서버(deploy-server)에 clearing-hub를 처음 올릴 때 한 번 실행한다 (root, SSM Run Command 또는 Session Manager).
# - /srv/clearing-hub/docker-compose.yml: 허브(app, 256MB)와 가짜 은행(sim, 128MB). 같은 이미지, 명령만 다르다
# - 공용 PostgreSQL에 role·database 생성
# - /srv/clearing-hub/.env: DB 비밀번호·ADMIN_TOKEN을 서버 안에서 생성해 바로 쓴다 (밖으로 내보내지 않는다)
# - /srv/clearing-hub/.env의 HUB_SIGNING_KEY, sim.env의 SIM_SIGNING_KEY: Ed25519 개인키도 서버 안에서 만든다 (SPEC 2.6)
# - /srv/clearing-hub/sim.env: 가짜 은행 전용. 허브의 DB 주소·관리 토큰은 넣지 않는다. 902 공개키는 끝에 출력한다 (등록용)
# 다시 실행해도 안전하다: compose는 덮어쓰고, .env·sim.env가 이미 있으면 DB·비밀값은 건드리지 않는다.
set -euo pipefail

APP=clearing-hub
DB=clearing_hub
DIR=/srv/$APP

# Ed25519 키 (SPEC 2.6): raw 32바이트 base64. 개인키 = PKCS#8 DER의 끝 32바이트
ed25519_private() { openssl genpkey -algorithm ed25519 -outform DER | tail -c 32 | base64; }
ed25519_public_of() {  # 개인키(raw base64) → 공개키(raw base64). DER 머리 16바이트를 붙여 openssl에 넘긴다
  echo "$1" | base64 -d | (printf '\x30\x2e\x02\x01\x00\x30\x05\x06\x03\x2b\x65\x70\x04\x22\x04\x20'; cat) |
    openssl pkey -inform DER -pubout -outform DER | tail -c 32 | base64
}

mkdir -p "$DIR"
chown ubuntu:ubuntu "$DIR"

cat > "$DIR/docker-compose.yml" <<'EOF'
# deploy-app 스크립트가 ECR_REGISTRY / APP_NAME / IMAGE_TAG 를 넣어준다. pull·up -d가 두 서비스 모두에 적용된다.
services:
  app:
    image: ${ECR_REGISTRY}/${APP_NAME}:${IMAGE_TAG:-latest}
    container_name: ${APP_NAME}
    restart: unless-stopped
    env_file:
      - .env
    networks:
      - web
    expose:
      - "8000"
    deploy:
      resources:
        limits:
          memory: 256M
    healthcheck:
      # 베이스 이미지(uv slim)에 wget이 없으므로 파이썬 표준 라이브러리로 확인한다
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

  # 가짜 은행. Caddy에 올리지 않는다: web 네트워크 안에서 허브만 http://clearing-hub-sim:8100/interbank 로 부른다
  sim:
    image: ${ECR_REGISTRY}/${APP_NAME}:${IMAGE_TAG:-latest}
    container_name: ${APP_NAME}-sim
    restart: unless-stopped
    command: ["uvicorn", "bank_sim.main:app", "--host", "0.0.0.0", "--port", "8100"]
    env_file:
      - sim.env
    networks:
      - web
    expose:
      - "8100"
    deploy:
      resources:
        limits:
          memory: 128M
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8100/')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s

networks:
  web:
    external: true
EOF
chown ubuntu:ubuntu "$DIR/docker-compose.yml"
echo "compose 작성"

if [ -f "$DIR/.env" ]; then
  echo ".env가 이미 있어 DB·비밀값은 건드리지 않음"
else
  PW="$(openssl rand -hex 24)"   # hex는 URL에 그대로 넣어도 안전하다 (base64의 / + = 문제 회피)
  docker exec -i postgres psql -U postgres -v ON_ERROR_STOP=1 -q <<SQL
CREATE ROLE $DB WITH LOGIN PASSWORD '$PW';
CREATE DATABASE $DB OWNER $DB;
SQL
  echo "PostgreSQL role·database 생성: $DB"
  umask 077
  cat > "$DIR/.env" <<EOF
DATABASE_URL=postgresql+psycopg://$DB:$PW@postgres:5432/$DB
ADMIN_TOKEN=$(openssl rand -hex 32)
DB_ECHO=false
RECEIVER_TIMEOUT_SECONDS=5
RETRY_INTERVAL_SECONDS=60
RETRY_MAX_COUNT=10
MAX_TRANSFER_AMOUNT=100000000
HUB_SIGNING_KEY=$(ed25519_private)
EOF
  chown ubuntu:ubuntu "$DIR/.env"
  chmod 600 "$DIR/.env"
  echo ".env 작성 (값은 출력하지 않음)"
fi

if [ -f "$DIR/sim.env" ]; then
  echo "sim.env가 이미 있어 건드리지 않음"
else
  umask 077
  SIM_KEY="$(ed25519_private)"
  HUB_KEY="$(grep '^HUB_SIGNING_KEY=' "$DIR/.env" | cut -d= -f2- || true)"
  cat > "$DIR/sim.env" <<EOF
# 가짜 은행 Ed25519 개인키(필수). 공개키를 허브에 902로 등록한다 (docs/DEPLOY.md "가짜 은행 연결")
SIM_SIGNING_KEY=$SIM_KEY
# 허브 공개키 (GET /v1/hub-key). 비어 있으면 허브 서명 검증을 건너뛴다
SIM_HUB_PUBLIC_KEY=$([ -n "$HUB_KEY" ] && ed25519_public_of "$HUB_KEY")
SIM_DELAY_SECONDS=8
EOF
  echo "902 공개키(등록용): $(ed25519_public_of "$SIM_KEY")"
  unset SIM_KEY HUB_KEY
  chown ubuntu:ubuntu "$DIR/sim.env"
  chmod 600 "$DIR/sim.env"
  echo "sim.env 작성"
fi

ls -la "$DIR"
grep -o '^[A-Z_]*=' "$DIR/.env" "$DIR/sim.env"
