#!/usr/bin/env bash
# Ed25519 필수화(8c) 배포 전 서버 키 준비 (docs/DEPLOY.md "Ed25519 필수화 배포 순서" 2번).
# 운영 서버에서 root 또는 sudo 가능한 계정으로 한 번 실행한다 (Session Manager):
#   sudo bash /srv/clearing-hub/prepare-ed25519-keys.sh
# 하는 일:
#   - /srv/clearing-hub/.env     HUB_SIGNING_KEY(없을 때만 생성), HUB_PUBLIC_URL
#   - /srv/clearing-hub/sim.env  SIM_SIGNING_KEY(새로 생성), SIM_HUB_PUBLIC_KEY; SIM_HUB_SECRET·SIM_SIGNATURE_MODE 제거
#   - /srv/avengers-bank/.env    INTERBANK_SIGNING_KEY(없을 때만 생성), HUB_PUBLIC_KEY; HUB_SECRET·INTERBANK_SIGNATURE_MODE 제거
#   - /srv/clearing-hub/pubkeys.env  공개키·지문만 모아 둠 (관리자가 901·902 공개키 등록과 초대 문구에 쓴다)
# 개인키는 .env 파일에만 쓰고 화면에 내지 않는다. 바꾸기 전 파일은 *.bak-<시각>으로 남긴다.
# 다시 실행해도 안전하다: 이미 있는 HUB_SIGNING_KEY·INTERBANK_SIGNING_KEY는 그대로 두고, 가짜 은행 키만 새로 만든다
# (가짜 은행 키를 새로 만들면 902 공개키를 다시 등록해야 한다 — pubkeys.env의 SIM_PUB).
set -euo pipefail

HUB_ENV=/srv/clearing-hub/.env
SIM_ENV=/srv/clearing-hub/sim.env
AB_ENV=/srv/avengers-bank/.env
OUT=/srv/clearing-hub/pubkeys.env
PFX='\x30\x2e\x02\x01\x00\x30\x05\x06\x03\x2b\x65\x70\x04\x22\x04\x20'  # Ed25519 PKCS#8 DER 머리 (16바이트)

new_raw_key() { openssl genpkey -algorithm ed25519 -outform DER | tail -c 32 | base64 -w0; }
pub_of() { ( printf "$PFX"; echo "$1" | base64 -d ) | openssl pkey -inform DER -pubout -outform DER | tail -c 32 | base64 -w0; }
fp_of() { echo "$1" | base64 -d | sha256sum | cut -c1-16; }
ensure_line() {  # ensure_line <파일> <키> <값>  : 키가 없을 때만 추가
  grep -q "^$2=.\+" "$1" || echo "$2=$3" >> "$1"
}
drop_lines() {  # drop_lines <파일> <키>...
  local f=$1; shift
  for k in "$@"; do sed -i "/^$k=/d" "$f"; done
}

TS=$(date +%Y%m%d%H%M%S)
for f in "$HUB_ENV" "$SIM_ENV" "$AB_ENV"; do cp "$f" "$f.bak-$TS"; done

# 1. 허브 키 (있으면 유지 — 바꾸면 모든 은행이 허브 공개키를 다시 받아야 한다)
ensure_line "$HUB_ENV" HUB_SIGNING_KEY "$(new_raw_key)"
ensure_line "$HUB_ENV" HUB_PUBLIC_URL "https://clearing-hub.ai-agent-develop.cloud"
HUB_PUB=$(pub_of "$(grep '^HUB_SIGNING_KEY=' "$HUB_ENV" | cut -d= -f2-)")

# 2. 가짜 은행 키 (새로 만든다) + 허브 공개키
SIM_KEY=$(new_raw_key)
SIM_PUB=$(pub_of "$SIM_KEY")
drop_lines "$SIM_ENV" SIM_SIGNING_KEY SIM_HUB_PUBLIC_KEY SIM_HUB_SECRET SIM_SIGNATURE_MODE
printf 'SIM_SIGNING_KEY=%s\nSIM_HUB_PUBLIC_KEY=%s\n' "$SIM_KEY" "$HUB_PUB" >> "$SIM_ENV"
unset SIM_KEY

# 3. AvengersBank 키 (있으면 유지) + 허브 공개키, HMAC 설정 제거
ensure_line "$AB_ENV" INTERBANK_SIGNING_KEY "$(new_raw_key)"
AB_PUB=$(pub_of "$(grep '^INTERBANK_SIGNING_KEY=' "$AB_ENV" | cut -d= -f2-)")
drop_lines "$AB_ENV" HUB_PUBLIC_KEY HUB_SECRET INTERBANK_SIGNATURE_MODE
echo "HUB_PUBLIC_KEY=$HUB_PUB" >> "$AB_ENV"

# 4. 공개키·지문 보관 (비밀 아님)
printf 'HUB_PUB=%s\nHUB_FP=%s\nSIM_PUB=%s\nAB_PUB=%s\nAB_FP=%s\n' \
  "$HUB_PUB" "$(fp_of "$HUB_PUB")" "$SIM_PUB" "$AB_PUB" "$(fp_of "$AB_PUB")" > "$OUT"
chmod 600 "$HUB_ENV" "$SIM_ENV" "$AB_ENV"

echo "== 공개키·지문 ($OUT)"; cat "$OUT"
echo "== 설정 키 이름 확인 (값은 표시하지 않음)"
grep -oE '^(HUB_SIGNING_KEY|HUB_PUBLIC_URL)=' "$HUB_ENV"
grep -oE '^(SIM_SIGNING_KEY|SIM_HUB_PUBLIC_KEY|SIM_HUB_SECRET|SIM_SIGNATURE_MODE)=' "$SIM_ENV" || true
grep -oE '^(INTERBANK_SIGNING_KEY|HUB_PUBLIC_KEY|HUB_SECRET|INTERBANK_SIGNATURE_MODE|HUB_API_KEY|BANK_CODE)=' "$AB_ENV"
echo "완료. 다음: docs/DEPLOY.md 'Ed25519 필수화 배포 순서' 3번(main push)부터."
