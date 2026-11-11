#!/usr/bin/env bash
# SimBank(902) 시연 화면 배포 전 서버 준비 (docs/SIMBANK_PLAN.md 4단계, docs/DEPLOY.md "SimBank 화면 배포").
# 운영 서버에서 root로 한 번 실행한다 (SSM):  sudo bash /srv/clearing-hub/prepare-simbank.sh
# 하는 일 (다시 실행해도 안전하다. 이미 된 것은 건너뛴다):
#   1. /srv/clearing-hub/sim-data 폴더 (SimBank 장부 SQLite, 컨테이너 /data)
#   2. docker-compose.yml의 sim 서비스에 볼륨 ./sim-data:/data
#   3. sim.env에 SIM_DB_PATH, SIM_HUB_URL, SIM_UI_PASSWORD(새로 생성), SIM_API_KEY(902 API 키 재발급)
#      - 902 API 키 재발급은 API 키 해시만 바꾼다. 902 공개키·서명 키·셀프 시험에는 영향이 없다
#      - 시연 비밀번호는 화면에 내지 않고 /srv/clearing-hub/simbank-password.txt(root만 읽기)에 둔다
#   4. Caddyfile에 simbank.ai-agent-develop.cloud 블록 (/interbank/*·/docs·/openapi.json은 404) + reload
#   5. 902 예치금·순채무 출력 (보내기가 902 한도를 쓰므로)
# 컨테이너는 다시 만들지 않는다. 바뀐 compose·sim.env는 다음 배포(main push → deploy-app)에 반영된다.
# 바꾸기 전 파일은 *.bak-<시각>으로 남긴다.
set -euo pipefail

DIR=/srv/clearing-hub
SIM_ENV=$DIR/sim.env
COMPOSE=$DIR/docker-compose.yml
CADDYFILE=/srv/caddy/Caddyfile
PASS_FILE=$DIR/simbank-password.txt
HUB=https://clearing-hub.ai-agent-develop.cloud
DOMAIN=simbank.ai-agent-develop.cloud
STAMP=$(date +%Y%m%d%H%M%S)

[ "$(id -u)" = 0 ] || { echo "root로 실행하세요"; exit 1; }
TOKEN=$(grep '^ADMIN_TOKEN=' "$DIR/.env" | cut -d= -f2-)
[ -n "$TOKEN" ] || { echo ".env에 ADMIN_TOKEN이 없습니다"; exit 1; }

echo "== 1. sim-data 폴더"
mkdir -p "$DIR/sim-data"
ls -ld "$DIR/sim-data"

echo "== 2. compose sim 볼륨"
if grep -q '\./sim-data:/data' "$COMPOSE"; then
  echo "이미 있음"
else
  cp -p "$COMPOSE" "$COMPOSE.bak-$STAMP"
  # sim 서비스의 env_file(- sim.env) 바로 뒤에 볼륨을 넣는다 (sim.env는 sim 서비스에만 있다)
  sed -i 's#^      - sim\.env$#      - sim.env\n    volumes:\n      - ./sim-data:/data#' "$COMPOSE"
  grep -q '\./sim-data:/data' "$COMPOSE" || { echo "compose 수정 실패"; cp -p "$COMPOSE.bak-$STAMP" "$COMPOSE"; exit 1; }
  echo "추가함 (백업 $COMPOSE.bak-$STAMP)"
fi
sed -n '/^  sim:/,/^    expose:/p' "$COMPOSE"

echo "== 3. sim.env"
cp -p "$SIM_ENV" "$SIM_ENV.bak-$STAMP"
ensure_line() { grep -q "^$1=.\+" "$SIM_ENV" || { sed -i "/^$1=/d" "$SIM_ENV"; echo "$1=$2" >> "$SIM_ENV"; echo "$1 추가"; }; }
ensure_line SIM_DB_PATH /data/simbank.db
ensure_line SIM_HUB_URL http://clearing-hub:8000
if ! grep -q '^SIM_UI_PASSWORD=.\+' "$SIM_ENV"; then
  PASS=$(openssl rand -hex 6)
  ensure_line SIM_UI_PASSWORD "$PASS"
  umask 077; echo "$PASS" > "$PASS_FILE"; unset PASS
  echo "시연 비밀번호를 $PASS_FILE 에 저장 (화면에 내지 않음)"
fi
if ! grep -q '^SIM_API_KEY=.\+' "$SIM_ENV"; then
  RES=$(curl -fsS -X POST "$HUB/admin/banks/902/keys" -H "X-Admin-Token: $TOKEN")
  KEY=$(printf '%s' "$RES" | python3 -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
  [ -n "$KEY" ] || { echo "902 API 키 재발급 실패"; exit 1; }
  ensure_line SIM_API_KEY "$KEY"; unset KEY RES
fi
grep -o '^[A-Z_]*=' "$SIM_ENV"

echo "== 4. Caddy"
if grep -q "^$DOMAIN" "$CADDYFILE"; then
  echo "이미 있음"
else
  cp -p "$CADDYFILE" "$CADDYFILE.bak-$STAMP"
  cat >> "$CADDYFILE" <<EOF

$DOMAIN {
    @hidden path /interbank/* /docs /docs/* /redoc /openapi.json
    respond @hidden 404
    reverse_proxy clearing-hub-sim:8100
}
EOF
  docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
  echo "추가하고 reload (백업 $CADDYFILE.bak-$STAMP)"
fi

echo "== 5. 902 예치금"
curl -fsS "$HUB/admin/reserve" -H "X-Admin-Token: $TOKEN" \
  | python3 -c 'import json,sys; [print(r) for r in json.load(sys.stdin) if r.get("bank_code") == "902"]'
echo "완료. 다음: main push → deploy-app이 sim 컨테이너를 새 이미지·볼륨·sim.env로 다시 만든다"
