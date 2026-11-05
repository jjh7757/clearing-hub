# ClearingHub 참가 은행 연동 규격 v0.2 (초안)

- 작성일: 2026-11-05
- 대상: ClearingHub(은행 간 이체 중계 서버)에 참가하려는 은행 서버 개발팀
- 상태: **초안**. 8절 "합의가 필요한 것"을 정한 뒤 v1.0으로 확정한다
- 배경·전체 설계: [PROJECT_PLAN.md](PROJECT_PLAN.md)

이 문서는 두 가지를 정한다.

1. 은행이 허브에 **보내는** 요청 (3절)
2. 은행이 허브로부터 **받기 위해 만들어야 하는** API (4절)

둘 다 같은 인증 방식(2절)과 오류 형식(5절)을 쓴다.

---

## 1. 기본 규칙

| 항목 | 규칙 |
|---|---|
| 프로토콜 | HTTPS, JSON (`Content-Type: application/json; charset=utf-8`) |
| 문자 | UTF-8 |
| 금액 | **정수, 원 단위** (예: 50000). 소수·문자열·음수·0은 `VALIDATION_ERROR`(422)로 거절. 허브 1회 상한(1억 원) 초과는 `LIMIT_EXCEEDED`(400) |
| 은행 코드 | 허브가 배정하는 숫자 3자리 문자열 (예: `"901"`). 앞자리 0이 있을 수 있으므로 문자열로 다룬다 |
| 계좌번호 | 문자열. 허브는 형식을 검사하지 않고 받는 은행에 그대로 전달한다. 하이픈 포함 여부는 각 은행이 정한다 (예: `"110-123-456789"`) |
| 거래 ID (`transfer_id`) | 보내는 은행이 만드는 UUID v4 문자열(소문자). **같은 ID는 어디서든 한 번만 처리된다** (멱등성 키) |
| 시각 | ISO 8601, UTC, 초 단위 (`"2026-11-05T01:20:00Z"`). 응답에만 쓰인다 |
| 이름 | `sender_name`, `holder_name`은 최대 50자 |
| 메모 | `memo`는 최대 100자, 없으면 `""` |
| 타임아웃 | 허브 → 은행 호출은 **5초**. 은행 → 허브 호출은 10초를 권장 |

---

## 2. 인증: API 키 + HMAC 서명

허브는 은행마다 **API 키**와 **서명 비밀키(secret)**를 발급한다. 은행 → 허브, 허브 → 은행 요청 **모두** 같은 방식으로 서명한다. 받는 쪽은 서명이 맞을 때만 처리한다.

### 2.1 헤더

| 헤더 | 값 |
|---|---|
| `X-Bank-Code` | 요청을 보내는 쪽의 은행 코드. 허브가 보낼 때는 `"000"` |
| `X-Api-Key` | 발급받은 API 키 (허브가 은행에 보낼 때는 은행이 허브에 준 키, 없으면 생략 가능. 8절) |
| `X-Timestamp` | Unix 시각(초), 10진 문자열. 예: `"1762300800"` |
| `X-Signature` | 아래 방법으로 만든 HMAC-SHA256, 소문자 16진수 64자 |

### 2.2 서명 만들기

```
message   = X-Timestamp + "\n" + body
signature = hex( HMAC-SHA256( key = secret, message = UTF-8 bytes of message ) )
```

- `body`는 **실제로 전송하는 바이트 그대로**다. 서명한 뒤에 JSON을 다시 직렬화하면 안 된다 (키 순서·공백이 바뀌면 서명이 틀린다). 본문이 없는 GET은 `body = ""`.
- 받는 쪽은 `X-Timestamp`가 현재 시각과 **5분(300초)** 넘게 차이 나면 `SIGNATURE_EXPIRED`로 거절한다 (재전송 공격 방지).
- 서명 비교는 상수 시간 비교(`hmac.compare_digest`)를 쓴다.

### 2.3 예시 (검증용)

```
secret      = example-secret-change-me
X-Timestamp = 1762300800
body (190 bytes, 공백 없음) =
{"transfer_id":"7c9e6679-7425-40de-944b-e07fc1f90ae7","to_bank":"902","to_account":"110-123-0001","from_account":"110-200-456789","amount":50000,"sender_name":"홍길동","memo":"생활비"}

X-Signature = a05b6c21278364c75bd36df9cbedbbca72e241466da38d7aa2ee3a1d0725a8c7
```

Python 참고 구현:

```python
import hmac, hashlib, time

def sign(secret: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    msg = ts.encode() + b"\n" + body
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return {"X-Timestamp": ts, "X-Signature": sig}

def verify(secret: str, ts: str, body: bytes, signature: str, skew: int = 300) -> bool:
    if abs(int(time.time()) - int(ts)) > skew:
        return False
    expected = hmac.new(secret.encode(), ts.encode() + b"\n" + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

FastAPI에서 본문 바이트는 `await request.body()`로 받아 그대로 검증한 뒤 JSON으로 푼다.

---

## 3. 은행 → 허브 API

기본 주소: `https://clearing-hub.ai-agent-develop.cloud/v1` (API 문서: `https://clearing-hub.ai-agent-develop.cloud/docs`)

### 3.1 참가 은행 목록 `GET /v1/banks`

화면의 "받는 은행" 선택에 쓴다. 인증 필요.

```json
{ "banks": [ { "bank_code": "901", "name": "AvengersBank" }, { "bank_code": "902", "name": "B은행" } ] }
```

### 3.2 수취 조회 `POST /v1/inquiry`

받는 사람 이름을 확인 화면에 보여 주기 위한 것. 돈은 움직이지 않는다.

요청
```json
{ "to_bank": "902", "to_account": "110-123-0001" }
```

응답 `200`
```json
{ "to_bank": "902", "to_account": "110-123-0001", "holder_name": "김철수" }
```

오류: `BANK_NOT_FOUND`(404), `BANK_INACTIVE`(400), `ACCOUNT_NOT_FOUND`(404), `ACCOUNT_INACTIVE`(400), `RECEIVER_TIMEOUT`(504, 받는 은행 응답 없음), `RECEIVER_ERROR`(502, 받는 은행 5xx). 수취 조회는 허브가 재시도하지 않으므로 504·502면 사용자에게 잠시 뒤 다시 시도하라고 안내한다.

### 3.3 이체 요청 `POST /v1/transfers`

**보내는 은행은 이 요청 전에 자기 DB에서 출금을 끝내고**(중간 계정으로 옮기고) 이체 상태를 "처리 중"으로 저장해 둔다. 허브 응답에 따라 완료 또는 되돌림한다 (6절).

요청
```json
{
  "transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "to_bank": "902",
  "to_account": "110-123-0001",
  "from_account": "110-200-456789",
  "amount": 50000,
  "sender_name": "홍길동",
  "memo": "생활비"
}
```

`from_bank`는 본문에 없다. 허브가 인증된 `X-Bank-Code`로 정한다 (사칭 방지).

응답 `200` — 받는 은행이 입금을 확인한 경우
```json
{
  "transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "status": "COMPLETED",
  "holder_name": "김철수",
  "finished_at": "2026-11-05T01:20:03Z"
}
```

응답 `202` — 받는 은행 응답을 아직 못 받은 경우 (`RECEIVER_TIMEOUT`)
```json
{ "transfer_id": "7c9e6679-…", "status": "PENDING" }
```
보내는 은행은 **출금을 되돌리지 말고** 3.4로 결과를 확인한다.

응답 `4xx` — 받는 은행이 거절한 경우 (5절 형식, `status: "FAILED"` 포함). 보내는 은행은 출금을 되돌린다.

같은 `transfer_id`가 다시 오면 허브는 새로 처리하지 않고 저장된 결과를 같은 형식으로 돌려준다 (`DUPLICATE_TRANSFER`가 아니라 **원래 결과**를 준다. 재전송한 쪽이 그대로 마무리할 수 있게 하기 위해서다).

### 3.4 결과 조회 `GET /v1/transfers/{transfer_id}`

자기 은행이 보낸 거래만 조회할 수 있다.

```json
{
  "transfer_id": "7c9e6679-…",
  "status": "COMPLETED",
  "to_bank": "902",
  "amount": 50000,
  "requested_at": "2026-11-05T01:20:00Z",
  "finished_at": "2026-11-05T01:21:10Z",
  "fail_code": null,
  "fail_message": null
}
```

`status` 값: `ACCEPTED`, `DELIVERING`, `PENDING`, `COMPLETED`, `FAILED`, `UNKNOWN`

| 값 | 보내는 은행이 할 일 |
|---|---|
| `ACCEPTED` / `DELIVERING` / `PENDING` | 기다렸다가 다시 조회 (권장: 30초 간격) |
| `COMPLETED` | 이체 완료 처리 |
| `FAILED` | 출금 되돌림 |
| `UNKNOWN` | 허브가 재시도를 다 했는데도 결과를 모름. 허브 관리자가 확인 중. 되돌리지 말고 기다림 |

---

## 4. 허브 → 은행 API (은행이 구현할 것)

은행은 허브에 **`base_url`** 하나를 등록한다 (예: `https://b-bank.example.com/interbank`). 허브는 그 아래 경로를 부른다. 요청에는 2절 서명이 붙어 있으니 검증해야 한다 (`X-Bank-Code: "000"`).

### 4.1 수취 조회 `POST {base_url}/inquiry`

요청
```json
{ "account_num": "110-123-0001" }
```

응답 `200`
```json
{ "holder_name": "김철수" }
```

오류 `404`: `ACCOUNT_NOT_FOUND`, `400`: `ACCOUNT_INACTIVE`

### 4.2 입금 `POST {base_url}/receive`

요청
```json
{
  "transfer_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "from_bank": "901",
  "from_bank_name": "AvengersBank",
  "from_account": "110-200-456789",
  "to_account": "110-123-0001",
  "amount": 50000,
  "sender_name": "홍길동",
  "memo": "생활비"
}
```

응답 `200`
```json
{ "transfer_id": "7c9e6679-…", "status": "COMPLETED", "holder_name": "김철수" }
```

**은행이 지켜야 할 것**

1. **멱등성**: `transfer_id`를 저장해 두고, 같은 ID가 다시 오면 입금하지 않고 `200 COMPLETED`를 다시 돌려준다. 허브는 타임아웃 뒤 같은 요청을 다시 보낸다.
2. 입금과 `transfer_id` 저장은 **한 트랜잭션**으로 한다. 입금만 되고 ID 저장이 안 되면 다음 재시도에서 두 번 들어간다.
3. 거절할 때는 `4xx` + 5절 형식. `ACCOUNT_NOT_FOUND`, `ACCOUNT_INACTIVE`, `AMOUNT_INVALID`, `LIMIT_EXCEEDED`(받는 쪽 한도가 있을 때)
4. 일시 장애는 `5xx`. 허브는 5xx와 타임아웃만 재시도하고 4xx는 재시도하지 않는다.
5. 거래내역에는 보낸 사람 이름과 보낸 은행 이름을 남긴다 (`"AvengersBank 홍길동"`).

### 4.3 입금 확인 `GET {base_url}/receive/{transfer_id}`

허브가 응답을 못 받은 거래를 확인할 때 부른다.

응답 `200`
```json
{ "transfer_id": "7c9e6679-…", "status": "COMPLETED", "holder_name": "김철수", "received_at": "2026-11-05T01:20:02Z" }
```

`holder_name`은 권장 항목이다. 빠져 있으면 허브가 4.1 수취 조회로 이름을 채운다 (실패해도 이체 결과에는 영향 없음). 보내는 은행의 결과 조회(3.4)에서 받는 사람 이름이 비지 않게 하기 위해서다.

응답 `404`: 그 ID로 입금한 적 없음 (`TRANSFER_NOT_FOUND`). 허브는 4.2를 다시 보낸다.

---

## 5. 오류 형식

모든 오류는 HTTP 4xx/5xx + 다음 본문.

```json
{ "error": "ACCOUNT_NOT_FOUND", "message": "받는 계좌를 찾을 수 없습니다", "transfer_id": "7c9e6679-…" }
```

`transfer_id`는 이체 관련 오류에만 붙는다. `message`는 사람이 읽는 설명이며 프로그램은 `error` 코드로 분기한다.

| 코드 | HTTP | 뜻 | 누가 내는가 |
|---|---|---|---|
| `UNAUTHORIZED` | 401 | 은행 코드·API 키 불일치 | 허브·은행 |
| `SIGNATURE_INVALID` | 401 | 서명 불일치 | 허브·은행 |
| `SIGNATURE_EXPIRED` | 401 | 시각 차이 5분 초과 | 허브·은행 |
| `BANK_NOT_FOUND` | 404 | 없는 은행 코드 | 허브 |
| `BANK_INACTIVE` | 400 / 401 | 받는 은행이 비활성이면 400, 요청한 은행 자신이 비활성이면 401 | 허브 |
| `ACCOUNT_NOT_FOUND` | 404 | 받는 계좌 없음 | 은행 (허브가 전달) |
| `ACCOUNT_INACTIVE` | 400 | 해지·정지 계좌 | 은행 |
| `AMOUNT_INVALID` | 400 | 받는 은행 기준으로 받을 수 없는 금액 (허브는 금액 형식 오류에 `VALIDATION_ERROR`를 낸다) | 은행 |
| `LIMIT_EXCEEDED` | 400 | 허브 1회 상한 또는 받는 은행 한도 초과 | 허브·은행 |
| `SAME_BANK` | 400 | 받는 은행이 보내는 은행과 같음 (같은 은행 안의 이체는 자체 처리) | 허브 |
| `VALIDATION_ERROR` | 422 | 필드 누락·형식 오류, 금액이 0·음수·소수·문자열 (`message`에 필드 이름) | 허브·은행 |
| `TRANSFER_NOT_FOUND` | 404 | 모르는 `transfer_id` | 허브·은행 |
| `FORBIDDEN` | 403 | 남의 은행 거래 조회 | 허브 |
| `RECEIVER_TIMEOUT` | 202 / 504 | 받는 은행 응답 없음. 이체는 202(본문 `status: PENDING`), 수취 조회는 504 | 허브 |
| `RECEIVER_ERROR` | 502 | 받는 은행 5xx. 이체는 `PENDING`으로 재시도, 수취 조회는 즉시 실패 | 허브 |
| `NOT_FOUND` | 404 | 없는 경로 (주소 오타) | 허브 |
| `INTERNAL_ERROR` | 500 | 그 밖의 오류 | 허브·은행 |

---

## 6. 보내는 은행의 처리 순서 (권장)

```
1. 사용자 확인·승인, 자체 FDS 검사
2. 허브 POST /v1/inquiry 로 예금주 확인 (확인 화면용, 생략 가능)
3. [한 트랜잭션] 사용자 계좌 출금 → 타행 송금 중간 계정 입금, transfers.status = PENDING, transfer_id 생성
4. 허브 POST /v1/transfers
   ├ 200 COMPLETED → [트랜잭션] 중간 계정 차감, status = COMPLETED
   ├ 4xx FAILED    → [트랜잭션] 중간 계정 → 사용자 계좌 되돌림, status = FAILED, 사용자에게 사유 안내
   ├ 202 PENDING   → 30초마다 GET /v1/transfers/{id} (최대 30분), 결과에 따라 위 둘 중 하나
   └ 통신 오류·타임아웃 → 같은 transfer_id로 POST 재전송 (멱등이라 안전). 계속 실패하면 PENDING 조회
5. 서버 재시작 시 status = PENDING인 거래를 찾아 4의 조회부터 이어서 한다
```

**하지 말아야 할 것**
- 허브 응답을 모르는 상태에서 출금을 되돌리기 (입금이 됐을 수 있다 → 이중 지급)
- 재전송할 때 `transfer_id`를 새로 만들기 (두 번 입금된다)
- 허브 호출을 LLM 에이전트의 도구로 주기 (돈이 움직이는 호출은 이체 서비스 안에서만)

---

## 7. 연동 절차

1. 은행이 허브 관리자에게 **은행 이름, `base_url`**을 알린다.
2. 허브 관리자가 은행을 등록하고(관리자 API `POST /admin/banks`) **은행 코드, API 키, secret**을 발급한다. secret은 등록할 때 한 번만 나오며, GitHub·채팅방이 아닌 개별 경로로 그 은행 팀에만 전달한다.
3. 은행은 4절 API 3개를 만들고, 허브의 서명 검증을 붙인다.
4. 허브 관리자가 `POST /admin/banks/{bank_code}/ping`으로 `{base_url}/inquiry` 호출이 되는지 확인한다. `base_url`은 허브 서버에서 접근할 수 있는 공개 HTTPS 주소여야 한다.
5. 테스트 순서: 수취 조회 → 소액 이체 → 없는 계좌 → 같은 `transfer_id` 재전송(입금 1번만인지 확인). 받는 상대가 아직 없으면 허브의 가짜 은행 `902`로 보낸다. 받는 계좌번호 끝 4자리로 동작이 바뀐다: `0001` 정상, `0002` 계좌 없음, `0003` 지연(타임아웃), `0004` 5xx 두 번 뒤 성공, `0005` 입금은 됐는데 응답 없음.
6. 참고 구현: [`src/bank_sim/main.py`](../src/bank_sim/main.py) (4절 API만 구현한 가짜 은행), 서명은 [`src/clearing_hub/core/signing.py`](../src/clearing_hub/core/signing.py). `bank_sim`은 허브 패키지의 `signing`을 import하므로, 따로 가져갈 때는 2.3의 참고 구현을 쓴다.

---

## 8. 합의가 필요한 것 (v1.0 전에 정한다)

| # | 항목 | 초안 | 결정 |
|---|---|---|---|
| 1 | 은행 코드 배정 | 허브가 `901`부터 순서대로 배정 | |
| 2 | 계좌번호 형식 | 은행별 자유, 허브는 검사 안 함 | |
| 3 | 허브 → 은행 호출의 `X-Api-Key` | 생략 (서명만으로 확인) / 은행이 허브에 키 발급 | |
| 4 | `base_url` 아래 경로 이름 (`/inquiry`, `/receive`) | 고정 | |
| 5 | 1회 이체 상한 | 1억 원 (허브), 받는 은행 한도는 은행 자율 | |
| 6 | 받는 은행이 거절할 수 있는 조건 | 계좌 없음·정지·한도 초과만 | |
| 7 | 재시도 간격·횟수 | 1분 × 10회, 이후 `UNKNOWN` | |
| 8 | 정산 시각·방식 | 매일 00:05 KST, 전날 `COMPLETED` 상계, 표시만 | |
| 9 | 허브 도메인, 테스트 환경 주소 | 허브: `https://clearing-hub.ai-agent-develop.cloud`. 별도 테스트 환경 없이 가짜 은행 `902`로 시험 (7절 5) | 확정 (2026-11-05 배포) |
| 10 | 사기 신고 계좌 공유 (선택) | v1.1 | |

---

## 변경 이력

| 버전 | 날짜 | 내용 |
|---|---|---|
| 0.1 | 2026-11-05 | 초안 |
| 0.2 | 2026-11-05 | 허브 주소 기입, 4.3 응답에 `holder_name`(권장) 추가, `SAME_BANK` 추가, 금액·비활성 은행·수취 조회 오류의 HTTP 코드를 구현에 맞춤, 연동 절차를 관리자 API 기준으로 정리, 가짜 은행 시험 계좌 안내 |
