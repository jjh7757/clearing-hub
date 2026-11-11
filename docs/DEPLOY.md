# 배포 (AWS)

AvengersBank와 같은 공용 배포 서버에 올린다. 환경 구성·설계 이유는 TIL 저장소의 `AWS배포환경/`(README, 새앱배포절차, DB환경구성)을 따른다.

- 주소: https://clearing-hub.ai-agent-develop.cloud (API 문서 `/docs`)
- 흐름: `main`에 push → GitHub Actions(arm64)가 **테스트** → 이미지 빌드 → ECR → SSM으로 서버의 `deploy-app clearing-hub <커밋>` 실행
- 서버: EC2 `t4g.small`(2GB, 공용), `/srv/clearing-hub`, 공용 PostgreSQL(`clearing_hub` db·role), Caddy가 HTTPS
- 컨테이너 두 개, 이미지 하나:

| 컨테이너 | 명령 | 포트 | 메모리 상한 | 공개 |
|---|---|---|---|---|
| `clearing-hub` | `alembic upgrade head` → `uvicorn clearing_hub.main:app` | 8000 | 256MB | Caddy로 HTTPS |
| `clearing-hub-sim` | `uvicorn bank_sim.main:app` (가짜 은행) | 8100 | 128MB | 안 함. `web` 네트워크 안에서 허브만 부른다 |

- 로컬 측정 (서버와 같은 compose, 이체 몇 건 뒤): 허브 75MB, 가짜 은행 37MB
- 허브 워커는 1개만 띄운다. 재시도 태스크가 프로세스 안에서 돌기 때문에 여러 개면 같은 거래를 겹쳐 확인한다
- 가짜 은행은 메모리에만 저장한다. 재배포하면 입금 기록이 사라진다 (허브 DB의 거래 기록은 남는다)

| 파일 | 역할 |
|---|---|
| `Dockerfile` | uv(Python 3.13)에 의존성(`--no-dev`). 기본 명령은 허브, compose에서 가짜 은행 명령으로 바꾼다 |
| `.github/workflows/deploy.yml` | TIL `templates/deploy.yml` 기반. 브랜치 `main`, 빌드 전에 `pytest` |
| `deploy/server-setup.sh` | 서버 최초 1회: compose, DB role·database, `.env`·`sim.env`(비밀값을 서버 안에서 생성) |
| `alembic/versions/ef6bf3b35409_init.py` | 초기 테이블 (`banks`, `transfers`, `transfer_events`) |
| `alembic/versions/c7e1a9d4f2b6_reserve_ledger.py`, `d8f2b3a6e1c9_settlement_runs.py` | 정산: 예치금 원장(`reserve_accounts`·`reserve_ledger`·`banks.net_debit_cap`), 정산 run(`settlement_*`·`transfers.settlement_run_id`) |
| `alembic/versions/e5a1c7f3d9b2_nonrepudiation.py` | 부인방지: `banks` 공개키 칸, `transfer_evidence` (8a의 `signature_mode`는 다음 리비전에서 지움) |
| `alembic/versions/f3b8d2a7c4e1_ed25519_only_self_register.py` | 8c: `banks.hmac_secret`·`signature_mode` 삭제, 셀프 등록 `invites` |

## 처음 한 번

순서대로 한다. 1·2를 끝내기 전에 `main`에 push하면 워크플로가 AWS 단계에서 실패한다 (테스트까지만 돈다). 그때는 다 끝낸 뒤 Actions에서 `Run workflow`로 다시 돌린다.

- [x] 1. ECR 리포지토리 `clearing-hub` (프라이빗, **Mutable**), 수명 주기 규칙: 이미지 10개 초과 시 만료
- [x] 2. IAM 역할 `github-actions-deploy` 신뢰 정책 `sub` 목록에 추가:
  ```
  "repo:jjh7757@307797437/clearing-hub@1446511280:*"
  ```
  (저장소는 퍼블릭이라 arm64 러너가 무료다. 비공개로 바꾸면 안 된다)
- [x] 3. 서버에서 `deploy/server-setup.sh` 실행 (SSM 또는 Session Manager, root) → `/srv/clearing-hub/{docker-compose.yml,.env,sim.env}`, PostgreSQL `clearing_hub`
- [x] 4. 가비아 DNS: A `clearing-hub` → `13.209.128.48`, **전파 확인 후** 다음 단계
  ```bash
  nslookup clearing-hub.ai-agent-develop.cloud 8.8.8.8
  ```
- [x] 5. `/srv/caddy/Caddyfile`에 블록 추가 후 reload. reload 뒤 실제 적용을 Caddy 로그(`docker logs caddy`의 `certificate obtained successfully`)로 확인한다 (AvengersBank 때 첫 reload가 적용되지 않았다. 컨테이너 안에서 `localhost:2019` 관리 API는 연결이 거부됐다)
  ```
  clearing-hub.ai-agent-develop.cloud {
      reverse_proxy clearing-hub:8000
  }
  ```
- [x] 6. `main`에 push → Actions 초록불 → 아래 "확인"
- [x] 7. 가짜 은행 등록과 secret 연결 (아래 "가짜 은행 연결")
- [x] 8. `/srv/monitor.sh`에 추가 (CRLF가 섞이지 않게):
  ```
  check_url "clearing-hub" "https://clearing-hub.ai-agent-develop.cloud"
  check_container_health "clearing-hub"
  check_container_health "clearing-hub-sim"
  ```

## 첫 배포 (2026-11-05)

- 커밋 `9caebda`: Actions 전 단계 성공(테스트 14개 포함) → `clearing-hub`·`clearing-hub-sim` 모두 `healthy`, 이미지 태그 = 커밋, 마이그레이션 `ef6bf3b35409` 적용
- 메모리: 허브 83MB / 256MB, 가짜 은행 38MB / 128MB (배포 전 서버 여유 약 770MB)
- HTTPS `/` 200, Let's Encrypt 인증서 발급
- 가짜 은행 902 등록, `sim.env`에 secret 연결 후 재생성 → `ping` 200 (`holder_name` 김철수), 서명 없는 호출은 401
- `monitor.sh`에 3줄 추가(백업 `monitor.sh.bak-*`), 실행 결과 `ok`
- 걸린 것: 로컬(Windows, Docker Desktop)에는 arm64 에뮬레이션이 없어 이미지를 직접 빌드할 수 없다. 배포는 Actions로만 한다

## 정산·Ed25519 배포 (2026-11-06)

`feature/settlement`(1~11단계)를 `main`에 fast-forward 병합한 커밋 `da0e1f8`을 배포했다. 순서는 "Ed25519 필수화 배포 순서"와 "정산 배포 순서"를 합친 것이고, 서버 쪽은 `deploy/prepare-ed25519-keys.sh`(배포 전)·`deploy/after-deploy-8c.sh --settle`(배포 직후)로 했다.

- 배포 전: 서버 안에서 허브 키·가짜 은행 키·AvengersBank 키 생성, `.env`·`sim.env`·AvengersBank `.env`에 기록(백업 `*.bak-20261106*`), HMAC 줄 삭제. 공개키·지문은 `/srv/clearing-hub/pubkeys.env`. 허브 지문 `37021b33778cec72`, 901 지문 `dd7ceee7877057e8`, 902 지문 `ed9d08addf00c1c3`
- Actions 42183910566 성공(테스트 267개) → 두 컨테이너 `healthy`, 태그 = `da0e1f8`, 마이그레이션 `b7e3f1a9c2d4`(head), `/` → `"spec": "v1.2"`, `GET /v1/hub-key` 지문 일치
- 배포 직후: 901·902 공개키 등록, 가짜 은행 재생성, 901·902 순채무한도 10억, 902 ping `ok:true`(응답 서명 확인)
- 개시 정산 run 1(정산일 2026-11-05, 8건 12만 원): 901 −6만(`OVERDRAWN`, 한도 안), 902 +12만, `999` −6만(`OVERDUE` — 시험용 은행, 한도 0이라 송금만 막힘. 필요하면 비활성화)
- AvengersBank: `feature/backend-api` push가 8d(`c7a9285`)를 먼저 배포했고(키 없이 타행 이체 꺼짐), 키를 넣은 `.env`로 `deploy-app avengers-bank c7a9285…` 재생성 → `keys ping` `ok: True`, `keys status` 지문 일치, 허브 쪽 901 ping `ok:true`(응답 서명 확인)
- 걸린 것: AvengersBank 배포 트리거가 `feature/backend-api` push라, 허브보다 먼저 배포되어 둘 다 준비될 때까지 타행 이체가 약 20분 꺼져 있었다. 다음에 서명 체계를 바꿀 일이 있으면 허브 쪽 키 준비 → 허브 배포 → AvengersBank push 순서로 한다

## 확인

```bash
curl -s https://clearing-hub.ai-agent-develop.cloud/
```
```bash
docker compose -f /srv/clearing-hub/docker-compose.yml ps
```

`{"service":"ClearingHub",...}`가 오고, 두 컨테이너가 `healthy`이며 이미지 태그가 방금 push한 커밋과 같아야 한다. 태그가 다르면 워크플로가 초록불이어도 배포는 실패한 것이다.

> compose 명령을 서버에서 직접 칠 때는 `ECR_REGISTRY`·`APP_NAME`·`IMAGE_TAG`가 비어 이미지 이름을 못 만든다. `ps`·`logs`는 괜찮지만, 컨테이너를 다시 만들 때(`up -d`)는 아래처럼 `deploy-app`을 쓴다.

## 가짜 은행 연결

가짜 은행은 자기 Ed25519 키(`SIM_SIGNING_KEY`, **필수**: 없으면 컨테이너가 시작하지 않는다)로 응답에 서명하고, 허브 공개키(`SIM_HUB_PUBLIC_KEY`)로 허브 서명을 검증한다. 키는 서버 안에서 만들고, 공개키를 902로 등록한다. Session Manager에서:

```bash
TOKEN=$(sudo grep '^ADMIN_TOKEN=' /srv/clearing-hub/.env | cut -d= -f2)
DER=$(openssl genpkey -algorithm ed25519 -outform DER | base64 -w0)
SIM_PUB=$(echo "$DER" | base64 -d | openssl pkey -inform DER -pubout -outform DER | tail -c 32 | base64)
HUB_PUB=$(sudo grep '^HUB_SIGNING_KEY=' /srv/clearing-hub/.env | cut -d= -f2- | base64 -d | \
  (printf '\x30\x2e\x02\x01\x00\x30\x05\x06\x03\x2b\x65\x70\x04\x22\x04\x20'; cat) | \
  openssl pkey -inform DER -pubout -outform DER | tail -c 32 | base64)
sudo sed -i '/^SIM_SIGNING_KEY=/d;/^SIM_HUB_PUBLIC_KEY=/d;/^SIM_HUB_SECRET=/d;/^SIM_SIGNATURE_MODE=/d' /srv/clearing-hub/sim.env
printf 'SIM_SIGNING_KEY=%s\nSIM_HUB_PUBLIC_KEY=%s\n' "$(echo "$DER" | base64 -d | tail -c 32 | base64)" "$HUB_PUB" \
  | sudo tee -a /srv/clearing-hub/sim.env >/dev/null
unset DER
echo "902 공개키: $SIM_PUB"
```

`HUB_PUB`은 `.env`의 허브 개인키에서 공개키를 꺼낸 것이다 (앞의 16바이트는 Ed25519 개인키 DER 머리). 허브가 이미 떠 있으면 `curl -s https://clearing-hub.ai-agent-develop.cloud/v1/hub-key`의 `public_key`와 같아야 한다.

902가 없으면 등록하고, 있으면 공개키만 바꾼다:

```bash
curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks \
  -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"bank_code\":\"902\",\"name\":\"SimBank\",\"base_url\":\"http://clearing-hub-sim:8100/interbank\",\"public_key\":\"$SIM_PUB\"}"
curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/902/public-key \
  -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d "{\"public_key\":\"$SIM_PUB\"}"
```

지금 태그 그대로 컨테이너를 다시 만든다 (`restart`로는 env가 바뀌지 않는다):

```bash
sudo /usr/local/bin/deploy-app clearing-hub "$(docker inspect -f '{{.Config.Image}}' clearing-hub | cut -d: -f2)"
```

연결 확인:

```bash
curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/902/ping -H "X-Admin-Token: $TOKEN"
```

`"ok":true`(`status` 200, `response_signature_valid` true)면 된다. `SIM_HUB_PUBLIC_KEY`가 비어 있으면 가짜 은행은 허브 서명 검증을 건너뛴다 (로컬 시험용. 운영에서는 채운다). `SIM_SIGNING_KEY`를 바꾸면 902 공개키도 같이 바꿔야 한다 (안 바꾸면 902로 가는 이체가 모두 `PENDING`).

## SimBank 화면 배포

SimBank(902) 시연 화면 `https://simbank.ai-agent-develop.cloud/` (잔액·거래내역·보내기, `docs/SIMBANK_PLAN.md`). 허브 코드와 DB는 바뀌지 않는다 (마이그레이션 없음).

1. 가비아 DNS A `simbank` → `13.209.128.48`, `nslookup simbank.ai-agent-develop.cloud 8.8.8.8`로 확인
2. `deploy/prepare-simbank.sh`를 서버 `/srv/clearing-hub/`에 두고 `sudo bash prepare-simbank.sh` (다시 실행해도 안전). 하는 일: `sim-data` 폴더, compose sim 볼륨 `./sim-data:/data`, `sim.env`에 `SIM_DB_PATH`·`SIM_HUB_URL`·`SIM_UI_PASSWORD`(생성, `/srv/clearing-hub/simbank-password.txt`에만)·`SIM_API_KEY`(`POST /admin/banks/902/keys` 재발급. API 키 해시만 바뀌고 902 공개키·셀프 시험은 그대로), Caddy 블록(`/interbank/*`·`/docs`·`/openapi.json`은 404) reload, 902 예치금 출력. 컨테이너는 다시 만들지 않는다
3. Caddy 적용 확인: `docker logs caddy 2>&1 | grep simbank`에 `certificate obtained successfully`
4. `main` push → Actions → `deploy-app`이 sim 컨테이너를 새 이미지·볼륨·sim.env로 다시 만든다
5. 확인: `https://simbank.ai-agent-develop.cloud/` 200(화면), `/health` 200 JSON, `/interbank/inquiry` 404. 화면에서 비밀번호 입력 → 고객 김철수 → AvengersBank 시연 계좌로 1만 원 보내기 `완료`, AvengersBank 모아로 "SimBank 110-123-0001로 1만 원" → SimBank 화면에 "방금 입금". `docker restart clearing-hub-sim` 뒤에도 잔액 유지
6. 시연 비밀번호 보기: `sudo cat /srv/clearing-hub/simbank-password.txt`. 바꾸기: `sim.env`의 `SIM_UI_PASSWORD`를 고치고 같은 태그로 `deploy-app` (위 "가짜 은행 연결"의 재생성 명령)

보내기는 902의 순채무한도를 쓴다. 한도가 모자라면 보내기와 셀프 시험이 함께 막히므로 `POST /admin/reserve/902/deposit`으로 예치한다.

## 참가 은행 등록

새 팀은 **초대 코드로 셀프 등록**한다. 관리자 일은 초대 코드와 허브 공개키 지문을 보내는 것뿐이다: [ONBOARDING.md](ONBOARDING.md).

| 은행 | `base_url` |
|---|---|
| 가짜 은행 (902) | `http://clearing-hub-sim:8100/interbank` (서버 내부) |
| AvengersBank (901) | `https://avengers-bank.ai-agent-develop.cloud/api/interbank`. 같은 서버이므로 `http://avengers-bank:8000/api/interbank`로 내부 호출을 해도 된다 |
| 다른 팀 | 각 팀이 셀프 등록 때 넣은 공개 HTTPS 주소. 바꿀 때는 은행이 `PATCH /v1/me`로 (새 주소로 ping해 정상일 때만 반영) |

관리자가 직접 고칠 때는 다시 등록하지 않고 고친다 (코드·키는 그대로). 넣은 필드만 바뀐다:

```bash
curl -s -X PATCH https://clearing-hub.ai-agent-develop.cloud/admin/banks/903 \
  -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"base_url":"https://new.example.com/interbank"}'
curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/903/ping -H "X-Admin-Token: $TOKEN"
```

## 정산 배포 순서 (예치금·순채무한도)

정산([SETTLEMENT_PLAN.md](SETTLEMENT_PLAN.md) 1~7단계)이 들어간 커밋을 처음 배포하면 **이미 등록된 은행(901·902)의 송금이 `LIMIT_EXCEEDED`로 막힌다.** 마이그레이션이 기존 은행의 예치금과 순채무한도를 0으로 두기 때문이다 (받기는 된다). 기본 한도 10억(`DEFAULT_NET_DEBIT_CAP`, 8c)은 **새로 등록하는 은행에만** 적용된다. 은행 쪽은 고칠 것이 없고, 허브 관리자가 아래 순서로 한다.

1. **마이그레이션** `alembic upgrade head`: 컨테이너가 시작할 때 자동으로 돈다 (`c7e1a9d4f2b6` → `d8f2b3a6e1c9` → `e5a1c7f3d9b2` → `f3b8d2a7c4e1`). 확인:
   ```bash
   docker exec clearing-hub alembic current
   ```
2. **서버 재시작**: `main` push → Actions → `deploy-app`이 새 이미지로 두 컨테이너를 다시 만든다. 정산 워커(`SETTLEMENT_WORKER_ENABLED`, 기본 켜짐)도 함께 뜬다
3. **관리자가 참가 은행마다 예치금 또는 한도를 넣는다.** 안 넣으면 그 은행의 송금이 전부 `LIMIT_EXCEEDED`("순채무한도를 넘었습니다")다. 배포 전 거래는 모두 미정산이라 순채무(`exposure`)에 이미 잡혀 있으므로, 먼저 `/admin/reserve`의 `exposure`를 보고 그보다 넉넉히 넣는다
   ```bash
   curl -s https://clearing-hub.ai-agent-develop.cloud/admin/reserve -H "X-Admin-Token: $TOKEN"
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/reserve/901/deposit      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"amount":1000000,"memo":"개시 예치"}'
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/reserve/902/deposit      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"amount":1000000,"memo":"개시 예치 (가짜 은행)"}'
   ```
   예치금 대신(또는 함께) 순채무한도를 줄 수도 있다: `PATCH /admin/banks/{code}` `{"net_debit_cap": 1000000000}` (새 은행의 기본값과 같게). 가짜 은행 `902`는 받기만 하지만, 시연에서 관리자가 902 키로 `902 → 901` 이체를 만들므로(DEMO.md 3) 예치금이 필요하다
4. **`/admin/reserve`로 확인**: 은행마다 `available`(= `balance + net_debit_cap − exposure`)이 0보다 커야 송금이 된다
5. **(권장) 개시 정산**: 배포 전 거래가 첫 run에 모두 들어간다. 시연 숫자를 깨끗하게 하려면 오늘 날짜로 run을 만들고 바로 확정해 둔다. 확정하면 그 차액만큼 예치금이 옮겨지므로 3을 먼저 한다
   ```bash
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/settlements/run      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d "{\"settle_date\":\"$(TZ=Asia/Seoul date +%F)\"}"
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/settlements/<run_id>/confirm      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"reason":"개시 정산"}'
   ```
   AvengersBank가 그 거래들의 내부 분개(`interbank_ledger`)를 갖고 있지 않으면 이 run은 AvengersBank 쪽에서 `MISMATCH`(`hub_only`)로 남는다. 그 run만 그렇고 이체와 다음 run의 대사는 계속된다

배포 뒤에는 워커가 매일 00:05(KST) 어제 정산일의 DRAFT를 만든다. 확정은 9단계부터 자동이다: 포지션이 있는 은행이 모두 ack하면 즉시, 아니면 09:00(KST, `SETTLEMENT_CONFIRM_DEADLINE`)에 dispute가 없으면 확정된다 (SPEC 10.7). **dispute가 있는 DRAFT는 관리자가 확정하거나 취소해야 하고, 그동안 다음 날 배치는 건너뛴다** (알림 `batch_skipped`. 거래는 미정산으로 남아 다음 run에 들어가고, 그동안 순채무에서도 빠지지 않는다).

### 9단계 배포 (자동 확정·셀프 시험·알림)

1. 마이그레이션 `a9d4e2c6b8f1`(`settlement_acks`)은 배포 때 자동으로 돈다 (기존 테이블은 그대로)
2. 서버 `.env`에 `ALERT_WEBHOOK_URL`을 넣는다 (관리자 채널의 incoming webhook). 나머지 새 설정은 기본값이면 넣지 않아도 된다
3. 재시작 뒤 확인: `curl -s $HUB/` → `"spec": "v1.1"`. 알림 시험은 아무 은행이나 `POST /v1/test-receive`로 5xx를 일으키기보다, 서버 안에서 `curl -s -X POST "$ALERT_WEBHOOK_URL" -H 'Content-Type: application/json' -d '{"text":"ClearingHub 알림 시험","content":"ClearingHub 알림 시험"}'`로 채널에 글이 오는지 본다
4. 셀프 시험은 902(가짜 은행)의 순채무한도 안에서 돈다. 902는 셀프 시험마다 돈을 내므로, 알림 `sim_bank`(한도 부족)가 오면 `POST /admin/reserve/902/deposit`으로 예치금을 넣는다

### 10단계 배포 (연동 진단, SPEC v1.2)

1. 마이그레이션 `b7e3f1a9c2d4`(`auth_failures`)은 배포 때 자동으로 돈다 (기존 테이블은 그대로). 새 설정은 없다
2. 재시작 뒤 확인: `curl -s $HUB/` → `"spec": "v1.2"`. `curl -s $HUB/admin/banks/902/auth-failures -H "X-Admin-Token: $TOKEN"`이 `{"bank_code":"902","findings":[…]}`로 답한다
3. API 키가 맞은 은행의 401 본문에 `hint`(규칙 진단 한 줄)가 붙는다. 은행은 `GET /v1/diagnose/failures`(서명 없이 API 키만)로 자세히 본다

### 관리자 정산 화면

브라우저로 `https://clearing-hub.ai-agent-develop.cloud/admin/ui/settlements`를 열고 `ADMIN_TOKEN`을 넣는다. SSM으로 DB를 보지 않아도 된다.

- **목록**: run마다 정산일·상태·건수·합계·만든 주체/시각·확정 주체/시각(`auto`·`auto-ack`·`auto-deadline`·`admin`). 시각은 모두 KST
- **상세**(행 클릭, 주소 `#run=5`로 바로 열기): 은행별 포지션(+ 받을 은행, − 낼 은행, 확정 뒤 반영 결과·잔액), 은행별 ack/dispute(시각, dispute 메시지·transfer_id, 아직 확인하지 않은 은행은 `미응답`), 은행 쌍별 표, 거래 목록
- **DRAFT 안내**: 다음 확정 마감 시각(`SETTLEMENT_CONFIRM_DEADLINE`)과 남은 시간, 그때 자동 확정되는지(dispute 없음) 아니면 관리자 판정이 필요한지(dispute 있음, 빨간 상자). 워커가 꺼져 있으면(`SETTLEMENT_WORKER_ENABLED=false`) 그렇게 알린다
- **관리 동작**: DRAFT의 확정(사유 필수)·취소, 수동 정산 run. 모두 확인 대화상자를 거친다. 기존 관리자 API(`/admin/settlements/...`)를 그대로 부른다
- 토큰은 그 탭의 `sessionStorage`에만 둔다(탭을 닫거나 `토큰 지우기`를 누르면 지워진다). 페이지는 토큰 없이 열리지만 데이터는 관리자 API가 401로 막는다. 새 마이그레이션·환경 변수는 없다
- 상세 응답(`GET /admin/settlements/{run_id}`)에 화면용 필드 `unanswered`(포지션이 있는데 ack도 dispute도 안 보낸 은행)와 `confirm_deadline_at`(DRAFT이고 워커가 켜져 있을 때 다음 마감 시각, 그 밖에는 null)이 더해졌다

로컬에서는 `uv run python -m clearing_hub.devserver`로 띄우고 `http://127.0.0.1:8000/admin/ui/settlements`에 시작 로그의 관리자 토큰을 넣는다.

### 관리자 실시간 장부 화면

`/admin/ui/ledger` (세 관리자 화면은 상단 링크로 서로 오간다. 토큰도 같은 탭이면 다시 넣지 않는다). 5초마다 다시 읽고, `일시정지`를 누르거나 탭이 안 보이면 멈춘다.

- **은행별 예치금 현황**: 잔액·순채무한도·미정산 순채무·가용액·상태. 가용액이 한도의 20% 미만이거나 OVERDRAWN이면 노란색, OVERDUE면 빨간색
- **지금 정산하면**: 본문 없는 정산 run(어제 정산일)을 지금 돌리면 모을 거래의 포지션·쌍별 상계, 마감 뒤 완료돼 다음 정산으로 넘어갈 거래, 진행 중 거래. DRAFT가 있으면 그 거래는 빠진다고 알린다 (`GET /admin/settlements/preview`, 아무것도 저장하지 않는다)
- **최근 거래 20건**, **최근 예치금 분개 20건** (`GET /admin/reserve/ledger`, 전체 은행)
- 새 마이그레이션·환경 변수 없음. 은행용 API(SPEC)는 바뀌지 않는다

## Ed25519 필수화 배포 순서 (8c, SPEC v1.0)

8c부터 허브는 **Ed25519 서명만** 받고 보낸다 (HMAC·서명 모드 없음). 허브는 `HUB_SIGNING_KEY`가 없으면, 가짜 은행은 `SIM_SIGNING_KEY`가 없으면 **시작하지 않는다.** 공개키가 없는 은행은 요청이 `401 SIGNATURE_INVALID`("등록된 공개키가 없습니다"), 그 은행으로 보내는 이체는 접수 전 거절 `PUBLIC_KEY_MISSING`이다 (돈은 안 움직인다). AvengersBank(901)도 같은 때 Ed25519로 바꿔야 한다 (AvengersBank 8d).

1. **901·902 공개키를 먼저 등록한다.** 운영 허브에 `POST /admin/banks/{code}/public-key`가 있으면(8a 이후 버전) **배포 전에** 등록한다 (공개키 칸만 채워지고 지금 동작은 바뀌지 않는다). 지금 운영(`main`, SPEC v0.5)에는 이 API가 없으므로 2를 배포 전에 해 두고, 3의 배포 직후 **가장 먼저** 등록한다. 그 사이(몇 분) 901·902의 요청·이체가 거절되므로 시연·실사용이 없는 시간에 한다.
   ```bash
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/901/public-key -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"public_key":"<AvengersBank가 준 공개키>"}'
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/902/public-key -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"public_key":"<2의 902 공개키>"}'
   ```
   901 공개키는 AvengersBank 팀이 만들어 지문과 함께 준다 (개인키는 AvengersBank 서버 `.env`에만). 응답의 `public_key_fingerprint`가 그 지문과 같아야 한다.
2. **서버 키 준비 (배포 전, 서버 안에서).** 개인키는 화면·채팅에 내지 않는다. 운영 서버의 옛 이미지에는 Ed25519 도구가 없으므로 `openssl`로 만든다 (raw 32바이트 = PKCS#8 DER의 끝 32바이트).
   - 허브 키 (`.env`에 `HUB_SIGNING_KEY`가 이미 있으면 그대로 둔다. 바꾸면 모든 은행이 허브 공개키를 새로 받아야 한다):
     ```bash
     sudo grep -q '^HUB_SIGNING_KEY=.\+' /srv/clearing-hub/.env || \
       echo "HUB_SIGNING_KEY=$(openssl genpkey -algorithm ed25519 -outform DER | tail -c 32 | base64)" | sudo tee -a /srv/clearing-hub/.env >/dev/null
     ```
   - 초대 문구에 넣을 허브 주소: `.env`에 `HUB_PUBLIC_URL=https://clearing-hub.ai-agent-develop.cloud` (없으면 넣는다)
   - 가짜 은행 키·허브 공개키 → `sim.env`, 902 공개키 출력: 위 "가짜 은행 연결"의 첫 명령 (`SIM_HUB_SECRET`·`SIM_SIGNATURE_MODE` 줄도 지운다)
   - 허브 공개키 지문을 AvengersBank 팀에 개별로 전달한다 (그 팀이 `HUB_PUBLIC_KEY`로 넣는다). 지문 = 허브 공개키 raw 32바이트의 sha256 앞 16자: `echo "$HUB_PUB" | base64 -d | sha256sum | cut -c1-16`. 배포 뒤 `GET /v1/hub-key`의 `fingerprint`와 같아야 한다
3. **배포**: `main` push → Actions → `deploy-app`. 마이그레이션 `f3b8d2a7c4e1`이 자동으로 돈다 (`hmac_secret`·`signature_mode` 삭제, `invites` 추가). 1을 못 했으면 지금 한다. AvengersBank 8d 배포도 이때 한다
4. **확인**:
   ```bash
   curl -s https://clearing-hub.ai-agent-develop.cloud/v1/hub-key
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/902/ping -H "X-Admin-Token: $TOKEN"
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/901/ping -H "X-Admin-Token: $TOKEN"
   ```
   ping은 둘 다 `"ok":true`여야 한다. 901이 `response_signature_valid:false`면 901로 가는 이체가 모두 `PENDING`이 되므로 AvengersBank에 바로 알린다. 그다음 모아 화면에서 902로 소액 이체 → `COMPLETED`
5. **새 팀**: [ONBOARDING.md](ONBOARDING.md) (초대 코드)

**분쟁 조회**: 거래 하나의 접수 요청·`/receive` 요청·응답 원문과 서명 검증 결과

```bash
curl -s https://clearing-hub.ai-agent-develop.cloud/admin/transfers/<transfer_id>/evidence -H "X-Admin-Token: $TOKEN"
```

`body`는 서명한 바이트 그대로(UTF-8 문자열, 아니면 `body_encoding: base64`)라 그 자리에서 다시 검증할 수 있다. 이 표는 고치거나 지우지 않는다. 8c 전 거래의 증거에는 `signature_alg: hmac`이 남아 있을 수 있다.

키 문제(은행 개인키 유출·분실, API 키 재발급, 허브 키 유출)는 [ONBOARDING.md](ONBOARDING.md) 6.

## UNKNOWN 거래 정리

재시도를 다 해도 결과를 모르는 거래는 `UNKNOWN`으로 남고, 허브는 더 이상 자동으로 확인하지 않는다. 찾기:

```bash
curl -s "https://clearing-hub.ai-agent-develop.cloud/admin/transfers?status=UNKNOWN" -H "X-Admin-Token: $TOKEN"
curl -s https://clearing-hub.ai-agent-develop.cloud/admin/transfers/<transfer_id>/events -H "X-Admin-Token: $TOKEN"
```

1. **받는 은행 장애가 풀렸으면 재확인**: 재시도 횟수를 0으로 하고 지금 한 번 확인한다. 이번에도 모르면 `PENDING`으로 남아 재시도 태스크가 이어서 확인한다
   ```bash
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/transfers/<transfer_id>/recheck -H "X-Admin-Token: $TOKEN"
   ```
2. **받는 은행이 자동 확인(SPEC 4.3)에 답하지 못하면 직접 확정**: 받는 은행 팀에 그 `transfer_id`로 입금됐는지 물어 본 결과로 정한다. `reason`은 이력에만 남고 보내는 은행에는 보이지 않는다
   ```bash
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/transfers/<transfer_id>/resolve \
     -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
     -d '{"status":"COMPLETED","reason":"SimBank 원장 확인 (담당 OOO)","holder_name":"김철수"}'
   ```

> **`FAILED`는 받는 은행에 입금이 없다는 것을 확인한 뒤에만 쓴다.** 보내는 은행이 출금을 되돌리므로, 입금이 됐는데 `FAILED`로 정하면 이중 지급이 된다. 모르겠으면 정하지 말고 `UNKNOWN`으로 둔다. `UNKNOWN`이 아닌 거래(`PENDING`·확정된 거래)는 둘 다 `409 TRANSFER_NOT_UNKNOWN`으로 거절된다.

## 서버 `.env`

| 파일 | 키 | 값 |
|---|---|---|
| `.env` | `DATABASE_URL` | `postgresql+psycopg://clearing_hub:<생성>@postgres:5432/clearing_hub` |
| | `ADMIN_TOKEN` | 생성 (hex 64자). 관리 API(`/admin/*`) 헤더 `X-Admin-Token` |
| | `RECEIVER_TIMEOUT_SECONDS`, `RETRY_INTERVAL_SECONDS`, `RETRY_MAX_COUNT`, `MAX_TRANSFER_AMOUNT` | `.env.example`과 같은 기본값 |
| | `CI_KEY` | 가상 CI 키 (SPEC 9.1, hex 64자). **허브 앱은 쓰지 않는다**. 원본 보관용이며 참가 은행들에 나눠 주는 값 |
| | `SETTLEMENT_WORKER_ENABLED` | `true`(기본): 일일 정산 배치 태스크를 띄운다. `false`면 관리자가 `POST /admin/settlements/run`으로만 |
| | `SETTLEMENT_CUTOFF_TIME` | `00:05`(기본, `HH:MM`). 매일 이 시각에 어제 정산일의 DRAFT를 만든다 |
| | `SETTLEMENT_TIMEZONE` | `Asia/Seoul`(기본). 정산일·마감 시각의 기준 |
| | `SETTLEMENT_AUTO_CONFIRM` | `false`(기본): 은행 확인(ack)·마감 시각으로 확정. `true`면 배치가 DRAFT를 대사 없이 바로 확정한다 (시연용) |
| | `SETTLEMENT_CONFIRM_DEADLINE` | `09:00`(기본, `HH:MM`). 확정 마감: 포지션이 있는 은행이 모두 ack하지 않았어도 이 시각에 dispute가 없는 DRAFT를 확정한다 (SPEC 10.7). dispute가 있으면 확정하지 않고 알림 |
| | `SIM_BANK_CODE` | `902`(기본). 셀프 시험(SPEC 3.7)에서 허브가 이 은행 명의로 시험 이체를 만든다. 이 은행이 없거나 비활성이면 셀프 시험은 503 `SIM_BANK_UNAVAILABLE` |
| | `SELF_TEST_DAILY_LIMIT` | `20`(기본). 은행별 하루(KST) 셀프 시험 횟수 (넘으면 429 `TOO_MANY_TESTS`) |
| | `ALERT_WEBHOOK_URL` | 관리자 알림 웹훅 (Slack·Discord incoming webhook. 본문 `{"text","content"}`). 비우면 보내지 않고 로그만 남긴다. dispute·자동 확정·배치 건너뜀·`OVERDUE`·`UNKNOWN`·셀프 등록·ping 실패·셀프 시험 5xx·가짜 은행 한도 부족을 보낸다. **비밀값이다** (주소만 알면 누구나 그 채널에 글을 쓸 수 있다) |
| | `HUB_SIGNING_KEY` | **필수.** 허브 Ed25519 개인키 (raw 32바이트 base64, 44자). 서버 안에서 생성 ("Ed25519 필수화 배포 순서" 2). 없거나 형식이 틀리면 허브가 시작하지 않는다 (마이그레이션도 같은 설정을 읽는다) |
| | `PREVIOUS_KEY_TTL_SECONDS` | `86400`(기본). 은행 공개키 회전 뒤 옛 키를 받아 주는 시간 |
| | `INVITE_TTL_DAYS` | `7`(기본). 초대 코드 유효 기간 |
| | `BANK_CODE_START` | `903`(기본). 셀프 등록 때 이 코드부터 비어 있는 가장 작은 코드를 준다 |
| | `DEFAULT_NET_DEBIT_CAP` | `1000000000`(기본, 10억). 새로 등록하는 은행의 순채무한도 |
| | `HUB_PUBLIC_URL` | `https://clearing-hub.ai-agent-develop.cloud`. 초대 문구(`POST /admin/invites`의 `message`)에 넣을 허브 주소. 비어 있으면 그 관리자 요청의 주소가 들어간다 (서버 안에서 내부 주소로 부르면 내부 주소) |
| `sim.env` | `SIM_SIGNING_KEY` | **필수.** 가짜 은행 Ed25519 개인키. `/interbank/*` 2xx·4xx 응답에 응답 서명. 공개키는 902에 등록한다. 없거나 틀리면 컨테이너가 시작하지 않는다 |
| | `SIM_HUB_PUBLIC_KEY` | 허브 공개키 (`GET /v1/hub-key`). 허브 요청 검증. 비어 있으면 검증을 건너뛴다 (로컬 시험용) |
| | `SIM_DELAY_SECONDS` | `8` (허브 타임아웃 5초보다 길어야 지연 시나리오가 재현된다) |
| | `SIM_CI_KEY` | `.env`의 `CI_KEY`와 같은 값. 비어 있으면 예시 키 `example-ci-key-change-me`를 써서, 실제 키로 만든 `ci`로는 가짜 은행 계좌가 나오지 않는다 |

가짜 은행에는 허브의 DB 주소와 관리 토큰을 주지 않으려고 env 파일을 나눴다.

## CI_KEY (가상 CI 키)

2026-11-05 서버 안에서 `openssl rand -hex 32`로 만들었다. 값은 서버 밖(채팅·문서·Git)에 적지 않았다. 넣기 전 파일은 `.env.bak-20261105053610`, `sim.env.bak-20261105053610`으로 남겼다.

- 확인: 실제 키로 만든 김철수 `ci` → 902 계좌 2개, 예시 키로 만든 `ci` → 0개
- 참가 은행에 전달할 때 (Session Manager): `sudo grep '^CI_KEY=' /srv/clearing-hub/.env | cut -d= -f2`. 초대 코드와 함께 그 은행 팀에만 개별로 준다 (ONBOARDING.md 1). 은행은 자기 서버 `.env`에 넣고, 고객 `ci`를 이 키로 계산해 저장한다
- **바꾸지 않는다**: 바꾸면 모든 은행이 저장해 둔 고객 `ci`를 다시 계산해야 한다. 유출됐을 때만 새로 만들고, `.env`·`sim.env`를 함께 고친 뒤 아래 명령으로 컨테이너를 다시 만들고 전 은행에 알린다
  ```bash
  sudo /usr/local/bin/deploy-app clearing-hub "$(docker inspect -f '{{.Config.Image}}' clearing-hub | cut -d: -f2)"
  ```

## 알아 둘 것

- `/admin/*`이 인터넷에 열려 있고 `ADMIN_TOKEN` 하나로만 막는다. 관리자 JWT 로그인은 다음 단계이므로 토큰을 채팅·문서에 붙이지 않는다
- 허브 DB에는 은행의 비밀이 없다: 은행 공개키(비밀 아님)와 API 키의 해시뿐이다 (8c에서 `hmac_secret`을 지웠다). DB 백업이 새도 은행 명의 요청은 만들 수 없다. 허브 개인키(`HUB_SIGNING_KEY`)는 `.env`에만 있다
- `invites`는 쓴 뒤에도 남긴다 (누가 어느 코드로 등록했는지). 목록에는 코드 앞 8자만 나온다
- `transfer_evidence`는 이체마다 3~5행이 쌓이고 지우지 않는다 (보관 기간은 아직 정하지 않았다)
- 마이그레이션은 컨테이너가 시작될 때마다 `alembic upgrade head`로 적용된다. 모델을 바꾸면 리비전을 만들어 함께 커밋한다

## 로컬 검증 (2026-11-05)

서버와 같은 compose(`server-setup.sh`에서 꺼낸 것)에 `web` 네트워크, `postgres:16-alpine`을 붙여 돌렸다.

- 마이그레이션 `ef6bf3b35409` 적용, 두 컨테이너 `healthy`
- 902·901 등록 → `sim.env`에 secret → 다시 만든 뒤 `ping` 200, 서명 없는 호출은 가짜 은행이 401로 거절
- 이체: `0001` COMPLETED · `0002` FAILED(404) · `0004` PENDING → 재시도 1회 뒤 COMPLETED · `0005` PENDING → 재시도로 COMPLETED
