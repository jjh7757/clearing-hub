# 정산 시연 대본

허브가 은행 사이의 돈을 어떻게 증명하는지 보여 주는 순서: **예치 → 한도 → 하루치 거래 → 마감 상계 → 대사 → 확정 → 은행 장부 정리 → 속이는 은행**. 설계는 [SETTLEMENT_PLAN.md](SETTLEMENT_PLAN.md) 9절, 은행 쪽 규격은 [SPEC.md](SPEC.md) 10절.

아래의 기대 숫자는 [`tests/test_demo_scenario.py`](../tests/test_demo_scenario.py)가 같은 순서로 돌려 고정한 값이다 (`uv run pytest tests/test_demo_scenario.py`). 그 테스트는 모든 은행이 예치금 0에서 시작한다. 운영에서 이미 예치금이 있으면 **증감**을 보면 된다.

| 은행 | 코드 | 역할 |
|---|---|---|
| AvengersBank | `901` | 보내는 은행. 모아 화면에서 이체하고, 대사·장부 정리를 한다 |
| SimBank (가짜 은행) | `902` | 받는 은행. 장부가 없고 메모리에만 입금 기록(`received`)을 둔다. 계좌 끝 `0006`은 입금 없이 `200 COMPLETED` |
| DemoBank (시연용) | `$DEMO` | 셀프 등록·한도 검사용. 코드는 등록할 때 허브가 배정한다 (테스트에서는 `903`. 운영에서는 다른 팀이 먼저 등록했으면 그다음 번호) |

## 준비

1. 정산 기능이 운영에 배포돼 있어야 한다 (`feature/settlement` → `main`). 처음 배포했다면 [DEPLOY.md](DEPLOY.md) "정산 배포 순서"를 먼저 한다
2. 터미널에 관리자 토큰과 허브 주소 (토큰은 화면에 띄우지 않는다. Session Manager에서 `sudo grep '^ADMIN_TOKEN=' /srv/clearing-hub/.env`)
   ```bash
   export HUB=https://clearing-hub.ai-agent-develop.cloud TOKEN=<ADMIN_TOKEN>
   ```
3. **남은 미정산 거래 정리**: run은 "아직 정산되지 않은 `COMPLETED` 거래 전부"를 모은다. 시연 전 시험 거래가 섞이지 않게 오늘 run을 한 번 만들어 확정해 둔다 (4·6과 같은 명령, `reason` "시연 전 정리"). DRAFT가 남아 있으면 4에서 `409 SETTLEMENT_DRAFT_EXISTS`가 난다
4. **은행 서명 호출 도구**: 902가 보내는 이체(3)와 902의 대사 파일(8)은 902의 **개인키**로 서명해야 한다 (SPEC 2절, Ed25519). 902(가짜 은행)의 개인키는 서버의 `sim.env` `SIM_SIGNING_KEY`에 있다. 허브 저장소에서 아래 파일을 만든다. 허브 응답의 서명(SPEC 2.5)도 허브 공개키로 검증해 보여 준다. 902의 `api_key`가 없으면 `POST /admin/banks/902/keys`로 다시 발급한다 (API 키만 바뀌고 서명 키는 그대로)
   ```bash
   cat > hub_call.py <<'EOF'
   """은행 개인키로 서명해 허브를 부른다. 예: uv run python hub_call.py GET /v1/reserve"""
   import json, os, sys

   import httpx

   from clearing_hub.core.signing import sign_ed25519, verify_response

   method, path = sys.argv[1], sys.argv[2]
   body = sys.argv[3].encode() if len(sys.argv) > 3 else b""
   signed = sign_ed25519(os.environ["BANK_PRIVATE_KEY"], method, path, body)
   headers = {"X-Bank-Code": os.environ["BANK_CODE"], "X-Api-Key": os.environ["BANK_KEY"],
              "Content-Type": "application/json", **signed}
   res = httpx.request(method, os.environ["HUB"] + path, content=body, headers=headers, timeout=10)
   hub_key = httpx.get(os.environ["HUB"] + "/v1/hub-key", timeout=10).json()["public_key"]
   check = verify_response(hub_key, res.headers.get("x-response-timestamp"), res.status_code, signed["X-Signature"],
                           res.content, res.headers.get("x-response-signature"))
   print(res.status_code, "허브 응답 서명:", "맞음" if check is None else check)
   print(json.dumps(res.json(), ensure_ascii=False, indent=1))
   EOF
   ```
   902의 개인키는 서버 밖으로 꺼내지 않는다. 서버(Session Manager)에서 `export BANK_PRIVATE_KEY=$(sudo grep '^SIM_SIGNING_KEY=' /srv/clearing-hub/sim.env | cut -d= -f2-)`로 넣고 그 자리에서 부른다
5. AvengersBank: 서버 `.env`에 `INTERBANK_SETTLEMENT_SYNC_INTERVAL_SECONDS=30`(시연 동안만, 기본 1시간)을 넣고 재시작한다. 허브 DB를 초기화했다면 AvengersBank의 `settlement_reconciliations`와 `interbank_ledger.hub_run_id`도 비운다 (run 번호가 1부터 다시 붙어 이미 끝난 run으로 보고 건너뛴다)

## 1. 예치 — "은행은 허브에 돈을 맡겨 두고 거래한다"

```bash
curl -s -X POST $HUB/admin/reserve/901/deposit -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"amount":1000000,"memo":"시연 예치"}'
```
```bash
curl -s -X POST $HUB/admin/reserve/902/deposit -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"amount":1000000,"memo":"시연 예치"}'
```
```bash
curl -s $HUB/admin/reserve -H "X-Admin-Token: $TOKEN"
```

| 은행 | `balance` | `net_debit_cap` | `exposure` | `available` |
|---|---|---|---|---|
| 901 | 1,000,000 | 0 | 0 | 1,000,000 |
| 902 | 1,000,000 | 0 | 0 | 1,000,000 |

입금 응답의 `entry.kind`는 `DEPOSIT`. 모든 변동이 분개로 남는다 (`GET /admin/reserve/901/ledger`). **모의 예치금**이다: 실제 돈과 무관하고 관리자만 넣고 뺀다.

## 2. 셀프 등록과 한도 검사 — "관리자는 초대 코드만 보낸다", "갚을 수 없는 만큼은 보내지 못한다"

관리자가 초대 코드를 만든다 (실제로는 이 코드와 허브 공개키 지문을 팀에 개별로 보낸다, [ONBOARDING.md](ONBOARDING.md)):

```bash
curl -s -X POST $HUB/admin/invites -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"note":"시연 DemoBank"}'
```

DemoBank가 키 쌍을 만들고(개인키는 자기만), 초대 코드로 스스로 등록한다. 인증 없이 부르는 유일한 API다 (SPEC 3.5):

```bash
read DEMO_PRIVATE DEMO_PUBLIC < <(uv run python -c "from clearing_hub.core.signing import generate_keypair; print(*generate_keypair())")
curl -s -X POST $HUB/v1/banks/register -H "Content-Type: application/json" -d "{\"invite_code\":\"<inv_…>\",\"name\":\"DemoBank\",\"base_url\":\"https://demo-bank.invalid/interbank\",\"public_key\":\"$DEMO_PUBLIC\"}"
```

`201`: `bank_code`(배정된 코드, `export DEMO=<bank_code>`), `api_key`(이때만), `hub_public_key`·`hub_fingerprint`, `net_debit_cap: 1000000000`(기본 10억 — 예치금 0으로도 바로 보낼 수 있다), `ping`. DemoBank는 받는 서버가 없는 시연용이라 `ping.ok`가 `false`지만 **등록은 된다** (진짜 은행이면 서버를 고친 뒤 `POST /v1/me/ping`으로 다시 확인한다). `GET /admin/invites`에서 그 코드는 `USED`.

한도 검사를 보이려고 관리자가 DemoBank의 한도를 0으로 내린다 (`/admin/reserve`에서 `$DEMO`는 0·0·0·0):

```bash
curl -s -X PATCH $HUB/admin/banks/$DEMO -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"net_debit_cap":0}'
```

DemoBank가 902에 1만 원을 보낸다:

```bash
export BANK_CODE=$DEMO BANK_KEY=<DemoBank api_key> BANK_PRIVATE_KEY=$DEMO_PRIVATE
uv run python hub_call.py POST /v1/transfers "{\"transfer_id\":\"$(uv run python -c 'import uuid;print(uuid.uuid4())')\",\"to_bank\":\"902\",\"to_account\":\"110-123-0001\",\"from_account\":\"990-000-0001\",\"amount\":10000,\"sender_name\":\"시연\"}"
```

`400` — 접수 전에 거절되고 받는 은행까지 가지 않는다:

```json
{ "error": "LIMIT_EXCEEDED", "message": "순채무한도를 넘었습니다 (예치금 0원 + 한도 0원, 미정산 순채무 0원, 요청 10,000원)", "status": "FAILED" }
```

`$DEMO`에 5만 원을 예치하고(`POST /admin/reserve/$DEMO/deposit`) 같은 명령(새 `transfer_id`)을 다시 보내면 `200 COMPLETED`. `GET /v1/reserve`(DemoBank 키로)는 `balance 50,000 · exposure 10,000 · available 40,000`.

## 3. 하루치 거래

| # | 누가 | 무엇 | 결과 |
|---|---|---|---|
| ① ② | 발표자 (AvengersBank 모아) | "SimBank 110-123-0001로 5만 원 보내줘" 두 번 | 각각 `COMPLETED` |
| ③ | 관리자 (902 키로 허브 API) | 902 → 901 3만 원 (가짜 은행은 스스로 보내지 못한다) | `COMPLETED`, AvengersBank 계좌에 입금 |

③의 명령 (`to_account`는 AvengersBank 시연 계좌):

```bash
export BANK_CODE=902 BANK_KEY=<902 api_key> BANK_PRIVATE_KEY=$(sudo grep '^SIM_SIGNING_KEY=' /srv/clearing-hub/sim.env | cut -d= -f2-)
uv run python hub_call.py POST /v1/transfers "{\"transfer_id\":\"$(uv run python -c 'import uuid;print(uuid.uuid4())')\",\"to_bank\":\"901\",\"to_account\":\"110-900-000001\",\"from_account\":\"110-123-0001\",\"amount\":30000,\"sender_name\":\"김철수\"}"
```

`/admin/reserve` — 예치금은 그대로이고 **미정산 순채무**가 쌓인다:

| 은행 | `balance` | `exposure` | `available` | 뜻 |
|---|---|---|---|---|
| 901 | 1,000,000 | 70,000 | 930,000 | 10만 보내고 3만 받음 |
| 902 | 1,000,000 | −80,000 | 1,080,000 | 11만 받고 3만 보냄. 받은 만큼 더 보낼 수 있다 |
| `$DEMO` | 50,000 | 10,000 | 40,000 | |

> 901의 송금은 반드시 모아 화면(AvengersBank)으로 한다. 관리자가 901 키로 허브에 직접 보내면 AvengersBank 장부에 없는 거래가 되어 5의 대사가 `MISMATCH`가 된다.

## 4. 마감 상계 — "하루치를 은행마다 숫자 하나로"

```bash
curl -s -X POST $HUB/admin/settlements/run -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d "{\"settle_date\":\"$(TZ=Asia/Seoul date +%F)\"}"
```

`201`, `status: DRAFT`, `tx_count 4`, `total_amount 140,000`. 응답의 `run_id`를 적어 둔다 (`export RUN=<run_id>`).

| `positions` | 보낸 건수·합계 | 받은 건수·합계 | `net_amount` |
|---|---|---|---|
| 901 | 2 · 100,000 | 1 · 30,000 | **−70,000** |
| 902 | 1 · 30,000 | 3 · 110,000 | **+80,000** |
| `$DEMO` | 1 · 10,000 | 0 · 0 | **−10,000** |
| 합 | | | **0** |

거래 4건이 예치금 이동 3번으로 줄었다 (다자간 차액). 쌍별 표 `pairs`(화면용): `901–902` 10만 / 3만 → 7만 `a_to_b`, `902–$DEMO` 0 / 1만 → 1만 `b_to_a`. CSV:

```bash
curl -s $HUB/admin/settlements/$RUN/transfers.csv -H "X-Admin-Token: $TOKEN"
```

헤더 `transfer_id,from_bank,to_bank,amount,finished_at,holder_name`와 4행.

## 5. 대사 — "은행이 허브 기록을 자기 장부와 맞춰 본다"

허브 쪽에서 보이는 것: 901의 대사 파일에는 **901의 몫만** 있다 (DemoBank의 거래, 다른 은행의 포지션, 계좌번호·이름이 없다). 901의 개인키는 AvengersBank 서버에만 있으므로 **허브 관리자는 901 명의로 부를 수 없다** — AvengersBank 팀이 자기 키로 부른다 (허브가 은행 비밀을 갖지 않는다는 것을 보여 주는 장면이다).

```bash
export BANK_CODE=901 BANK_KEY=<901 api_key> BANK_PRIVATE_KEY=<AvengersBank 서버의 INTERBANK_SIGNING_KEY>   # AvengersBank 팀이, 그 서버에서
uv run python hub_call.py GET /v1/settlements/$RUN
```

`transfers`: `SENT 902 50,000`, `SENT 902 50,000`, `RECEIVED 902 30,000`. `position.net_amount −70,000`, `result null`(DRAFT). 901의 `GET /v1/reserve`는 아직 `exposure 70,000` — DRAFT로는 순채무가 줄지 않는다.

AvengersBank 쪽: 30초 안에 서버 로그에 `허브 정산 대사: run <RUN> MATCHED`, `settlement_reconciliations`에 그 run이 `MATCHED`, `hub_net_amount = our_net_amount = −70000`, `hub_status DRAFT`, `settled_at` 비어 있음. (같은 하루치를 가짜 허브 응답으로 증명하는 테스트: `avengers_bank/tests/test_settlement_sync.py` B1)

## 6. 확정 — "차액만큼 예치금이 옮겨진다"

```bash
curl -s -X POST $HUB/admin/settlements/$RUN/confirm -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"reason":"시연: 대사 완료"}'
```

`200`, `status: SETTLED`, `settled_by: admin`. `/admin/reserve`:

| 은행 | `balance` | `exposure` | `available` | 분개 (`/admin/reserve/{code}/ledger`) |
|---|---|---|---|---|
| 901 | **930,000** | 0 | 930,000 | `SETTLE_DEBIT −70,000` |
| 902 | **1,080,000** | 0 | 1,080,000 | `SETTLE_CREDIT +80,000` |
| `$DEMO` | **40,000** | 0 | 40,000 | `SETTLE_DEBIT −10,000` |

`available`은 확정 전과 같다: 순채무에서 빠진 금액과 예치금에서 빠진 금액이 한 트랜잭션에서 바뀐다. 같은 run을 다시 확정하면 `409 SETTLEMENT_NOT_DRAFT`, 잔액은 그대로.

## 7. 은행 장부 정리 — "은행도 외상을 지운다"

AvengersBank 워커가 다음 주기(30초 안)에 SETTLED를 보고 장부를 정리한다. 로그 `정산 run <RUN> 장부 정리: 허브 예치금 -70000원 (거래 3건)`. AvengersBank 내부 계정:

| 계정 | 정리 전 | 정리 후 |
|---|---|---|
| `INTERBANK_SETTLEMENT:902` (902에 줄 외상) | +70,000 | **0** |
| `HUB_RESERVE` (허브에 맡긴 예치금의 변화) | 0 | **−70,000** |

`settlement_reconciliations.settled_at`이 채워진다. `HUB_RESERVE`는 확정된 run 차액의 누적일 뿐이라 허브의 901 `balance`(930,000, 관리자 예치 포함)와 같은 숫자가 아니다.

## 8. 속이는 은행 — "데이터로만 주고받는데 어떻게 아느냐"

모아에게 "SimBank **110-123-0006**으로 2만 원 보내줘". 가짜 은행은 이 계좌로 온 입금을 **처리하지 않고** `200 COMPLETED`라고 답한다.

| 어디 | 보이는 것 |
|---|---|
| AvengersBank·허브 | 이체 `COMPLETED`. 허브는 받는 은행의 2xx를 입금 완료로 본다 (SPEC 4.2) |
| 가짜 은행 장부 | 없다. `GET /interbank/receive/{id}`는 `404 TRANSFER_NOT_FOUND`, 가짜 은행 `/`의 `received` 수도 그대로 |
| 새 run (4의 명령) | `tx_count 1`: 901 `−20,000`, 902 `+20,000` |
| 902의 대사 파일 (902 키로 `GET /v1/settlements/<새 run_id>`) | 그 `transfer_id`가 `RECEIVED 901 20,000`으로 있다 |

허브 기록에는 있는데 902 장부에는 없다 — 902가 대사하면 이 거래가 **허브에만 있음**(`hub_only`)으로 나오고 그 run은 `MISMATCH`다. "입금했다"는 말은 거짓이었고, 그 증거는 확정 전에 두 장부를 맞춘 결과다. 902가 확정에 동의하지 않으면 관리자는 확정하지 않고 원인을 확인한다 (`POST /admin/settlements/<run_id>/cancel`로 DRAFT를 취소하면 거래는 다음 run으로 돌아간다).

> 가짜 은행은 장부가 없어 스스로 대사하지 않는다 (SETTLEMENT_PLAN 11절). `MISMATCH` 화면을 AvengersBank에서 직접 보이려면 반대 방향이 필요하다: 허브에는 있는데 AvengersBank 장부에는 없는 거래 (예: 시연 DB에서 `interbank_ledger`의 `RECEIVE` 줄 하나를 지우고 다음 대사를 기다리면 `MISMATCH`, `diff_json.hub_only`에 그 `transfer_id`). 운영 DB에서는 하지 않는다.

## 정리 (시연 뒤)

```bash
curl -s -X PATCH "$HUB/admin/banks/$DEMO/active?active=false" -H "X-Admin-Token: $TOKEN"
```

DemoBank는 지울 수 없으므로 비활성으로 둔다 (`/v1/banks` 목록에서 빠지고 그 코드로는 보내거나 받을 수 없다). 다음 시연에서 같은 `base_url`로 다시 셀프 등록하면 `409 BANK_EXISTS`이므로, `active=true`로 되살려 쓰거나 다른 `base_url`(예: `https://demo-bank-2.invalid/interbank`)로 등록한다. 8의 DRAFT는 확정하거나 취소한다. AvengersBank의 `INTERBANK_SETTLEMENT_SYNC_INTERVAL_SECONDS`를 지운다.

## 발표에서 말할 것

> 은행 간 이체는 서로 다른 두 DB에서 일어나서, 받는 은행이 "입금했다"고 답하는 것 말고는 증거가 없습니다. 실제 금융망이 이 문제를 푸는 방법은 기술 하나가 아니라 **신뢰를 강제하는 장부 체계**입니다. 우리는 그 장치를 코드로 옮겼습니다. 요청마다 **서명**해 누가 보냈는지 확인하고, 같은 거래 ID는 한 번만 처리하는 **멱등**으로 재시도에도 돈이 두 번 움직이지 않게 하고, 모든 거래를 양쪽이 아닌 **제3자(허브)의 기록**으로 남깁니다. 은행은 **예치금**을 맡겨 두고 **순채무한도** 안에서만 보낼 수 있으며, 하루치 거래는 은행마다 차액 하나로 **상계**해 예치금에서 옮깁니다. 마지막으로 은행이 허브 기록을 자기 장부와 **대사**해, 방금처럼 입금 없이 완료라고 답한 거래를 확정 전에 찾아냅니다. 서명은 은행마다 자기만 가진 개인키로 하는 공개키 서명이라, 허브조차 은행 명의의 요청을 만들 수 없고, 받는 은행이 "입금했다"고 답한 응답에도 서명이 남아 나중에 부인할 수 없습니다. 새 은행은 관리자가 보낸 초대 코드 하나로 스스로 등록하고 바로 거래를 시작합니다.
