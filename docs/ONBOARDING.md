# 참가 은행 받기 (허브 관리자용)

새 팀이 연동하러 오면 관리자가 할 일은 **초대 코드 하나 보내기**다. 등록·키·주소 변경·연결 확인은 은행이 [SPEC.md](SPEC.md) 7절대로 스스로 한다. 이 문서는 그 앞뒤에서 관리자가 보는 곳을 모은 것이다.

명령은 모두 Session Manager(서버 안)에서 친다. 관리자 토큰은 화면·채팅에 띄우지 않는다.

```bash
export HUB=https://clearing-hub.ai-agent-develop.cloud TOKEN=$(sudo grep '^ADMIN_TOKEN=' /srv/clearing-hub/.env | cut -d= -f2)
```

> **곧 자동**으로 표시한 것은 다음 단계(SETTLEMENT_PLAN 7절 9단계)에서 관리자 손을 떠날 일이다. 지금은 관리자가 한다.

## 1. 팀이 오면: 초대 코드 + 지문

```bash
curl -s -X POST $HUB/admin/invites -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"note":"C팀 (홍길동)"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["message"])'
```

출력된 **초대 문구(`message`)를 그대로 복사해 그 팀 담당자에게만 DM으로** 보낸다 (GitHub·단체 채팅방 X). 응답의 `code`(`inv_…`, 7일 동안 한 번)는 **이때만 보인다.** 문구에 들어 있는 것:

- 초대 코드와 만료 시각 (KST와 UTC)
- 허브 주소: 설정 `HUB_PUBLIC_URL`, 없으면 이 요청의 주소. 서버 안에서 내부 주소로 부르면 그 주소가 들어가므로 운영 `.env`에는 `HUB_PUBLIC_URL`을 넣어 둔다
- 연동 규격 위치 `docs/SPEC.md` (7절부터)
- 허브 공개키와 지문 (등록 응답의 `hub_fingerprint`와 맞춰 보라는 말)
- 등록 방법 한 줄: `POST /v1/banks/register`에 `invite_code`·`name`·`base_url`·`public_key`
- "코드는 1회용이며 공개 채널에 올리지 말 것"

문구 예 (값은 발급마다 다르다):

```
ClearingHub 참가 초대입니다.
- 초대 코드: inv_…
- 만료: 2026-11-13 12:00 (KST, 2026-11-13T03:00:00Z)
- 허브 주소: https://clearing-hub.ai-agent-develop.cloud
- 연동 규격: ClearingHub 저장소의 docs/SPEC.md (7절 연동 절차부터)
- 허브 공개키: …
- 허브 공개키 지문: … (등록 응답의 hub_fingerprint가 이 값과 같은지 꼭 확인하세요)
- 등록 방법: 키 쌍을 만들고 받는 쪽 API를 띄운 뒤 POST https://clearing-hub.ai-agent-develop.cloud/v1/banks/register 에 invite_code·name·base_url·public_key를 보냅니다 (SPEC 3.5)
초대 코드는 한 번만 쓸 수 있습니다. 공개 채널(GitHub·단체 채팅방 등)에 올리지 마세요.
```

공개키·지문은 `curl -s $HUB/v1/hub-key`의 값과 같다. 지문을 초대 문구와 같은 DM으로 받으니, 은행은 API 응답만 믿지 않고 이 값과 맞춰 볼 수 있다. 9절(본인 계좌 조회)에 참여하는 팀이면 `CI_KEY`도 같은 DM으로 (`sudo grep '^CI_KEY=' /srv/clearing-hub/.env | cut -d= -f2`, DEPLOY.md "CI_KEY").

그다음은 은행이 한다: 키 쌍 만들기 → 받는 쪽 API 3개 → `POST /v1/banks/register` → ping 확인 → 가짜 은행 `902`로 보내기 시험.

**관리자가 직접 등록할 때** (코드를 정해야 하는 시연용 은행 등): 은행에게 공개키를 받아 `POST /admin/banks` `{"bank_code","name","base_url","public_key"}`(한도 `net_debit_cap`은 빼면 10억). 응답의 `api_key`는 이때만 나온다.

## 2. 등록됐는지 확인

```bash
curl -s $HUB/admin/invites -H "X-Admin-Token: $TOKEN"
```
```bash
curl -s $HUB/admin/banks -H "X-Admin-Token: $TOKEN"
```

| 볼 것 | 정상 |
|---|---|
| 초대 코드 `status` | `USED`, `used_by_bank_code`에 배정된 코드 (`UNUSED`면 아직, `EXPIRED`면 새로 발급) |
| 은행 `public_key_fingerprint` | 은행이 알려 준 자기 지문과 같다. 다르면 그 은행에 알린다 (중간에서 바뀐 키일 수 있다) |
| 은행 `net_debit_cap` | 기본 `1000000000` (10억) |
| ping | 아래 명령이 `"ok": true` |

```bash
curl -s -X POST $HUB/admin/banks/903/ping -H "X-Admin-Token: $TOKEN"
```

`ok`가 `false`면 결과 읽는 법은 SPEC 3.6 표. 은행도 같은 결과를 `POST /v1/me/ping`으로 볼 수 있으니 결과를 그대로 전해 주면 된다. **응답 서명이 틀린 은행(`response_signature_valid: false`)으로 가는 이체는 모두 `PENDING`이 된다** — 다른 은행이 그 은행으로 보내기 전에 고치게 한다.

## 3. 받는 쪽 시험 요청 (곧 자동)

가짜 은행 `902`는 보내지 않으므로, 은행이 받는 쪽(SPEC 7절 7)을 시험하려면 관리자가 AvengersBank(`901`, 모아 화면)에서 그 은행 시험 계좌로 소액 이체를 보낸다. 결과는 `GET /admin/transfers?status=...`와 `/admin/transfers/<id>/events`로 함께 본다.

> 곧 자동: 은행이 직접 부르는 셀프 시험 API(`test-receive`).

## 4. 예치금·한도 요청

새 은행은 예치금 0, 한도 10억으로 시작해 바로 보낼 수 있다. 더 필요하다고 하면:

```bash
curl -s $HUB/admin/reserve -H "X-Admin-Token: $TOKEN"
curl -s -X POST $HUB/admin/reserve/903/deposit -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"amount":1000000,"memo":"요청: C팀"}'
curl -s -X PATCH $HUB/admin/banks/903 -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"net_debit_cap":2000000000}'
```

모의 예치금이라 실제 돈과 무관하다. 분개는 `GET /admin/reserve/903/ledger`.

## 5. 문제가 생겼을 때 보는 곳

| 상황 | 보는 곳 | 할 일 |
|---|---|---|
| 정산 DRAFT가 확정을 기다림 (매일 00:05 KST에 생김) | `GET /admin/settlements` | 은행들의 대사가 끝났으면 `POST /admin/settlements/<run_id>/confirm`. **곧 자동**: 은행 ack/dispute로 자동 확정 |
| 확정 뒤 `OVERDUE` 은행 | 확정 응답의 `positions[].result`, `GET /admin/reserve` | 그 은행의 송금이 막혀 있다 (받기는 된다). 은행에 알리고 예치금 입금 또는 한도 상향 (4). **곧 자동**: 알림 웹훅 |
| 은행이 대사 불일치를 알려 옴 (`run_id`, `transfer_id`) | `GET /admin/settlements/<run_id>`, `/admin/settlements/<run_id>/transfers.csv`, `GET /admin/transfers/<transfer_id>/evidence` | 확정하지 않는다. 증거 원문(서명된 요청·응답)으로 누가 무엇이라고 답했는지 확인. DRAFT면 `POST /admin/settlements/<run_id>/cancel`(거래는 다음 run으로) |
| `UNKNOWN` 거래 | `GET /admin/transfers?status=UNKNOWN`, `/admin/transfers/<id>/events` | 받는 은행이 고쳐졌으면 `recheck`, 아니면 받는 은행과 확인 뒤 `resolve` (DEPLOY.md "UNKNOWN 거래 정리"). **곧 자동**: 알림 웹훅 |
| 어떤 은행으로 가는 이체가 계속 `PENDING` | `POST /admin/banks/<code>/ping` | `response_signature_valid: false`거나 연결 안 됨. 그 은행에 ping 결과를 보낸다 |
| 은행 요청이 계속 401 | 은행이 받은 `message` | `등록된 공개키가 없습니다` → 공개키 등록(6), `서명이 맞지 않습니다` → 은행의 서명 코드·키 (SPEC 2.3 예시 값으로 확인) |

## 6. 키 문제

| 상황 | 할 일 |
|---|---|
| 은행 **개인키 유출** | 급하면 먼저 막는다: `PATCH /admin/banks/<code>/active?active=false` (그 은행 명의 요청이 바로 401, 그 은행으로도 못 보낸다). 은행이 새 키로 회전(`PUT /v1/keys`, 옛 키로 서명)하고 나면 다시 `active=true`. 은행이 옛 키로 서명할 수 없으면 관리자가 새 공개키를 등록한다 (아래). 옛 키는 **24시간 동안 계속 받으므로**, 그 사이에는 비활성으로 두는 편이 안전하다 |
| 은행이 개인키를 **잃어버림** | 은행에게 새 공개키와 지문을 받아 `POST /admin/banks/<code>/public-key` `{"public_key":"…"}`. 지문이 은행이 말한 값과 같은지 응답의 `public_key_fingerprint`로 확인 |
| **API 키** 유출·분실 | `POST /admin/banks/<code>/keys` → 새 `api_key`(이때만 보인다)를 그 팀에 개별로. 옛 API 키는 바로 401. API 키는 식별용이라 이것만으로는 은행 명의 요청을 만들 수 없다 |
| 은행이 `base_url`을 바꿈 | 은행이 `PATCH /v1/me`로 스스로 한다 (새 주소로 ping해 정상일 때만 반영). 관리자가 할 때는 `PATCH /admin/banks/<code>` 뒤 ping |
| **허브 개인키**(`HUB_SIGNING_KEY`) 유출 | 새 키를 만들어 `.env`에 넣고 다시 만든다 (DEPLOY.md "허브 키 만들기"). 모든 은행이 허브 공개키를 새로 받아야 하므로 전 은행에 새 지문을 개별로 알린다. 유출이 아니면 바꾸지 않는다 |

허브는 은행의 비밀을 갖지 않는다 (공개키·API 키 해시뿐). 허브 DB가 새도 은행 명의의 요청은 만들 수 없지만, `ADMIN_TOKEN`이 새면 관리자 API로 공개키를 바꿀 수 있으므로 토큰은 `.env`에만 둔다.
