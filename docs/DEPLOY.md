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
| `alembic/versions/e5a1c7f3d9b2_nonrepudiation.py` | 부인방지: `banks.signature_mode`(기본 `hmac`)·공개키 칸, `transfer_evidence` |

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

가짜 은행은 허브가 발급한 secret으로 허브 서명을 검증한다. secret은 등록할 때 한 번만 나오므로 등록 → `sim.env`에 넣기 → 컨테이너 다시 만들기 순서로 한다. Session Manager에서:

```bash
TOKEN=$(sudo grep '^ADMIN_TOKEN=' /srv/clearing-hub/.env | cut -d= -f2)
curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks \
  -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"bank_code":"902","name":"SimBank","base_url":"http://clearing-hub-sim:8100/interbank"}'
```

응답의 `secret`을 `/srv/clearing-hub/sim.env`의 `SIM_HUB_SECRET=` 뒤에 넣고, 지금 태그 그대로 컨테이너를 다시 만든다 (`restart`로는 env가 바뀌지 않는다):

```bash
sudo /usr/local/bin/deploy-app clearing-hub "$(docker inspect -f '{{.Config.Image}}' clearing-hub | cut -d: -f2)"
```

연결 확인:

```bash
curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/902/ping -H "X-Admin-Token: $TOKEN"
```

`"reachable":true, "status":200`이면 된다. `SIM_HUB_SECRET`이 비어 있으면 가짜 은행은 서명 검증을 건너뛴다. 외부에 공개하지 않아 위험은 작지만, 서명까지 시연하려면 채운다.

## 참가 은행 등록

| 은행 | `base_url` |
|---|---|
| 가짜 은행 (902) | `http://clearing-hub-sim:8100/interbank` (서버 내부) |
| AvengersBank (901) | `https://avengers-bank.ai-agent-develop.cloud/api/interbank`. AvengersBank가 `/interbank/*`를 `/api` 아래에 만든 뒤 등록한다. 같은 서버이므로 `http://avengers-bank:8000/api/interbank`로 내부 호출을 해도 된다 |
| 다른 팀 | 각 팀이 알려 준 공개 HTTPS 주소 |

등록 응답의 `api_key`·`secret`은 그 은행 팀에게만 전달한다. AvengersBank는 서버 `.env`에 허브 주소와 함께 넣는다.

등록한 뒤 주소나 이름이 바뀌면 다시 등록하지 않고 고친다 (코드·키는 그대로). 넣은 필드만 바뀐다:

```bash
curl -s -X PATCH https://clearing-hub.ai-agent-develop.cloud/admin/banks/903 \
  -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" \
  -d '{"base_url":"https://new.example.com/interbank"}'
curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/903/ping -H "X-Admin-Token: $TOKEN"
```

## 정산 배포 순서 (예치금·순채무한도)

정산([SETTLEMENT_PLAN.md](SETTLEMENT_PLAN.md) 1~7단계)이 들어간 커밋을 처음 배포하면 **모든 참가 은행의 송금이 `LIMIT_EXCEEDED`로 막힌다.** 예치금과 순채무한도가 둘 다 0에서 시작하기 때문이다 (받기는 된다, SPEC 10.2). 은행 쪽은 고칠 것이 없고, 허브 관리자가 아래 순서로 한다.

1. **마이그레이션** `alembic upgrade head`: 컨테이너가 시작할 때 자동으로 돈다 (`c7e1a9d4f2b6` → `d8f2b3a6e1c9`). 확인:
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
   예치금 대신(또는 함께) 순채무한도를 줄 수도 있다: `PATCH /admin/banks/{code}` `{"net_debit_cap": 500000}`. 가짜 은행 `902`는 받기만 하지만, 시연에서 관리자가 902 키로 `902 → 901` 이체를 만들므로(DEMO.md 3) 예치금이 필요하다
4. **`/admin/reserve`로 확인**: 은행마다 `available`(= `balance + net_debit_cap − exposure`)이 0보다 커야 송금이 된다
5. **(권장) 개시 정산**: 배포 전 거래가 첫 run에 모두 들어간다. 시연 숫자를 깨끗하게 하려면 오늘 날짜로 run을 만들고 바로 확정해 둔다. 확정하면 그 차액만큼 예치금이 옮겨지므로 3을 먼저 한다
   ```bash
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/settlements/run      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d "{\"settle_date\":\"$(TZ=Asia/Seoul date +%F)\"}"
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/settlements/<run_id>/confirm      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"reason":"개시 정산"}'
   ```
   AvengersBank가 그 거래들의 내부 분개(`interbank_ledger`)를 갖고 있지 않으면 이 run은 AvengersBank 쪽에서 `MISMATCH`(`hub_only`)로 남는다. 그 run만 그렇고 이체와 다음 run의 대사는 계속된다

배포 뒤에는 워커가 매일 00:05(KST) 어제 정산일의 DRAFT를 만든다. 자동 확정(`SETTLEMENT_AUTO_CONFIRM`)이 꺼져 있으면 관리자가 확정한다(`POST /admin/settlements/{run_id}/confirm`). **DRAFT를 확정하지 않고 두면 다음 날 배치는 경고만 남기고 건너뛴다** (거래는 미정산으로 남아 다음 run에 들어가고, 그동안 순채무에서도 빠지지 않는다).

## 부인방지 배포·전환 순서 (8단계, SPEC 2.6)

이 커밋을 배포해도 **은행 쪽 동작은 바뀌지 않는다.** 모든 은행이 `hmac` 모드로 시작하고(마이그레이션 `e5a1c7f3d9b2`의 기본값), 허브 → 은행 요청에 `X-Signature-Alg: hmac` 헤더가 하나 더 붙을 뿐이다 (모르는 헤더는 무시, SPEC 1절).

1. **마이그레이션**: 컨테이너가 시작할 때 자동 (`d8f2b3a6e1c9` → `e5a1c7f3d9b2`)
2. **허브 키 만들기** (Session Manager, 서버 안에서. 개인키를 화면·채팅에 내지 않는다):
   ```bash
   KEYS=$(docker exec clearing-hub python -c "from clearing_hub.core.signing import generate_keypair; print(*generate_keypair())")
   echo "HUB_SIGNING_KEY=${KEYS%% *}" | sudo tee -a /srv/clearing-hub/.env >/dev/null
   unset KEYS
   sudo /usr/local/bin/deploy-app clearing-hub "$(docker inspect -f '{{.Config.Image}}' clearing-hub | cut -d: -f2)"
   ```
   로컬에서는 `uv run python -c "from clearing_hub.core.signing import generate_keypair; print(generate_keypair())"`로 만들어 `.env`에 넣는다 (앞이 개인키, 뒤가 공개키).
   키가 없으면 허브는 Ed25519로 **보내지도**(`ed25519` 전환이 400 `HUB_KEY_MISSING`) 응답에 서명하지도 못한다. 받는 것(`both`)은 된다. 형식이 틀린 키는 서버가 시작하지 않는다
3. **확인·지문 전달**:
   ```bash
   curl -s https://clearing-hub.ai-agent-develop.cloud/v1/hub-key
   ```
   응답의 `fingerprint`를 secret처럼 **은행 팀마다 개별 경로로** 알려 준다. 은행은 이 API로 받은 공개키를 그 지문과 맞춰 본다 (응답만 믿으면 바꿔치기한 키를 받을 수 있다). 허브 키를 바꾸면 모든 `ed25519` 은행이 새 공개키를 받아야 하므로, 유출이 아니면 바꾸지 않는다
4. **은행별 전환** (은행이 요청할 때마다): 은행이 받는 쪽(허브 공개키 검증, 응답 서명)을 준비하고 `PUT /v1/keys`로 공개키를 등록한 뒤(또는 공개키를 받아 관리자가 등록) 요청하면 올린다
   ```bash
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/903/public-key      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"public_key":"<은행이 준 공개키>"}'
   curl -s -X PATCH https://clearing-hub.ai-agent-develop.cloud/admin/banks/903/signature-mode      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"signature_mode":"both"}'
   # 은행이 보내는 쪽까지 Ed25519로 바꿨다고 하면
   curl -s -X PATCH https://clearing-hub.ai-agent-develop.cloud/admin/banks/903/signature-mode      -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"signature_mode":"ed25519"}'
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/903/ping -H "X-Admin-Token: $TOKEN"
   ```
   ping에서 `status`가 200·404이고 `response_signature_valid`가 `true`여야 한다. `false`면 그 은행으로 가는 이체가 모두 `PENDING`이 되므로 바로 `both`로 내리고 은행에 알린다 (내리는 것은 언제든 된다). 관리자 은행 목록(`GET /admin/banks`)에 `signature_mode`·`public_key_fingerprint`가 나온다
5. **가짜 은행 `902`를 Ed25519로** (시연용): 가짜 은행 키 쌍을 서버 안에서 만들어 개인키는 `sim.env`의 `SIM_SIGNING_KEY`, 공개키는 902에 등록하고, `SIM_HUB_PUBLIC_KEY`에 3의 `public_key`를 넣는다. 순서는 위와 같다: `SIM_SIGNATURE_MODE=both`로 다시 만든다 → 허브 `ed25519` 전환 → `SIM_SIGNATURE_MODE=ed25519`로 다시 만든다 → ping
   ```bash
   KEYS=$(docker exec clearing-hub python -c "from clearing_hub.core.signing import generate_keypair; print(*generate_keypair())")
   printf 'SIM_SIGNING_KEY=%s\nSIM_HUB_PUBLIC_KEY=%s\nSIM_SIGNATURE_MODE=both\n' "${KEYS%% *}" \
     "$(curl -s https://clearing-hub.ai-agent-develop.cloud/v1/hub-key | python3 -c 'import json,sys; print(json.load(sys.stdin)["public_key"])')" \
     | sudo tee -a /srv/clearing-hub/sim.env >/dev/null
   curl -s -X POST https://clearing-hub.ai-agent-develop.cloud/admin/banks/902/public-key \
     -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d "{\"public_key\":\"${KEYS##* }\"}"
   unset KEYS
   ```
   그다음 컨테이너를 다시 만들고(`deploy-app`), `signature-mode` `ed25519` → `sim.env`의 `SIM_SIGNATURE_MODE=ed25519`로 고쳐 다시 만든 뒤 ping
6. **분쟁 조회**: 거래 하나의 접수 요청·`/receive` 요청·응답 원문과 서명 검증 결과
   ```bash
   curl -s https://clearing-hub.ai-agent-develop.cloud/admin/transfers/<transfer_id>/evidence -H "X-Admin-Token: $TOKEN"
   ```
   `body`는 서명한 바이트 그대로(UTF-8 문자열, 아니면 `body_encoding: base64`)라 그 자리에서 다시 검증할 수 있다. 이 표는 고치거나 지우지 않는다

HMAC secret 재발급(`POST /admin/banks/{code}/keys`)은 공개키와 모드를 바꾸지 않는다. 은행의 개인키가 유출되면 은행이 `PUT /v1/keys`로 회전하고(옛 키는 24시간 받는다), 급하면 관리자가 먼저 `hmac`으로 내린다.

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
| | `SETTLEMENT_AUTO_CONFIRM` | `false`(기본): 관리자가 대사 뒤 확정. `true`면 배치가 DRAFT를 바로 확정한다 (시연용) |
| | `HUB_SIGNING_KEY` | 허브 Ed25519 개인키 (raw 32바이트 base64, 44자). 서버 안에서 생성 ("부인방지 배포·전환 순서" 2). 비어 있으면 Ed25519로 보내지 못하고 응답 서명도 없다 |
| | `PREVIOUS_KEY_TTL_SECONDS` | `86400`(기본). 은행 공개키 회전 뒤 옛 키를 받아 주는 시간 |
| `sim.env` | `SIM_HUB_SECRET` | 902 등록 때 받은 secret (HMAC 검증) |
| | `SIM_SIGNATURE_MODE` | `hmac`(기본) / `both` / `ed25519`. 허브 요청을 어떤 서명으로 받을지. 허브의 902 `signature_mode`와 맞춘다 (`ed25519`면 HMAC 요청을 401로 거절) |
| | `SIM_HUB_PUBLIC_KEY` | 허브 공개키 (`GET /v1/hub-key`). `X-Signature-Alg: ed25519` 요청 검증. 비어 있으면 Ed25519 검증을 건너뛴다 (`SIM_HUB_SECRET`과 같은 규칙) |
| | `SIM_SIGNING_KEY` | 가짜 은행 Ed25519 개인키. 있으면 `/interbank/*` 2xx·4xx 응답에 응답 서명. 공개키는 902에 등록한다 |
| | `SIM_DELAY_SECONDS` | `8` (허브 타임아웃 5초보다 길어야 지연 시나리오가 재현된다) |
| | `SIM_CI_KEY` | `.env`의 `CI_KEY`와 같은 값. 비어 있으면 예시 키 `example-ci-key-change-me`를 써서, 실제 키로 만든 `ci`로는 가짜 은행 계좌가 나오지 않는다 |

가짜 은행에는 허브의 DB 주소와 관리 토큰을 주지 않으려고 env 파일을 나눴다.

## CI_KEY (가상 CI 키)

2026-11-05 서버 안에서 `openssl rand -hex 32`로 만들었다. 값은 서버 밖(채팅·문서·Git)에 적지 않았다. 넣기 전 파일은 `.env.bak-20261105053610`, `sim.env.bak-20261105053610`으로 남겼다.

- 확인: 실제 키로 만든 김철수 `ci` → 902 계좌 2개, 예시 키로 만든 `ci` → 0개
- 참가 은행에 전달할 때 (Session Manager): `sudo grep '^CI_KEY=' /srv/clearing-hub/.env | cut -d= -f2`. secret처럼 그 은행 팀에만 개별로 준다. 은행은 자기 서버 `.env`에 넣고, 고객 `ci`를 이 키로 계산해 저장한다
- **바꾸지 않는다**: 바꾸면 모든 은행이 저장해 둔 고객 `ci`를 다시 계산해야 한다. 유출됐을 때만 새로 만들고, `.env`·`sim.env`를 함께 고친 뒤 아래 명령으로 컨테이너를 다시 만들고 전 은행에 알린다
  ```bash
  sudo /usr/local/bin/deploy-app clearing-hub "$(docker inspect -f '{{.Config.Image}}' clearing-hub | cut -d: -f2)"
  ```

## 알아 둘 것

- `/admin/*`이 인터넷에 열려 있고 `ADMIN_TOKEN` 하나로만 막는다. 관리자 JWT 로그인은 다음 단계이므로 토큰을 채팅·문서에 붙이지 않는다
- `hmac_secret`은 아직 평문으로 저장된다 (다음 단계: Fernet 암호화). DB 백업에도 그대로 들어간다. 은행 Ed25519 공개키는 비밀이 아니다. 허브 개인키(`HUB_SIGNING_KEY`)는 `.env`에만 있다
- `transfer_evidence`는 이체마다 3~5행이 쌓이고 지우지 않는다 (보관 기간은 아직 정하지 않았다)
- 마이그레이션은 컨테이너가 시작될 때마다 `alembic upgrade head`로 적용된다. 모델을 바꾸면 리비전을 만들어 함께 커밋한다

## 로컬 검증 (2026-11-05)

서버와 같은 compose(`server-setup.sh`에서 꺼낸 것)에 `web` 네트워크, `postgres:16-alpine`을 붙여 돌렸다.

- 마이그레이션 `ef6bf3b35409` 적용, 두 컨테이너 `healthy`
- 902·901 등록 → `sim.env`에 secret → 다시 만든 뒤 `ping` 200, 서명 없는 호출은 가짜 은행이 401로 거절
- 이체: `0001` COMPLETED · `0002` FAILED(404) · `0004` PENDING → 재시도 1회 뒤 COMPLETED · `0005` PENDING → 재시도로 COMPLETED
