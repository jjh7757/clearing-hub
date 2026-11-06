#!/usr/bin/env bash
# 허브 8c 배포 직후 서버에서 한 번 실행 (docs/DEPLOY.md "Ed25519 필수화 배포 순서" 1·3·4번, "정산 배포 순서" 3·5번):
#   sudo bash /srv/clearing-hub/after-deploy-8c.sh
# 하는 일 (prepare-ed25519-keys.sh가 만든 /srv/clearing-hub/pubkeys.env를 쓴다):
#   1. 901(AvengersBank)·902(가짜 은행) 공개키를 허브에 등록
#   2. 가짜 은행 컨테이너를 같은 태그로 다시 만들어 sim.env(새 키)를 반영
#   3. 901·902 순채무한도를 새 은행 기본값(10억)과 같게
#   4. ping 두 개, hub-key 지문 확인
#   5. (선택) 개시 정산: 배포 전 거래를 run 하나로 모아 확정
set -euo pipefail

HUB=https://clearing-hub.ai-agent-develop.cloud
TOKEN=$(grep '^ADMIN_TOKEN=' /srv/clearing-hub/.env | cut -d= -f2-)
# shellcheck disable=SC1091
source /srv/clearing-hub/pubkeys.env
H=(-H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json")

echo "== 허브 버전"; curl -s "$HUB/"; echo
echo "== 허브 공개키 (지문이 $HUB_FP 인지)"; curl -s "$HUB/v1/hub-key"; echo

echo "== 1. 공개키 등록"
curl -s -X POST "$HUB/admin/banks/901/public-key" "${H[@]}" -d "{\"public_key\":\"$AB_PUB\"}"; echo
curl -s -X POST "$HUB/admin/banks/902/public-key" "${H[@]}" -d "{\"public_key\":\"$SIM_PUB\"}"; echo

echo "== 2. 가짜 은행 컨테이너 재생성 (sim.env 반영)"
/usr/local/bin/deploy-app clearing-hub "$(docker inspect -f '{{.Config.Image}}' clearing-hub | cut -d: -f2)"

echo "== 3. 기존 은행 순채무한도 10억"
curl -s -X PATCH "$HUB/admin/banks/901" "${H[@]}" -d '{"net_debit_cap":1000000000}'; echo
curl -s -X PATCH "$HUB/admin/banks/902" "${H[@]}" -d '{"net_debit_cap":1000000000}'; echo

echo "== 4. ping (둘 다 ok:true 여야 한다. 901은 AvengersBank 8d 컨테이너가 새 .env로 뜬 뒤에)"
curl -s -X POST "$HUB/admin/banks/902/ping" -H "X-Admin-Token: $TOKEN"; echo
curl -s -X POST "$HUB/admin/banks/901/ping" -H "X-Admin-Token: $TOKEN"; echo
echo "== 예치금·한도"; curl -s "$HUB/admin/reserve" -H "X-Admin-Token: $TOKEN"; echo

if [ "${1:-}" = "--settle" ]; then
  echo "== 5. 개시 정산"
  RUN=$(curl -s -X POST "$HUB/admin/settlements/run" "${H[@]}" -d "{\"settle_date\":\"$(TZ=Asia/Seoul date +%F)\"}" | sed -n 's/.*"run_id":\([0-9]*\).*/\1/p')
  echo "run_id=$RUN"
  curl -s -X POST "$HUB/admin/settlements/$RUN/confirm" "${H[@]}" -d '{"reason":"개시 정산 (8c 배포)"}'; echo
fi
echo "완료."
