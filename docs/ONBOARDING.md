# 참가 은행 받기 (허브 관리자용)

관리자가 하는 일은 셋뿐이다. 등록·키·주소 변경·연결 확인·받는 쪽 시험·정산 확정은 은행과 허브가 스스로 한다 ([SPEC.md](SPEC.md) 7절, 3.7, 10.8).

1. **초대 코드 발급·지문 전달** — 새 팀이 오면 (1)
2. **알림 대응** — dispute, `OVERDUE`, `UNKNOWN`, ping 실패 등이 관리자 채널(`ALERT_WEBHOOK_URL`)에 오면 (5)
3. **예치금·한도 요청 처리** — 은행이 더 보내야 한다고 하면 (4)

팀이 "왜 401이 나요?"라고 물으면 `POST /admin/banks/{code}/diagnose` 결과를 그대로 보내 준다 (7). 은행이 같은 진단을 `GET /v1/diagnose/failures`로 스스로 볼 수 있다.

이 문서는 그 셋을 할 때 보는 곳을 모은 것이다.

명령은 모두 Session Manager(서버 안)에서 친다. 관리자 토큰은 화면·채팅에 띄우지 않는다.

```bash
export HUB=https://clearing-hub.ai-agent-develop.cloud TOKEN=$(sudo grep '^ADMIN_TOKEN=' /srv/clearing-hub/.env | cut -d= -f2)
```

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

그다음은 은행이 한다: 키 쌍 만들기 → 받는 쪽 API 3개 → `POST /v1/banks/register` → ping 확인 → 가짜 은행 `902`로 보내기 시험 → 셀프 시험(`POST /v1/test-receive`)으로 받기 시험. 등록되면 관리자 채널에 알림(`register`, ping 결과 포함)이 온다.

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

## 3. 받는 쪽 시험 (은행이 스스로)

관리자가 이체를 보내 줄 필요가 없다. 은행이 셀프 시험 API(SPEC 3.7)를 부르면 허브가 가짜 은행 `902` 명의로 그 은행에 시험 이체를 만든다 (`POST /v1/test-receive`, 멱등 시험 `/resend`, 4.3 시험 `/status`, 은행별 하루 20번). 관리자가 볼 것은 둘이다.

- 은행 서버가 5xx를 주거나 응답 서명이 틀리면 알림 `self_test`가 온다. 그 은행으로 가는 실제 이체도 `PENDING`이 될 상태이므로, 은행에 고치라고 알린다 (은행도 응답의 `receiver_reply`로 같은 것을 본다)
- 알림 `sim_bank`(가짜 은행 한도 부족·비활성)가 오면 902에 예치금을 넣는다: `POST /admin/reserve/902/deposit` (4와 같은 명령). 셀프 시험 거래는 902가 낸 돈으로 정산에 들어가기 때문이다

시험 거래는 `GET /admin/transfers`에 `from_bank 902`로 보이고, 내용은 `/admin/transfers/<id>/events`·`/evidence`로 본다.

## 4. 예치금·한도 요청

새 은행은 예치금 0, 한도 10억으로 시작해 바로 보낼 수 있다. 더 필요하다고 하면:

```bash
curl -s $HUB/admin/reserve -H "X-Admin-Token: $TOKEN"
curl -s -X POST $HUB/admin/reserve/903/deposit -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"amount":1000000,"memo":"요청: C팀"}'
curl -s -X PATCH $HUB/admin/banks/903 -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"net_debit_cap":2000000000}'
```

모의 예치금이라 실제 돈과 무관하다. 분개는 `GET /admin/reserve/903/ledger`.

## 5. 알림이 오면 (문제가 생겼을 때 보는 곳)

알림은 `ALERT_WEBHOOK_URL`(Slack·Discord)로 온다. 글 맨 앞 `[ClearingHub·종류]`가 아래 표의 종류이고, 둘째 줄에 `run_id`·`bank_code`·`transfer_id`가 붙는다. 웹훅을 넣지 않았으면 같은 내용이 서버 로그(`logger.error`·`warning`)에만 남는다.

정산 확정은 관리자 일이 아니다: 포지션이 있는 은행이 모두 ack하면 즉시(`settled_by: auto-ack`), 아니면 매일 09:00 KST에 dispute가 없으면(`auto-deadline`) 확정되고 알림 `auto_confirm`이 온다 (SPEC 10.8). 관리자는 **dispute가 왔을 때만** 판정한다.

| 알림 (종류) | 보는 곳 | 할 일 |
|---|---|---|
| 대사 불일치 (`dispute`), 마감 보류 (`deadline_held`), 배치 건너뜀 (`batch_skipped`) | `GET /admin/settlements/<run_id>`(`acks`·`disputed`, 은행이 짚은 `transfer_ids`), `/admin/settlements/<run_id>/transfers.csv`, `GET /admin/transfers/<transfer_id>/evidence` | 증거 원문(서명된 요청·응답)으로 누가 무엇이라고 답했는지 확인. 허브 기록이 맞으면 `POST /admin/settlements/<run_id>/confirm`(강제 확정, 돈을 바로잡을 일은 예치금 정정), 아니면 `POST /admin/settlements/<run_id>/cancel`(거래는 다음 run으로, ack 기록은 남는다). **판정하기 전에는 다음 날 배치가 건너뛴다.** 은행이 원인을 고쳐 ack를 다시 보내면 dispute가 풀리고 자동 확정된다 |
| 확정 결과 `OVERDUE` (`overdue`) | `GET /admin/settlements/<run_id>`의 `positions[].result`, `GET /admin/reserve` | 그 은행의 송금이 막혀 있다 (받기는 된다). 은행에 알리고 예치금 입금 또는 한도 상향 (4) |
| 거래 `UNKNOWN` (`unknown`) | `GET /admin/transfers?status=UNKNOWN`, `/admin/transfers/<id>/events` | 받는 은행이 고쳐졌으면 `recheck`, 아니면 받는 은행과 확인 뒤 `resolve` (DEPLOY.md "UNKNOWN 거래 정리") |
| ping 실패 (`ping_failed`: 은행의 `PATCH /v1/me` 주소 변경이 거절됨), 셀프 등록 ping 실패 (`register`) | 알림 둘째 줄의 `원인 후보`, `POST /admin/banks/<code>/ping` | 주소는 바뀌지 않았다 (은행도 같은 결과를 받았다). 그 은행이 고치는지 지켜보고, 오래 안 되면 연락한다. 은행도 `GET /v1/diagnose/failures`로 같은 원인 후보를 본다 (7) |
| 셀프 시험 5xx·서명 실패 (`self_test`), 가짜 은행 문제 (`sim_bank`) | 3 | 3 |
| 어떤 은행으로 가는 이체가 계속 `PENDING` (알림 없음, 은행이 알려 옴) | `POST /admin/banks/<code>/ping` | `response_signature_valid: false`거나 연결 안 됨. 그 은행에 ping 결과를 보낸다 |
| 은행 요청이 계속 401 ("왜 401이 나요?") | 7 | `POST /admin/banks/<code>/diagnose` 결과를 그대로 보내 준다 |

## 6. 키 문제

| 상황 | 할 일 |
|---|---|
| 은행 **개인키 유출** | 급하면 먼저 막는다: `PATCH /admin/banks/<code>/active?active=false` (그 은행 명의 요청이 바로 401, 그 은행으로도 못 보낸다). 은행이 새 키로 회전(`PUT /v1/keys`, 옛 키로 서명)하고 나면 다시 `active=true`. 은행이 옛 키로 서명할 수 없으면 관리자가 새 공개키를 등록한다 (아래). 옛 키는 **24시간 동안 계속 받으므로**, 그 사이에는 비활성으로 두는 편이 안전하다 |
| 은행이 개인키를 **잃어버림** | 은행에게 새 공개키와 지문을 받아 `POST /admin/banks/<code>/public-key` `{"public_key":"…"}`. 지문이 은행이 말한 값과 같은지 응답의 `public_key_fingerprint`로 확인 |
| **API 키** 유출·분실 | `POST /admin/banks/<code>/keys` → 새 `api_key`(이때만 보인다)를 그 팀에 개별로. 옛 API 키는 바로 401. API 키는 식별용이라 이것만으로는 은행 명의 요청을 만들 수 없다 |
| 은행이 `base_url`을 바꿈 | 은행이 `PATCH /v1/me`로 스스로 한다 (새 주소로 ping해 정상일 때만 반영). 관리자가 할 때는 `PATCH /admin/banks/<code>` 뒤 ping |
| **허브 개인키**(`HUB_SIGNING_KEY`) 유출 | 새 키를 만들어 `.env`에 넣고 다시 만든다 (DEPLOY.md "허브 키 만들기"). 모든 은행이 허브 공개키를 새로 받아야 하므로 전 은행에 새 지문을 개별로 알린다. 유출이 아니면 바꾸지 않는다 |

허브는 은행의 비밀을 갖지 않는다 (공개키·API 키 해시뿐). 허브 DB가 새도 은행 명의의 요청은 만들 수 없지만, `ADMIN_TOKEN`이 새면 관리자 API로 공개키를 바꿀 수 있으므로 토큰은 `.env`에만 둔다.

## 7. 팀이 401로 막혔다고 하면 (연동 지원)

**먼저 은행이 스스로 보게 한다.** API 키가 맞은 은행의 401 본문에는 1순위 가설 한 줄(`hint`)이 이미 들어 있다 (SPEC 5절). 은행은 서명 없이 `X-Bank-Code`·`X-Api-Key`만으로 `GET /v1/diagnose/failures`를 불러 자기 인증 실패와 원인 가설을 본다 (SPEC 3.8, 7절 8). 허브가 실패한 요청을 흔한 실수 변형(`/v1` 빠짐, JSON 다시 직렬화, 경로 뒤 줄바꿈 누락, hex 서명, 밀리초 등)으로 다시 계산해 맞는 것을 찾으므로, 1순위 가설이 `verified: true`면 그것이 원인이다.

그래도 물어 오면 관리자가 같은 진단을 돌려 **결과를 그대로 보내 준다**:

```bash
curl -s -X POST $HUB/admin/banks/903/diagnose -H "X-Admin-Token: $TOKEN" -H "Content-Type: application/json" -d '{"question":"POST /v1/transfers가 401"}' | python3 -m json.tool
```

- 관리자 진단은 은행 진단과 같고, 추가로 **API 키가 틀려 은행이 볼 수 없는 실패**(`X-Bank-Code`만 그 은행인 것)도 넣는다. 1순위가 `API_KEY_MISMATCH`면 은행이 API 키를 잘못 넣었거나 재발급 전 키를 쓰는 것이다 (필요하면 6의 API 키 재발급)
- `explanation`은 LLM(Gemini) 설명이다(`GEMINI_API_KEY`가 있고 `question`이나 `snippet`을 넣었을 때만, 아니면 `null`). 원인 판단은 `findings[].hypotheses`가 근거이고, 설명은 그것을 문장으로 풀어 쓴 것이다
- 실패만 빠르게 보려면 `GET /admin/banks/903/auth-failures?limit=50` (LLM 없음)
- 은행 서버가 허브 요청을 401·403으로 거절했거나 응답 서명이 틀린 것(`direction: HUB_TO_BANK`)도 같은 목록에 나온다. 이때 가설은 은행 쪽 검증 코드(`BANK_VERIFY_PATH` 등)나 응답 서명(`RESPONSE_*`)이다
- 실패는 은행별 최근 200건만 남는다. `X-Api-Key` 값은 저장하지 않는다
